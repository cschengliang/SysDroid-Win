import json

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from sysdroid.core import commands as android_commands
from sysdroid.core.commands import prepare_command
from sysdroid.ui.pages.command_page import CommandLibraryPage


@pytest.fixture
def page(runner, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(android_commands, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "_adb_binary", lambda: "adb")
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    widget = CommandLibraryPage(runner)
    qtbot.addWidget(widget)
    return widget


def test_export_then_import_into_fresh_library(page, runner, qtbot, monkeypatch, tmp_path):
    target = tmp_path / "out" / "export"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
    assert page.export_commands()
    exported = json.loads((tmp_path / "out" / "export.json").read_text(encoding="utf-8"))
    assert {command["id"] for command in exported["commands"]} == set(page.store.commands)

    exported["commands"].append({**exported["commands"][0], "id": "imported", "name": "导入的命令"})
    exported["commands"][0]["name"] = "冲突改名"
    source = tmp_path / "in.json"
    source.write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(source), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args[2]) or QMessageBox.StandardButton.No)
    assert page.import_commands()
    assert "imported" in page.store.commands and asked and "冲突改名" in asked[0]
    assert page.store.commands[exported["commands"][0]["id"]].name != "冲突改名"
    names = {page.table.item(row, 1).text() for row in range(page.table.rowCount())}
    assert "导入的命令" in names

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    source.write_text(json.dumps({**exported, "commands": [{**exported["commands"][0], "name": "再次冲突"}]}), encoding="utf-8")
    assert not page.import_commands()


def test_import_of_broken_file_reports_and_keeps_library(page, monkeypatch, tmp_path):
    source = tmp_path / "broken.json"
    source.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(source), ""))
    errors = []
    monkeypatch.setattr(page, "_error", errors.append)
    before = dict(page.store.commands)
    assert not page.import_commands()
    assert errors and "无法读取" in errors[0] and page.store.commands == before


class FakeTask:
    id = "fake"


def test_execution_dialog_remembers_last_parameters(page, runner, monkeypatch):
    started = []
    monkeypatch.setattr(runner, "start_adb", lambda title, args, **kwargs: started.append(args) or FakeTask())
    page.set_device("SER", "device")
    page.open_execution("props")
    dialog = page.execution_dialog
    field = dialog.fields[0, "key"]
    assert field.text() == ""
    field.setText("ro.build.fingerprint")
    assert dialog.run_button.isEnabled()
    dialog._submit()
    assert started == [["shell", "getprop ro.build.fingerprint"]]
    page._close_dialog(dialog)
    fresh = CommandLibraryPage(runner)
    fresh.set_device("SER", "device")
    fresh.open_execution("props")
    assert fresh.execution_dialog.fields[0, "key"].text() == "ro.build.fingerprint"
    assert fresh.execution_dialog.run_button.isEnabled()
    fresh.execution_dialog.reject()


def test_run_directly_only_for_simple_commands(page, runner, monkeypatch):
    started = []
    monkeypatch.setattr(runner, "start_adb", lambda title, args, **kwargs: started.append((args, kwargs["serial"])) or FakeTask())
    opened = []
    monkeypatch.setattr(page, "open_execution", lambda command_id=None: opened.append(command_id))
    page._refresh_library(selected_id="packages")
    assert not page.run_direct_button.isEnabled()
    page.set_device("SER", "device")
    assert page.run_direct_button.isEnabled()
    page.run_direct_button.click()
    assert started == [(["shell", "pm list packages -3"], "SER")]
    page._refresh_library(selected_id="props")
    assert not page.run_direct_button.isEnabled()
    assert page.run_directly("props") is None and opened == ["props"]
    page.set_device("", "")
    assert page.run_directly("devices") is not None and started[-1] == (["devices", "-l"], "")
    texts = [action.text() for action in page.library_tools.build_menu(page._library_rows["devices"], 1).actions() if action.text()]
    assert "直接执行" in texts


def test_workflow_tab_edits_steps_and_runs_them_in_order(page, runner, monkeypatch):
    from sysdroid.core.backend import Task
    started: list[Task] = []

    def start_adb(title, args, serial="", command_id="", timeout=10, **kwargs):
        task = Task(id=f"w{len(started)}", title=title, program="adb", args=args, serial=serial, command_id=command_id)
        task.status = "running"
        started.append(task)
        return task

    monkeypatch.setattr(runner, "start_adb", start_adb)
    monkeypatch.setattr(page, "_ask_name", lambda title, text="": "巡检")
    page.set_device("SER", "device")
    page.new_workflow()
    workflow_id = page._workflow_id()
    assert page.store.workflows[workflow_id].name == "巡检" and not page.run_workflow_button.isEnabled()
    for command_id in ("devices", "props", "packages"):
        page.step_command.setCurrentIndex(page.step_command.findData(command_id))
        page.add_workflow_step()
    page.move_workflow_step(-1)
    assert page.store.workflows[workflow_id].steps == ["devices", "packages", "props"]
    page.step_list.setCurrentRow(0)
    page.remove_workflow_step()
    assert page.store.workflows[workflow_id].steps == ["packages", "props"]
    assert page.workflow_list.currentItem().text() == "巡检（2 步）"

    page.run_workflow()
    dialog = page.execution_dialog
    assert dialog.workflow_name == "巡检" and dialog.selector.isHidden()
    assert not dialog.run_button.isEnabled()
    dialog.fields[1, "key"].setText("ro.product.model")
    dialog._submit()
    assert [task.args for task in started] == [["shell", "pm list packages -3"]]
    assert "第 1/2 步" in page.workflow_status.text() and page.stop_workflow_button.isEnabled()
    started[0].status, started[0].exit_code = "succeeded", 0
    runner.task_finished.emit(started[0])
    assert started[1].args == ["shell", "getprop ro.product.model"]
    started[1].status, started[1].exit_code = "failed", 1
    runner.task_finished.emit(started[1])
    assert "第 2 步停止" in page.workflow_status.text() and not page.stop_workflow_button.isEnabled()


def test_stopping_a_workflow_cancels_the_step_and_skips_the_rest(page, runner, monkeypatch):
    from sysdroid.core.backend import Task
    from sysdroid.core.commands import Workflow
    started, cancelled = [], []

    def start_adb(title, args, **kwargs):
        task = Task(id=f"s{len(started)}", title=title, program="adb", args=args)
        task.status = "running"
        started.append(task)
        return task

    monkeypatch.setattr(runner, "start_adb", start_adb)
    monkeypatch.setattr(runner, "cancel", lambda task_id, force=False: cancelled.append(task_id))
    page.store.save_workflow(Workflow("wf", "两步", ["devices", "devices"]))
    page._refresh_workflows("wf")
    step = prepare_command(page.store.commands["devices"], {})
    page._execute_workflow("两步", [step, step])
    page.stop_workflow()
    assert cancelled == ["s0"] and "手动停止" in page.workflow_status.text()
    started[0].status = "cancelled"
    runner.task_finished.emit(started[0])
    assert len(started) == 1

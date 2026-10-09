import json

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from sysdroid.core import commands as android_commands
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

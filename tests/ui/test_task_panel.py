import sys

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QPushButton

from sysdroid.core.backend import TERMINAL_STATUSES
from sysdroid.ui.task_panel import TaskPanel


@pytest.fixture
def panel(runner, qtbot):
    widget = TaskPanel(runner)
    qtbot.addWidget(widget)
    widget.resize(900, 400)
    widget.show()
    return widget


def finished_adb(runner, title):
    task = runner.start_adb(title, ["devices"])
    runner.cancel(task.id)
    return task


def visible_titles(panel):
    return [panel.table.item(row, 0).text() for row in range(panel.table.rowCount()) if not panel.table.isRowHidden(row)]


def test_rows_are_plain_items_without_button_widgets(runner, panel):
    finished_adb(runner, "one")
    assert panel.table.columnCount() == 5
    assert panel.table.cellWidget(0, 4) is None
    assert not panel.table.findChildren(QPushButton)


def test_status_kind_and_text_filters(runner, panel, qtbot):
    finished_adb(runner, "adb stopped")
    process = runner.start_process("local ok", sys.executable, ["-c", "print('hi')"])
    qtbot.waitUntil(lambda: process.status in TERMINAL_STATUSES, timeout=10000)
    assert visible_titles(panel) == ["adb stopped", "local ok"]
    panel.status_filter.setCurrentText("成功")
    assert visible_titles(panel) == ["local ok"]
    panel.status_filter.setCurrentText("已停止")
    assert visible_titles(panel) == ["adb stopped"]
    panel.status_filter.setCurrentIndex(0)
    panel.kind_filter.setCurrentText("本地进程")
    assert visible_titles(panel) == ["local ok"]
    panel.kind_filter.setCurrentText("ADB 请求")
    assert visible_titles(panel) == ["adb stopped"]
    panel.kind_filter.setCurrentIndex(0)
    panel.filter_search.setText("LOCAL")
    assert visible_titles(panel) == ["local ok"]


def test_row_menu_and_toolbar_actions_follow_selection(runner, panel, monkeypatch):
    running = runner.start_process("sleeper", sys.executable, ["-c", "import time; time.sleep(30)"])
    row = panel._rows[running.id]
    menu = panel.table_tools.build_menu(row, 0)
    actions = {action.text(): action.isEnabled() for action in menu.actions() if action.text()}
    assert actions["停止"] and actions["强制结束…"] and actions["查看输出"]
    assert panel.row_stop_button.isEnabled() and panel.row_kill_button.isEnabled()
    menu_actions = {action.text(): action for action in menu.actions()}
    menu_actions["复制命令"].trigger()
    assert QApplication.clipboard().text() == running.command
    runner.cancel(running.id, force=True)
    adb = runner.start_adb("adb", ["devices"])
    actions = {a.text(): a.isEnabled() for a in panel.table_tools.build_menu(panel._rows[adb.id], 0).actions() if a.text()}
    assert not actions["强制结束…"]  # adb requests cannot be force-killed
    runner.cancel(adb.id)


def test_output_search_wrap_and_save(runner, panel, qtbot, tmp_path):
    process = runner.start_process("echo", sys.executable, ["-c",
                                   "import sys; print('alpha\\nneedle one\\nbeta'); print('needle err', file=sys.stderr)"])
    qtbot.waitUntil(lambda: process.status in TERMINAL_STATUSES, timeout=10000)
    panel.show_task(process.id)
    qtbot.waitUntil(lambda: "needle" in panel.stdout.toPlainText(), timeout=2000)
    panel.output_search.setText("needle")
    assert panel.stdout.textCursor().selectedText() == "needle"
    assert "共 2 处" in panel.find_label.text()
    assert panel.find_output()
    assert panel.stderr.textCursor().selectedText() == "needle"
    panel.output_search.setText("missing")
    assert panel.find_label.text() == "未找到"
    assert panel.stdout.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap
    panel.wrap_toggle.setChecked(True)
    assert panel.stderr.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    path = panel.save_output(str(tmp_path / "out.txt"))
    text = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert path and "needle one" in text and "## stderr\nneedle err" in text and process.command in text


def test_focus_search_targets_output_or_filter(runner, panel):
    finished_adb(runner, "x")
    panel.setCurrentIndex(0)
    panel.activateWindow()
    assert panel.focus_search()
    assert QApplication.focusWidget() in (panel.filter_search, None)
    panel.setCurrentIndex(1)
    assert panel.focus_search()
    assert QApplication.focusWidget() in (panel.output_search, None)


def test_pruned_task_rows_are_removed(runner, panel, qtbot, monkeypatch):
    from sysdroid.core import backend as android_backend
    monkeypatch.setattr(android_backend, "FINISHED_TASK_LIMIT", 2)
    for index in range(4):
        finished_adb(runner, f"t{index}")
    qtbot.waitUntil(lambda: panel.table.rowCount() == 2, timeout=2000)
    # The first row is auto-selected (pinned), so it survives; the oldest others go.
    assert visible_titles(panel) == ["t0", "t3"]
    assert set(panel._rows) == set(runner.tasks)

from sysdroid.core import commands as android_commands
from sysdroid.ui.pages.command_page import CommandLibraryPage


def test_history_tab_lists_only_command_library_runs(runner, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(android_commands, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "_adb_binary", lambda: "adb")
    page = CommandLibraryPage(runner)
    qtbot.addWidget(page)
    page.show()
    page.tabs.setCurrentIndex(3)
    internal = runner.start_adb("读取系统属性", ["devices"])
    library = runner.start_adb("查看设备列表", ["devices"], command_id="devices", source="command")
    runner.cancel(internal.id)
    runner.cancel(library.id)
    page._refresh_history()
    listed = {page.history_table.item(row, 0).data(0x0100) for row in range(page.history_table.rowCount())}
    assert listed == {library.id}
    assert page.clear_history_button.isEnabled()


def test_command_tables_export_checkboxes_and_history_menu(runner, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(android_commands, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "_adb_binary", lambda: "adb")
    page = CommandLibraryPage(runner)
    qtbot.addWidget(page)
    page.show()
    page.tabs.setCurrentIndex(0)
    assert page.table_tools is page.library_tools and page.table.rowCount()
    first = page.library_tools.row_values(0)
    assert first[0] in {"是", "否"} and first[1]
    texts = [action.text() for action in page.library_tools.build_menu(0, 1).actions() if action.text()]
    assert texts[:2] == ["执行…", "编辑"]
    library = runner.start_adb("查看设备列表", ["devices"], command_id="devices", source="command")
    runner.cancel(library.id)
    page.tabs.setCurrentIndex(3)
    page._refresh_history()
    texts = [action.text() for action in page.history_tools.build_menu(0, 1).actions() if action.text()]
    assert texts[:2] == ["查看输出", "复制命令"]

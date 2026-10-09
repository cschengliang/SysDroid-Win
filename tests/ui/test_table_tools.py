import csv

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLineEdit, QTableWidget, QTableWidgetItem

from sysdroid.ui.tables import TableTools


@pytest.fixture
def table(qapp):
    widget = QTableWidget(3, 3)
    widget.setHorizontalHeaderLabels(["名称", "值 ▾", "隐藏"])
    for row, values in enumerate([("b", "two\tcols"), ("a", "multi\nline"), ("c", "三")]):
        for column, text in enumerate(values):
            widget.setItem(row, column, QTableWidgetItem(text))
    widget.setColumnHidden(2, True)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    yield widget
    widget.deleteLater()


def test_copy_rows_is_tsv_of_visible_columns_without_breaking_cells(table):
    tools = TableTools(table, export_name="x")
    table.selectRow(0)
    assert tools.copy_selection() == "b\ttwo cols"
    assert QApplication.clipboard().text() == "b\ttwo cols"
    assert tools.copy_rows([0, 1]) == "b\ttwo cols\na\tmulti line"
    assert tools.copy_cell(2, 1) == "三"


def test_ctrl_c_copies_whole_selected_row(table):
    TableTools(table, export_name="x")
    table.selectRow(2)
    QApplication.clipboard().clear()
    QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "c\t三"


def test_single_cell_copy_in_item_selection_mode(table):
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
    tools = TableTools(table, export_name="x")
    table.setCurrentCell(1, 1)
    assert tools.copy_selection() == "multi\nline"


def test_export_csv_keeps_raw_values_and_skips_hidden_rows_and_columns(table, tmp_path):
    tools = TableTools(table, export_name="x", text=lambda row, column: "RAW" if (row, column) == (2, 1) else None)
    table.setRowHidden(0, True)
    path = tools.export_csv(str(tmp_path / "out.csv"))
    with open(path, encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream)) == [["名称", "值"], ["a", "multi\nline"], ["c", "RAW"]]
    assert tools.last_export_path == path


def test_menu_puts_page_actions_first_and_disables_cell_actions_off_rows(table):
    calls = []
    tools = TableTools(table, export_name="x", menu=lambda menu, row: calls.append(row) or menu.addAction("页面动作"))
    texts = [action.text() for action in tools.build_menu(1, 0).actions()]
    assert texts[0] == "页面动作" and calls == [1]
    assert {"复制单元格", "复制行", "导出 CSV…"} <= set(texts)
    empty = {action.text(): action.isEnabled() for action in tools.build_menu(-1, -1).actions() if action.text()}
    assert not empty["复制单元格"] and not empty["复制行"] and empty["导出 CSV…"]
    assert calls == [1]


def test_context_menu_selects_row_under_cursor(table, monkeypatch):
    tools = TableTools(table, export_name="x")
    shown = []
    monkeypatch.setattr(tools, "_exec_menu", lambda menu, position: shown.append([a.text() for a in menu.actions()]))
    table.resize(400, 300)
    position = table.visualRect(table.model().index(2, 0)).center()
    tools._context_menu(position)
    assert shown and table.currentRow() == 2


def test_refresh_and_search_hooks(table, qtbot):
    search = QLineEdit("query")
    qtbot.addWidget(search)
    refreshed = []
    tools = TableTools(table, export_name="x", refresh=lambda: refreshed.append(True), search=search)
    assert tools.refresh() and refreshed == [True]
    search.show()
    search.activateWindow()
    assert tools.focus_search() and search.selectedText() == "query"
    search.setEnabled(False)
    assert not tools.focus_search()
    assert not TableTools(QTableWidget(), export_name="y").refresh()


def test_sortable_flag_enables_sorting(table):
    TableTools(table, export_name="x", sortable=True)
    assert table.isSortingEnabled()

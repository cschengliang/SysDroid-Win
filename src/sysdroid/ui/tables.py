"""Shared table interactions: context menu copy / export, Ctrl+C, F5 and Ctrl+F hooks.

Pages attach a :class:`TableTools` to each data table and expose the one that
should answer F5 / Ctrl+F as ``page.table_tools``; the main window dispatches
those shortcuts to the current page so there are no competing page shortcuts.
"""
from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QFileDialog, QLineEdit, QMenu, QTableWidget


def _clean_header(text: str) -> str:
    return text.replace("▾", "").replace("▴", "").strip()


class TableTools(QObject):
    """Adds copy cell / copy rows / export CSV to a table, with optional page actions first.

    ``menu`` is called as ``menu(qmenu, row)`` before the shared actions so pages
    keep their own actions (and selection semantics) on top. ``text`` can map a
    cell to its full, unelided value for copying and export.
    """

    def __init__(self, table: QTableWidget, *, export_name: str,
                 menu: Callable[[QMenu, int], None] | None = None,
                 refresh: Callable[[], None] | None = None,
                 search: QLineEdit | None = None,
                 sortable: bool = False,
                 text: Callable[[int, int], str | None] | None = None) -> None:
        super().__init__(table)
        self.table = table
        self.export_name = export_name
        self._menu = menu
        self._refresh = refresh
        self._search = search
        self._text = text
        self.last_export_path = ""
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._context_menu)
        # QAbstractItemView's own Ctrl+C copies only the current cell; take it over.
        table.installEventFilter(self)
        if sortable:
            table.setSortingEnabled(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (watched is self.table and event.type() == QEvent.Type.KeyPress
                and isinstance(event, QKeyEvent) and event.matches(QKeySequence.StandardKey.Copy)):
            self.copy_selection()
            return True
        return super().eventFilter(watched, event)

    # -- data -------------------------------------------------------------
    def columns(self) -> list[int]:
        return [column for column in range(self.table.columnCount()) if not self.table.isColumnHidden(column)]

    def headers(self) -> list[str]:
        result = []
        for column in self.columns():
            item = self.table.horizontalHeaderItem(column)
            result.append(_clean_header(item.text()) if item is not None else str(column + 1))
        return result

    def cell_text(self, row: int, column: int) -> str:
        if self._text is not None:
            custom = self._text(row, column)
            if custom is not None:
                return custom
        item = self.table.item(row, column)
        return item.text() if item is not None else ""

    def row_values(self, row: int) -> list[str]:
        return [self.cell_text(row, column) for column in self.columns()]

    def visible_rows(self) -> list[int]:
        return [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]

    def selected_rows(self) -> list[int]:
        rows = {index.row() for index in self.table.selectionModel().selectedIndexes()} if self.table.selectionModel() else set()
        return sorted(row for row in rows if not self.table.isRowHidden(row))

    # -- actions ----------------------------------------------------------
    @staticmethod
    def _tsv(values: list[str]) -> str:
        return "\t".join(value.replace("\t", " ").replace("\r", " ").replace("\n", " ") for value in values)

    def rows_text(self, rows: list[int]) -> str:
        return "\n".join(self._tsv(self.row_values(row)) for row in rows)

    def copy_cell(self, row: int, column: int) -> str:
        text = self.cell_text(row, column)
        QApplication.clipboard().setText(text)
        return text

    def copy_rows(self, rows: list[int] | None = None) -> str:
        text = self.rows_text(self.selected_rows() if rows is None else rows)
        if text:
            QApplication.clipboard().setText(text)
        return text

    def copy_selection(self) -> str:
        """Ctrl+C: a single selected cell in item mode copies the cell, otherwise whole rows."""
        indexes = self.table.selectionModel().selectedIndexes() if self.table.selectionModel() else []
        if (len(indexes) == 1 and
                self.table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems):
            return self.copy_cell(indexes[0].row(), indexes[0].column())
        return self.copy_rows()

    def export_csv(self, path: str | None = None) -> str:
        """Write headers and visible rows (current sort / filter) as UTF-8 CSV with BOM for Excel."""
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self.table.window(), "导出 CSV",
                                                  str(Path.home() / f"{self.export_name}.csv"), "CSV (*.csv)")
            if not path:
                return ""
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(self.headers())
            for row in self.visible_rows():
                writer.writerow(self.row_values(row))
        self.last_export_path = path
        return path

    def refresh(self) -> bool:
        if self._refresh is None:
            return False
        self._refresh()
        return True

    def focus_search(self) -> bool:
        if self._search is None or not self._search.isEnabled():
            return False
        self._search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._search.selectAll()
        return True

    # -- menu -------------------------------------------------------------
    def build_menu(self, row: int, column: int) -> QMenu:
        menu = QMenu(self.table)
        if self._menu is not None and row >= 0:
            self._menu(menu, row)
            if not menu.isEmpty():
                menu.addSeparator()
        has_row = row >= 0
        menu.addAction("复制单元格", lambda: self.copy_cell(row, column)).setEnabled(has_row and column >= 0)
        rows = self.selected_rows() or ([row] if has_row else [])
        label = f"复制 {len(rows)} 行" if len(rows) > 1 else "复制行"
        menu.addAction(label, lambda: self.copy_rows(rows)).setEnabled(bool(rows))
        menu.addSeparator()
        menu.addAction("导出 CSV…", self.export_csv).setEnabled(bool(self.visible_rows()))
        return menu

    def _context_menu(self, position: QPoint) -> None:
        row = self.table.rowAt(position.y())
        column = self.table.columnAt(position.x())
        if row >= 0 and row not in self.selected_rows():
            self.table.selectRow(row)
            if column >= 0:
                self.table.setCurrentCell(row, column)
        menu = self.build_menu(row, column)
        self._exec_menu(menu, self.table.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _exec_menu(self, menu: QMenu, position: QPoint) -> None:  # separated for tests
        menu.exec(position)

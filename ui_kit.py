"""Small shared UI helpers: semantic roles, page spacing, info notes and empty states."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QAbstractItemView, QLabel, QLayout, QTableWidget, QWidget

PAGE_MARGIN = 12
PAGE_SPACING = 8
TAB_MARGIN = 10


def set_role(widget: QWidget, role: str, **properties) -> QWidget:
    """Tag a widget for theme styling (QSS selectors like QLabel[role="error"])."""
    widget.setProperty("role", role)
    for name, value in properties.items():
        widget.setProperty(name, value)
    repolish(widget)
    return widget


def set_state(widget: QWidget, state: str) -> None:
    if widget.property("state") != state:
        widget.setProperty("state", state)
        repolish(widget)


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def page_layout(layout: QLayout, margin: int = PAGE_MARGIN, spacing: int = PAGE_SPACING) -> QLayout:
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(spacing)
    return layout


def tab_layout(layout: QLayout) -> QLayout:
    return page_layout(layout, TAB_MARGIN, PAGE_SPACING)


def info_note(summary: str, details: str) -> QLabel:
    """One-line hint; the full explanation stays available as a tooltip."""
    label = QLabel(summary + "  ⓘ")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setToolTip(details)
    label.setAccessibleDescription(details)
    set_role(label, "hint")
    return label


def has_visible_rows(table: QTableWidget) -> bool:
    return any(not table.isRowHidden(row) for row in range(table.rowCount()))


def device_empty_text(serial: str, state: str, table: QTableWidget, status: str = "", idle: str = "暂无数据") -> str:
    if not serial or state != "device":
        return "未连接设备\n请在顶部选择在线设备"
    if table.rowCount():
        return "没有匹配的结果\n调整搜索词或筛选条件"
    return status.strip() or idle


class _EmptyState(QObject):
    def __init__(self, view: QAbstractItemView, text: Callable[[], str]) -> None:
        super().__init__(view)
        self._view = view
        self._text = text
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Paint and watched is self._view.viewport():
            table = self._view
            if isinstance(table, QTableWidget) and not has_visible_rows(table):
                text = self._text()
                if text:
                    painter = QPainter(watched)
                    painter.setPen(watched.palette().color(QPalette.ColorRole.PlaceholderText))
                    rect = watched.rect().adjusted(16, 16, -16, -16)
                    rect = QRect(rect.left(), rect.top(), rect.width(), min(rect.height(), 180))
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
                    painter.end()
        return False


def install_empty_state(view: QAbstractItemView, text: Callable[[], str], *signals) -> None:
    """Paint a centered hint on an item view while it has no visible rows."""
    _EmptyState(view, text)
    for signal in signals:
        signal.connect(view.viewport().update)

from __future__ import annotations

import bisect
import json
import shlex
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from sysdroid.core.backend import TaskRunner
from sysdroid.core.settings import SettingsController, SettingValue
from sysdroid.core.users import AndroidUserController
from sysdroid.ui import kit as ui_kit
from sysdroid.ui.tables import TableTools
def _note(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


# Characters QPlainTextEdit cannot round-trip as plain text: they need JSON escapes.
_JSON_ONLY = "\r\u2028\u2029"

# Common developer presets: (menu path, namespace, [(name, value), ...]).
SETTINGS_PRESETS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = tuple(
    [("动画缩放", f"{label}", "global", tuple((name, scale) for name in (
        "window_animation_scale", "transition_animation_scale", "animator_duration_scale")))
     for label, scale in (("关闭动画 (0)", "0"), ("0.5x", "0.5"), ("1x（默认）", "1"), ("2x", "2"))] +
    [("显示触摸操作", "开启", "system", (("show_touches", "1"),)),
     ("显示触摸操作", "关闭", "system", (("show_touches", "0"),)),
     ("指针位置", "开启", "system", (("pointer_location", "1"),)),
     ("指针位置", "关闭", "system", (("pointer_location", "0"),)),
     ("充电时保持唤醒", "开启（USB / 交流 / 无线）", "global", (("stay_on_while_plugged_in", "7"),)),
     ("充电时保持唤醒", "关闭", "global", (("stay_on_while_plugged_in", "0"),))])


def _needs_json(value: str) -> bool:
    return any(character in value for character in _JSON_ONLY)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _value_description(value: SettingValue | None) -> str:
    if value is None:
        return "未读取"
    if not value.exists:
        return "不存在（不是空字符串或文本 null）"
    if value.value is None:
        return "存在，但 SQL NULL 与文本 NULL 无法由当前接口区分（未确认字面值）"
    if value.value == "":
        return '空字符串（存在）：""'
    return json.dumps(value.value, ensure_ascii=True)


@dataclass(frozen=True)
class _PendingRead:
    purpose: str
    capture: tuple
    revision: int
    read_revision: int


class SettingsPage(QWidget):
    show_output = Signal(str)

    def __init__(self, runner: TaskRunner, users: AndroidUserController,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = SettingsController(runner, self)
        self._users = users
        self._active = False
        self._selected_user: int | None = None
        self._target = ("", "", "system", None)
        self._context_revision = 0
        self._task_revision = 0
        self._task_context = ("", "", False)
        self._refresh_attempted: tuple | None = None
        self._pending_read: _PendingRead | None = None
        self._confirmation_open = False
        self._loading_value = False
        self._proposed_value = ""
        self._local_error = ""
        self._proposal_error = ""
        self._table_snapshot: dict[str, SettingValue | None] = {}
        self._row_by_name: dict[str, int] = {}
        self._search_text: dict[str, str] = {}
        self._dirty = True
        self._filter_dirty = True
        self._build_ui()
        self.controller.changed.connect(self._controller_changed)
        users.changed.connect(self._users_changed)
        self._controller_changed()

    def _build_ui(self) -> None:
        layout = ui_kit.page_layout(QVBoxLayout(self))
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("当前设备"))
        self.device_input = QLineEdit()
        self.device_input.setReadOnly(True)
        self.device_input.setPlaceholderText("请在顶部选择在线设备")
        self.device_input.setObjectName("settingsDevice")
        device_row.addWidget(self.device_input, 1)
        self.output_button = QPushButton("任务输出")
        self.output_button.clicked.connect(self.open_output)
        device_row.addWidget(self.output_button)
        layout.addLayout(device_row)

        context_row = QHBoxLayout()
        context_row.addWidget(QLabel("namespace"))
        self.namespace_combo = QComboBox()
        self.namespace_combo.addItems(["system", "secure", "global"])
        self.namespace_combo.setObjectName("settingsNamespace")
        self.namespace_combo.currentTextChanged.connect(self._namespace_changed)
        context_row.addWidget(self.namespace_combo)
        context_row.addWidget(QLabel("Android 用户"))
        self.user_combo = QComboBox()
        self.user_combo.setObjectName("settingsUser")
        self.user_combo.setMinimumContentsLength(10)
        self.user_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.user_combo.currentIndexChanged.connect(self._user_selected)
        context_row.addWidget(self.user_combo, 1)
        self.users_button = QPushButton("刷新用户")
        self.users_button.clicked.connect(self._refresh_users)
        context_row.addWidget(self.users_button)
        self.refresh_button = QPushButton("刷新名称和值")
        self.refresh_button.clicked.connect(self._refresh)
        context_row.addWidget(self.refresh_button)
        self.preset_button = QToolButton()
        self.preset_button.setObjectName("settingsPresets")
        self.preset_button.setText("常用预设")
        self.preset_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        preset_menu = QMenu(self.preset_button)
        submenus: dict[str, QMenu] = {}
        for group, label, namespace, changes in SETTINGS_PRESETS:
            submenu = submenus.get(group) or submenus.setdefault(group, preset_menu.addMenu(group))
            action = submenu.addAction(label)
            action.triggered.connect(lambda _checked=False, g=group, l=label, n=namespace, c=changes:
                                     self.apply_preset(f"{g} · {l}", n, list(c)))
        self.preset_button.setMenu(preset_menu)
        context_row.addWidget(self.preset_button)
        layout.addLayout(context_row)
        self.scope_label = ui_kit.set_role(_note(), "hint")
        layout.addWidget(self.scope_label)
        self.status_label = _note()
        self.status_label.setObjectName("settingsStatus")
        ui_kit.set_role(self.status_label, "hint")
        layout.addWidget(self.status_label)
        self.error_label = _note()
        self.error_label.setObjectName("settingsError")
        ui_kit.set_role(self.error_label, "error")
        self.error_scroll = QScrollArea()
        ui_kit.set_role(self.error_scroll, "banner")
        self.error_scroll.setWidgetResizable(True)
        self.error_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.error_scroll.setMinimumHeight(50)
        self.error_scroll.setMaximumHeight(110)
        self.error_scroll.setWidget(self.error_label)
        self.error_scroll.hide()
        layout.addWidget(self.error_scroll)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("settingsSearch")
        self.search.setPlaceholderText("搜索名称或当前值（打开后自动读取）")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(self._search_changed)
        filters.addWidget(self.search, 1)
        self.count_label = QLabel()
        filters.addWidget(self.count_label)
        layout.addLayout(filters)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("settingsTable")
        self.table.setHorizontalHeaderLabels(["名称", "已读取当前值"])
        self.table.horizontalHeaderItem(1).setToolTip(
            "打开页面、切换用户 / namespace 或刷新时自动读取值；空字符串留白，悬停可查看状态。")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setColumnWidth(0, 210)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(90)
        self.table.setMinimumWidth(240)
        self.table.setMinimumHeight(100)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table_tools = TableTools(self.table, export_name="settings", menu=self._extend_menu,
                                      refresh=self.refresh_button.click, search=self.search, text=self._cell_text)
        self.splitter.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: ui_kit.device_empty_text(
            self.controller.serial, self.controller.device_state, self.table, self.status_label.text(), "暂无设置项 · 点击「刷新名称和值」读取"),
            self.controller.changed)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumWidth(280)
        detail_scroll.setMinimumHeight(150)
        editor = QGroupBox("读取 / 新增 / 修改 / 删除")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setSpacing(7)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_field = QLineEdit()
        self.name_field.setObjectName("settingsName")
        self.name_field.setMaxLength(2**31 - 1)
        self.name_field.setPlaceholderText("选择名称，或输入要读取 / 新增的名称")
        self.name_field.textChanged.connect(self._name_changed)
        form.addRow("名称", self.name_field)
        minimum_editor_height = self.fontMetrics().lineSpacing() * 3 + 14
        self.current_value = QPlainTextEdit()
        self.current_value.setObjectName("settingsCurrentValue")
        self.current_value.setReadOnly(True)
        self.current_value.setMinimumHeight(minimum_editor_height)
        self.current_value.setMaximumHeight(max(100, minimum_editor_height))
        form.addRow("当前实际值", self.current_value)
        self.current_value_note = _note()
        form.addRow(self.current_value_note)
        self.value_field = QPlainTextEdit()
        self.value_field.setObjectName("settingsValue")
        self.value_field.setMinimumHeight(minimum_editor_height)
        self.value_field.setMaximumHeight(max(120, minimum_editor_height))
        self.value_field.textChanged.connect(self._proposal_changed)
        form.addRow("拟写入值", self.value_field)
        mode_row = QHBoxLayout()
        self.json_mode = QCheckBox("JSON 转义模式")
        self.json_mode.setObjectName("settingsJsonMode")
        self.json_mode.setToolTip("默认按原文输入；需要 \\r 或 Unicode 行/段分隔符时使用 JSON 字符串精确表示。")
        self.json_mode.toggled.connect(self._json_mode_toggled)
        mode_row.addWidget(self.json_mode)
        mode_row.addStretch()
        form.addRow(mode_row)
        self.value_note = _note()
        form.addRow(self.value_note)
        editor_layout.addLayout(form)
        read_row = QHBoxLayout()
        self.new_button = QPushButton("新增 / 清空编辑")
        self.new_button.clicked.connect(self._new)
        read_row.addWidget(self.new_button)
        self.read_button = QPushButton("读取当前值")
        self.read_button.clicked.connect(self._read)
        read_row.addWidget(self.read_button)
        editor_layout.addLayout(read_row)
        change_row = QHBoxLayout()
        self.write_button = QPushButton("确认写入…")
        self.write_button.clicked.connect(self._write)
        change_row.addWidget(self.write_button)
        self.delete_button = QPushButton("确认删除…")
        self.delete_button.clicked.connect(self._delete)
        change_row.addWidget(self.delete_button)
        editor_layout.addLayout(change_row)
        editor_layout.addWidget(ui_kit.info_note(
            "写入前先读取最新值，提交后核对实际值；不自动提权。",
            "修改前先读取最新目标，确认后提交并核对实际值。权限由设备决定，"
            "不自动 Root/su、不重置 namespace、不修改数据库文件。"
            "列表和当前值是读取快照，并非持续同步；空字符串、文本 null 与不存在不同。"))
        editor_layout.addStretch()
        detail_scroll.setWidget(editor)
        self.splitter.addWidget(detail_scroll)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([640, 320])
        layout.addWidget(self.splitter, 1)
        self._update_mode_hint()
        self._update_proposal_note()

    def _row_name(self, row: int) -> str:
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def _cell_text(self, row: int, column: int) -> str | None:
        if column != 1:
            return None
        observed = self.controller.values.get(self._row_name(row))
        return observed.value if observed is not None and observed.exists and observed.value is not None else ""

    def _extend_menu(self, menu: QMenu, row: int) -> None:
        name = self._row_name(row)
        if not name:
            return
        if self._selected_name() != name:
            self.table.selectRow(row)
        menu.addAction("读取当前值", self._read).setEnabled(self.read_button.isEnabled())
        menu.addAction("复制名称", lambda: QApplication.clipboard().setText(name))
        observed = self.controller.values.get(name)
        if observed is not None and observed.exists and observed.value is not None:
            value = observed.value
            menu.addAction("复制原始值", lambda: QApplication.clipboard().setText(value))
            menu.addAction("复制为 settings put 命令（设备 shell）", lambda: QApplication.clipboard().setText(shlex.join(
                ["settings", "put", "--user", str(self.controller.user_id),
                 self.controller.namespace, name, value])))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([max(300, self.height() // 2), 320])

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) != (self.controller.serial, self.controller.device_state):
            self._selected_user = None
        self.controller.set_device(serial, state)
        self._users_changed()
        self._ensure_loaded()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._context_revision += 1
        self._active = active
        if active:
            if self._dirty:
                self._render_table()
            self._users_changed()
            self._ensure_loaded()

    def _shared_context_matches(self) -> bool:
        return (self._users.serial, self._users.device_state) == (
            self.controller.serial, self.controller.device_state)

    def _ensure_loaded(self) -> None:
        if not self._active or not self._shared_context_matches():
            return
        self._users.ensure_loaded()
        context = self._target
        if (self._ready() and not self._confirmation_open and self._pending_read is None
                and self._refresh_attempted != context):
            self._refresh_attempted = context
            self._refresh()

    def _users_changed(self) -> None:
        if not self._shared_context_matches():
            return
        users = self._users.users
        chosen = self._selected_user if self._selected_user in users else self._users.current_user
        if chosen not in users:
            chosen = None
        blocked = self.user_combo.blockSignals(True)
        try:
            self.user_combo.clear()
            for user_id, name in sorted(users.items()):
                self.user_combo.addItem(f"{user_id} · {name}", user_id)
            if chosen is not None:
                self.user_combo.setCurrentIndex(self.user_combo.findData(chosen))
        finally:
            self.user_combo.blockSignals(blocked)
        self._selected_user = chosen
        if chosen is None:
            self.controller._invalidate_user()
        else:
            self.controller.set_context(self.namespace_combo.currentText(), chosen)
        self._update_enabled()
        self._display_error()
        self._ensure_loaded()

    def _namespace_changed(self, namespace: str) -> None:
        if self._selected_user is not None:
            self.controller.set_context(namespace, self._selected_user)
        else:
            self._context_revision += 1
            self._clear_editor()
        self._update_scope()
        self._ensure_loaded()

    def _user_selected(self, index: int) -> None:
        chosen = self.user_combo.itemData(index)
        if chosen is None:
            return
        self._selected_user = chosen
        self.controller.set_context(self.namespace_combo.currentText(), chosen)
        self._ensure_loaded()

    def _update_scope(self) -> None:
        self.scope_label.setText("设备全局，共享设置；所选 user 仅用于访问协议，不代表值按用户隔离。"
                                 if self.namespace_combo.currentText() == "global" else
                                 "所选 Android 用户的 Settings；实际访问权限由设备决定。")

    def _controller_changed(self) -> None:
        controller = self.controller
        target = (controller.serial, controller.device_state, controller.namespace, controller.user_id)
        task_context = (controller.task_id, controller.last_task_id, controller.busy)
        if target != self._target:
            self._target = target
            self._context_revision += 1
            self._refresh_attempted = None
            self._local_error = ""
            self._pending_read = None
            self._clear_editor()
        if task_context != self._task_context:
            self._task_context = task_context
            self._task_revision += 1
        self.device_input.setText(f"{controller.serial} · {controller.device_state}" if controller.serial else "")
        self.device_input.setToolTip(self.device_input.text())
        self.status_label.setText(controller.status or
                                  ("名称与值尚未读取；首次进入将发现用户并自动加载名称和值。"
                                   if controller.serial and controller.device_state == "device" else
                                   "请在顶部选择在线设备"))
        self._update_scope()
        self._dirty = True
        if self._active:
            self._render_table()
        self._update_current_value()
        self._update_enabled()
        self._display_error()
        self._complete_pending_read()
        self._ensure_loaded()

    def _selected_name(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row(), 0) if rows else None
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _render_table(self) -> None:
        snapshot = {name: self.controller.values.get(name) for name in self.controller.names}
        self._dirty = False
        if snapshot == self._table_snapshot:
            if self._filter_dirty and not self._search_timer.isActive():
                self._apply_filter()
            return
        selected = self._selected_name()
        scroll = self.table.verticalScrollBar().value()
        horizontal_scroll = self.table.horizontalScrollBar().value()
        blocked = self.table.blockSignals(True)
        try:
            for name, row in sorted(self._row_by_name.items(), key=lambda item: item[1], reverse=True):
                if name not in snapshot:
                    self.table.removeRow(row)
                    self._search_text.pop(name, None)
            current = [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                       for row in range(self.table.rowCount())]
            for name in sorted(snapshot.keys() - self._table_snapshot.keys()):
                row = bisect.bisect_left(current, name)
                self.table.insertRow(row)
                name_item = QTableWidgetItem(name)
                name_item.setData(Qt.ItemDataRole.UserRole, name)
                name_item.setToolTip(name)
                self.table.setItem(row, 0, name_item)
                self.table.setItem(row, 1, QTableWidgetItem())
                current.insert(row, name)
            self._row_by_name = {name: row for row, name in enumerate(current)}
            for name, observed in snapshot.items():
                if name not in self._table_snapshot or observed != self._table_snapshot[name]:
                    item = self.table.item(self._row_by_name[name], 1)
                    text = _value_description(observed)
                    item.setText("" if observed is None or
                                 (observed.exists and observed.value == "") else text)
                    item.setToolTip("尚未读取；自动加载进行中或失败，可刷新名称和值重新查询。"
                                    if observed is None else text)
                    value = observed.value if observed is not None and observed.exists else ""
                    self._search_text[name] = (name + "\n" + (value or "")).casefold()
            if selected in self._row_by_name:
                self.table.selectRow(self._row_by_name[selected])
            self._table_snapshot = snapshot
            self._apply_filter()
            self.table.verticalScrollBar().setValue(scroll)
            self.table.horizontalScrollBar().setValue(horizontal_scroll)
        finally:
            self.table.blockSignals(blocked)

    def _search_changed(self) -> None:
        self._filter_dirty = True
        self._search_timer.start()

    def _apply_filter(self) -> None:
        if not self._active:
            self._filter_dirty = True
            self._dirty = True
            return
        self._filter_dirty = False
        query = self.search.text().casefold()
        visible = 0
        for name, row in self._row_by_name.items():
            hidden = query not in self._search_text[name]
            self.table.setRowHidden(row, hidden)
            visible += not hidden
        read_count = sum(value is not None for value in self._table_snapshot.values())
        self.count_label.setText(f"{visible} / {len(self._row_by_name)} 项 · 已读 {read_count}")

    def _selection_changed(self) -> None:
        name = self._selected_name()
        if not name:
            return
        self.name_field.setText(name)
        if self._ready() and not self._confirmation_open:
            self._begin_read("proposal")

    def _clear_editor(self) -> None:
        blocked = self.table.blockSignals(True)
        try:
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        finally:
            self.table.blockSignals(blocked)
        self.name_field.clear()
        self._set_proposed_value("")
        self._update_current_value()

    def _new(self) -> None:
        if not self._ready() or self._confirmation_open:
            return
        self._local_error = ""
        self._context_revision += 1
        self._clear_editor()
        self.name_field.setFocus()
        self._display_error()

    def _name_changed(self) -> None:
        self._context_revision += 1
        self._local_error = ""
        self._update_current_value()
        self._update_enabled()
        self._display_error()

    def _set_proposed_value(self, value: str) -> None:
        if self._proposed_value != value:
            self._context_revision += 1
        if _needs_json(value) and not self.json_mode.isChecked():
            # Plain text cannot preserve these characters exactly.
            with QSignalBlocker(self.json_mode):
                self.json_mode.setChecked(True)
        self._loading_value = True
        try:
            self.value_field.setPlainText(json.dumps(value, ensure_ascii=True) if self.json_mode.isChecked() else value)
            self._proposed_value = value
            self._proposal_error = ""
        finally:
            self._loading_value = False
        self._update_mode_hint()
        self._update_proposal_note()

    def _json_mode_toggled(self, checked: bool) -> None:
        if not checked and _needs_json(self._proposed_value):
            with QSignalBlocker(self.json_mode):
                self.json_mode.setChecked(True)
            self._set_local_error("拟写入值包含 \\r 或 Unicode 行/段分隔符，只能在 JSON 模式下精确编辑。")
            self._display_error()
            return
        # Re-render the last valid proposal in the new representation.
        self._local_error = ""
        self._set_proposed_value(self._proposed_value)
        self._update_current_value()
        self._display_error()
        self._update_enabled()

    def _update_mode_hint(self) -> None:
        self.value_field.setPlaceholderText(
            '输入 JSON 字符串，例如 "text"、""；换行使用 \\r / \\n，Unicode 可用 \\u 转义'
            if self.json_mode.isChecked() else "按原文输入要写入的值；留空表示写入空字符串（不是删除）")

    def _proposal_changed(self) -> None:
        if self._loading_value:
            return
        try:
            encoded = self.value_field.document().toRawText()
            if not self.json_mode.isChecked():
                # Qt stores line breaks as paragraph / line separators.
                self._proposed_value = encoded.replace("\u2029", "\n").replace("\u2028", "\n")
                self._proposal_error = ""
            else:
                if any(character in encoded for character in "\r\n\u2028\u2029"):
                    raise ValueError("换行及段落分隔符必须使用 JSON 转义")
                value = json.loads(encoded)
                if not isinstance(value, str):
                    raise ValueError("拟写入值必须是 JSON 字符串，而不是 null、数字或对象")
                self._proposed_value = value
                self._proposal_error = ""
        except (ValueError, json.JSONDecodeError) as exc:
            self._proposal_error = f"拟写入 JSON 字符串无效：{exc}"
        self._context_revision += 1
        self._local_error = ""
        self._update_proposal_note()
        self._display_error()
        self._update_enabled()

    def _update_proposal_note(self) -> None:
        self.value_note.setText("拟写入空字符串（不是删除）。" if not self._proposed_value else
                                f"拟写入 {len(self._proposed_value)} 个原始字符；空白与换行保留。")
        self.value_field.setToolTip("原始拟写入值（JSON）：\n" + _quoted(self._proposed_value))

    def _update_current_value(self) -> None:
        name = self.name_field.text()
        observed = self.controller.values.get(name)
        value = observed.value if observed is not None and observed.exists else None
        plain = value is not None and not self.json_mode.isChecked() and not _needs_json(value)
        display = value if plain else json.dumps(value, ensure_ascii=True) if value is not None else ""
        if self.current_value.toPlainText() != display:
            self.current_value.setPlainText(display)
        description = _value_description(observed)
        self.current_value.setPlaceholderText(description if not value else "")
        self.current_value.setToolTip(description)
        self.current_value_note.setText(
            f"当前实际值：{len(value)} 个原始字符{'' if plain else '（JSON 转义显示）'}。"
            if value is not None else description)

    def _target_ready(self) -> bool:
        return (bool(self.controller.serial) and self.controller.device_state == "device" and
                self._selected_user is not None and self.controller.user_id == self._selected_user and
                self._shared_context_matches() and self._selected_user in self._users.users)

    def _ready(self) -> bool:
        return self._target_ready() and not self.controller.busy

    def _update_enabled(self) -> None:
        actions_ready = self._ready() and not self._confirmation_open and self._pending_read is None
        self.name_field.setEnabled(self._target_ready())
        self.value_field.setEnabled(self._target_ready())
        self.refresh_button.setEnabled(actions_ready)
        self.preset_button.setEnabled(actions_ready)
        self.new_button.setEnabled(actions_ready)
        self.read_button.setEnabled(actions_ready and bool(self.name_field.text()))
        self.write_button.setEnabled(actions_ready and bool(self.name_field.text()) and not self._proposal_error)
        self.delete_button.setEnabled(actions_ready and bool(self.name_field.text()))
        self.user_combo.setEnabled(bool(self._users.users) and self._shared_context_matches())
        self.users_button.setEnabled(bool(self.controller.serial) and self.controller.device_state == "device"
                                     and self._shared_context_matches() and not self._users.busy)
        self.output_button.setEnabled(bool(self.controller.last_task_id))

    def _display_error(self) -> None:
        user_error = self._users.error if self._shared_context_matches() else ""
        text = "\n".join(message for message in (user_error, self.controller.error, self._local_error, self._proposal_error) if message)
        self.error_label.setText(text)
        self.error_scroll.setVisible(bool(text))

    def _set_local_error(self, text: str) -> None:
        self._local_error = text
        self._display_error()

    def _refresh_users(self) -> None:
        try:
            self._users.refresh()
        except ValueError as exc:
            self._set_local_error(str(exc))

    def _refresh(self) -> None:
        if self._confirmation_open or self._pending_read is not None:
            return
        self._local_error = ""
        self._refresh_attempted = self._target
        try:
            self.controller.refresh()
        except ValueError as exc:
            self._set_local_error(str(exc))

    def _capture(self) -> tuple:
        return (self.controller.serial, self.controller.device_state, self.controller.namespace,
                self.controller.user_id, self.name_field.text(), self._proposed_value)

    def _capture_valid(self, pending: _PendingRead) -> bool:
        return (pending.revision == self._context_revision and pending.capture == self._capture() and
                self._ready())

    def _begin_read(self, purpose: str) -> None:
        if self._confirmation_open or self._pending_read is not None or not self._ready():
            return
        pending = _PendingRead(purpose, self._capture(), self._context_revision, self.controller.read_revision)
        self._pending_read = pending
        self._local_error = ""
        try:
            task = self.controller.read(pending.capture[4])
        except ValueError as exc:
            if self._pending_read is pending:
                self._pending_read = None
                self._set_local_error(str(exc))
        else:
            if task is None and self._pending_read is pending:
                self._pending_read = None
        self._update_enabled()

    def _read(self) -> None:
        self._begin_read("proposal")

    def _write(self) -> None:
        if self._proposal_error:
            self._display_error()
            return
        self._begin_read("write")

    def _delete(self) -> None:
        self._begin_read("delete")

    def _complete_pending_read(self) -> None:
        pending = self._pending_read
        if pending is None or self.controller.busy:
            return
        self._pending_read = None
        completed = (self.controller.read_revision > pending.read_revision and
                     self.controller.last_read_name == pending.capture[4])
        if not completed:
            self._update_enabled()
            return
        if not self._capture_valid(pending):
            if pending.purpose != "proposal":
                self._set_local_error("目标或拟写入值在预读期间已变化；未提交，请重新确认。")
            self._update_enabled()
            return
        if pending.purpose == "proposal":
            observed = self.controller.values[pending.capture[4]]
            self._set_proposed_value(observed.value if observed.value is not None else "")
        else:
            self._confirm_mutation(pending)
        self._update_enabled()

    def _confirm_mutation(self, pending: _PendingRead) -> None:
        serial, state, namespace, user_id, name, proposal = pending.capture
        old_value = _value_description(self.controller.values.get(name))
        operation = pending.purpose
        confirmation = QMessageBox(self)
        confirmation.setObjectName("settingsWriteConfirmation" if operation == "write" else "settingsDeleteConfirmation")
        confirmation.setWindowTitle("确认写入 Settings" if operation == "write" else "确认删除 Settings")
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText(
            f"设备：{_quoted(serial)} · {state}\nnamespace：{namespace}\nuser：{user_id}\n"
            f"名称：{_quoted(name)}\n\n最新当前值：{old_value}\n" +
            (f"拟写入值：{_quoted(proposal)}" if operation == "write" else "将删除此名称及其值。"))
        confirmation.setInformativeText(
            "引号内使用 JSON 转义展示原始文本。空字符串不是删除；AOSP 可能把文本 null 归一为 provider null，不能据此确认字面保存。\n"
            + ("global 是设备全局共享设置，修改可影响其他用户。\n" if namespace == "global" else "")
            + "任何设置变更都可能影响系统服务；不自动提权。确认后仅提交捕获目标并读取设备核对。继续？")
        confirmation.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        confirmation.setDefaultButton(QMessageBox.StandardButton.No)
        confirmation.setEscapeButton(QMessageBox.StandardButton.No)
        task_revision = self._task_revision
        self._confirmation_open = True
        self._update_enabled()
        try:
            answer = confirmation.exec()
        finally:
            self._confirmation_open = False
            confirmation.deleteLater()
            self._update_enabled()
            self._ensure_loaded()
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._capture_valid(pending) or task_revision != self._task_revision:
            self._set_local_error("设备、用户、namespace、名称、拟写入值或任务状态在确认期间已变化；未提交。")
            return
        try:
            if operation == "write":
                self.controller.write(name, proposal)
            else:
                self.controller.delete(name)
        except ValueError as exc:
            self._set_local_error(str(exc))

    def apply_preset(self, title: str, namespace: str, changes: list[tuple[str, str]]) -> bool:
        """Confirm and write a preset in one verified round trip."""
        if not self._ready() or self._pending_read is not None or self._confirmation_open:
            self._set_local_error("设备或 Settings 正忙，暂不能应用预设。")
            self._display_error()
            return False
        capture = (self.controller.serial, self.controller.device_state, self.controller.user_id, self._task_revision)
        lines = "\n".join(f"  {namespace}/{name} = {_quoted(value)}" for name, value in changes)
        self._confirmation_open = True
        self._update_enabled()
        try:
            answer = QMessageBox.question(
                self, "应用 Settings 预设",
                f"预设：{title}\n设备：{self.controller.serial}\nuser：{self.controller.user_id}\n\n{lines}\n\n"
                + ("global 是设备全局共享设置，修改可影响其他用户。\n" if namespace == "global" else "")
                + "提交后会立即读回核对。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        finally:
            self._confirmation_open = False
            self._update_enabled()
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if capture != (self.controller.serial, self.controller.device_state, self.controller.user_id,
                       self._task_revision) or not self._ready():
            self._set_local_error("设备、用户或任务状态在确认期间已变化；预设未提交。")
            self._display_error()
            return False
        try:
            self.controller.write_many(namespace, changes)
        except ValueError as exc:
            self._set_local_error(str(exc))
            self._display_error()
            return False
        return True

    def open_output(self) -> None:
        if self.controller.last_task_id:
            self.show_output.emit(self.controller.last_task_id)

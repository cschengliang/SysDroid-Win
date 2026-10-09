from __future__ import annotations

import json

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from android_backend import TaskRunner
from android_props import PropController
import ui_kit


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _property_type(name: str) -> str:
    if name.startswith("ro."):
        return "通常只读"
    if name.startswith("persist."):
        return "持久化前缀"
    return "普通属性"


def _quoted(value: str) -> str:
    """Make empty strings and whitespace visible without changing their data."""
    return json.dumps(value, ensure_ascii=False)


class PropPage(QWidget):
    """Native property browser/editor; the controller owns all ADB operations."""

    show_output = Signal(str)

    def __init__(self, runner: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = PropController(runner, self)
        self._target = (self.controller.serial, self.controller.device_state)
        self._context = (*self._target, self.controller.task_id, self.controller.last_task_id)
        self._context_revision = 0
        self._table_snapshot: dict[str, str] | None = None
        self._column_widths_initialized = False
        self._active = False
        self._initial_requested = False
        self._editor_floating = False
        self._editor_sizes = {Qt.Orientation.Horizontal: [650, 320]}
        self._rows: dict[str, int] = {}
        self._names: tuple[str, ...] = ()
        self._search_text: dict[str, str] = {}
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filter)
        self._local_error = ""
        self._confirmation_open = False
        self._loading_value = False
        self._proposed_value = ""
        self._build_ui()
        self.controller.changed.connect(self._controller_changed)
        self._controller_changed()

    def _build_ui(self) -> None:
        layout = ui_kit.page_layout(QVBoxLayout(self))
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("当前设备"))
        self.device_input = QLineEdit()
        self.device_input.setObjectName("propDevice")
        self.device_input.setReadOnly(True)
        self.device_input.setPlaceholderText("请在顶部选择在线设备")
        device_row.addWidget(self.device_input, 1)
        self.refresh_button = QPushButton("刷新属性")
        self.refresh_button.setObjectName("propRefresh")
        self.refresh_button.clicked.connect(self._refresh)
        device_row.addWidget(self.refresh_button)
        self.output_button = QPushButton("任务输出")
        self.output_button.setObjectName("propOutput")
        self.output_button.setToolTip("在任务面板查看最近一次属性请求的原始输出")
        self.output_button.clicked.connect(self.open_output)
        device_row.addWidget(self.output_button)
        layout.addLayout(device_row)

        self.status_label = _note("")
        self.status_label.setObjectName("propStatus")
        ui_kit.set_role(self.status_label, "hint")
        layout.addWidget(self.status_label)
        self.error_label = _note("")
        self.error_label.setObjectName("propError")
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
        self.search.setObjectName("propSearch")
        self.search.setPlaceholderText("搜索属性名称或值")
        self.search.textChanged.connect(lambda: self._search_timer.start())
        filters.addWidget(self.search, 1)
        self.type_filter = QComboBox()
        self.type_filter.setObjectName("propTypeFilter")
        self.type_filter.setAccessibleName("按属性前缀类型筛选")
        self.type_filter.addItem("全部前缀", "")
        for prefix, name in (("ro.*", "通常只读"), ("persist.*", "持久化前缀"), ("其他", "普通属性")):
            self.type_filter.addItem(f"{prefix} · {name}", name)
        self.type_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.type_filter)
        self.count_label = QLabel()
        self.count_label.setObjectName("propCount")
        filters.addWidget(self.count_label)
        self.editor_toggle_button = QPushButton("关闭编辑区")
        self.editor_toggle_button.setObjectName("propEditorToggle")
        self.editor_toggle_button.setCheckable(True)
        self.editor_toggle_button.setChecked(True)
        self.editor_toggle_button.setToolTip("显示或关闭属性读取 / 编辑区；保留未提交的内容")
        self.editor_toggle_button.toggled.connect(self._set_editor_visible)
        filters.addWidget(self.editor_toggle_button)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.detail_splitter = splitter
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("propTable")
        self.table.setHorizontalHeaderLabels(["属性名称", "当前值", "前缀类型 ▾"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(72)
        self.table.horizontalHeader().setResizeContentsPrecision(200)
        for column, width in enumerate((280, 220, 104)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._choose_type_filter)
        self.table.horizontalHeaderItem(2).setToolTip("点击选择前缀类型；前缀仅为惯例，不保证权限或持久化。")
        self.table.setMinimumHeight(100)
        self.table.setMinimumWidth(250)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: ui_kit.device_empty_text(
            self.controller.serial, self.controller.device_state, self.table, self.status_label.text(), "暂无属性 · 点击「刷新属性」读取"),
            self.controller.changed)

        editor_scroll = QScrollArea()
        self.editor_scroll = editor_scroll
        editor_scroll.setObjectName("propEditorScroll")
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        editor_scroll.setMinimumHeight(150)
        editor_scroll.setMinimumWidth(320)
        editor = QGroupBox("读取 / 编辑 / 新增属性")
        editor_layout = QVBoxLayout(editor)
        editor_controls = QHBoxLayout()
        editor_controls.addStretch()
        self.editor_popout_button = QPushButton("弹出窗口")
        self.editor_popout_button.setObjectName("propEditorPopout")
        self.editor_popout_button.clicked.connect(self._toggle_editor_window)
        editor_controls.addWidget(self.editor_popout_button)
        self.editor_close_button = QPushButton("关闭")
        self.editor_close_button.setObjectName("propEditorClose")
        self.editor_close_button.setToolTip("关闭编辑区；可从顶部“编辑区”重新打开")
        self.editor_close_button.clicked.connect(lambda: self.editor_toggle_button.setChecked(False))
        editor_controls.addWidget(self.editor_close_button)
        editor_layout.addLayout(editor_controls)
        self.editor_device_label = _note("")
        self.editor_device_label.setObjectName("propEditorDevice")
        editor_layout.addWidget(self.editor_device_label)
        self.editor_error_label = _note("")
        self.editor_error_label.setObjectName("propEditorError")
        ui_kit.set_role(self.editor_error_label, "error")
        editor_layout.addWidget(self.editor_error_label)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_field = QLineEdit()
        self.name_field.setObjectName("propName")
        self.name_field.setMaxLength(2**31 - 1)
        self.name_field.setPlaceholderText("选择列表属性，或输入要读取 / 新增的名称")
        self.name_field.textChanged.connect(self._name_changed)
        form.addRow("属性名称", self.name_field)

        self.current_value = QPlainTextEdit()
        self.current_value.setObjectName("propCurrentValue")
        self.current_value.setAccessibleName("当前实际属性值")
        self.current_value.setReadOnly(True)
        self.current_value.setMinimumHeight(self.current_value.fontMetrics().lineSpacing() * 3 + 16)
        self.current_value.setMaximumHeight(110)
        form.addRow("当前值", self.current_value)
        self.current_value_note = _note("")
        form.addRow(self.current_value_note)

        self.value_field = QPlainTextEdit()
        self.value_field.setObjectName("propValue")
        self.value_field.setAccessibleName("拟写入属性值")
        self.value_field.setPlaceholderText("留空写入空字符串（不是删除）；空格和换行按原样提交")
        self.value_field.setMinimumHeight(self.value_field.fontMetrics().lineSpacing() * 3 + 16)
        self.value_field.setMaximumHeight(130)
        self.value_field.textChanged.connect(self._proposal_changed)
        form.addRow("拟写入值", self.value_field)
        self.value_note = _note("拟写入：空字符串（不是删除属性）。")
        form.addRow(self.value_note)
        editor_layout.addLayout(form)
        actions = QHBoxLayout()
        self.new_button = QPushButton("新增 / 清空编辑")
        self.new_button.setObjectName("propNew")
        self.new_button.clicked.connect(self._new)
        actions.addWidget(self.new_button)
        actions.addStretch()
        self.read_button = QPushButton("读取当前值")
        self.read_button.setObjectName("propRead")
        self.read_button.clicked.connect(self._read)
        actions.addWidget(self.read_button)
        self.write_button = QPushButton("确认写入…")
        self.write_button.setObjectName("propWrite")
        self.write_button.clicked.connect(self._write)
        actions.addWidget(self.write_button)
        editor_layout.addLayout(actions)
        editor_layout.addWidget(_note(
            "ro.* 通常只读；persist.* 是持久化前缀；其他为普通属性。"
            "实际写入权限和持久化行为取决于设备，前缀不是权限保证。"
            "任何属性都可能影响系统服务。仅使用 getprop / setprop，"
            "不自动提权、不修改 build.prop；空值不代表删除。"
        ))
        editor_scroll.setWidget(editor)
        splitter.addWidget(editor_scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([650, 320])
        layout.addWidget(splitter, 1)
        self.editor_window = QDialog(self)
        self.editor_window.setObjectName("propEditorWindow")
        self.editor_window.setWindowTitle("系统属性 · 读取 / 编辑")
        self.editor_window.resize(540, 660)
        window_layout = QVBoxLayout(self.editor_window)
        window_layout.setContentsMargins(8, 8, 8, 8)
        self.editor_window.finished.connect(lambda result: self.editor_toggle_button.setChecked(False))

    def _remember_editor_sizes(self) -> None:
        sizes = self.detail_splitter.sizes()
        if not self._editor_floating and not self.editor_scroll.isHidden() and len(sizes) == 2 and all(sizes):
            self._editor_sizes[self.detail_splitter.orientation()] = sizes

    def _restore_editor_sizes(self) -> None:
        self.detail_splitter.setSizes(self._editor_sizes.get(self.detail_splitter.orientation(), [300, 320]))

    def _update_editor_chrome(self) -> None:
        self.editor_popout_button.setText("嵌回页面" if self._editor_floating else "弹出窗口")
        serial, state = self.controller.serial, self.controller.device_state
        device = f"{serial} · {state or '未知状态'}" if serial else "未选择设备"
        self.editor_window.setWindowTitle(f"系统属性 · 读取 / 编辑 · {serial or '未选择设备'}")
        self.editor_device_label.setText(f"当前设备：{device}\n{self.status_label.text()}")
        self.editor_device_label.setVisible(self._editor_floating)
        self.editor_error_label.setVisible(self._editor_floating and bool(self.editor_error_label.text()))

    def _dock_editor(self) -> None:
        self.editor_window.layout().removeWidget(self.editor_scroll)
        self.detail_splitter.addWidget(self.editor_scroll)
        self.detail_splitter.setStretchFactor(1, 1)
        self._editor_floating = False
        self.editor_window.hide()
        self.editor_scroll.setVisible(self.editor_toggle_button.isChecked())
        if self.editor_toggle_button.isChecked():
            self._restore_editor_sizes()
        self._update_editor_chrome()

    def _set_editor_visible(self, visible: bool) -> None:
        self._context_revision += 1
        self.editor_toggle_button.setText("关闭编辑区" if visible else "显示编辑区")
        if not visible:
            self._remember_editor_sizes()
            if self._editor_floating:
                self._dock_editor()
        self.editor_scroll.setVisible(visible)
        if visible and not self._editor_floating:
            self._restore_editor_sizes()

    def _toggle_editor_window(self) -> None:
        if self._confirmation_open:
            return
        self._context_revision += 1
        if self._editor_floating:
            self._dock_editor()
        else:
            self._remember_editor_sizes()
            self.editor_window.layout().addWidget(self.editor_scroll)
            self._editor_floating = True
            self._update_editor_chrome()
            self.editor_window.setVisible(self._active)

    def set_device(self, serial: str, state: str = "device") -> None:
        self.controller.set_device(serial, state)
        self._ensure_loaded()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._context_revision += 1
        self._active = active
        if active:
            self._controller_changed()
            self._apply_filter()
            self._ensure_loaded()
        self.editor_window.setVisible(active and self._editor_floating and self.editor_toggle_button.isChecked())

    def _ensure_loaded(self) -> None:
        if (self._active and self._ready() and not self.controller.snapshot_valid
                and not self._initial_requested):
            self._initial_requested = True
            self._refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal
        if self.detail_splitter.orientation() != orientation:
            self._remember_editor_sizes()
            self.detail_splitter.setOrientation(orientation)
            if not self._editor_floating and self.editor_toggle_button.isChecked():
                self._restore_editor_sizes()

    def _controller_changed(self) -> None:
        target = (self.controller.serial, self.controller.device_state)
        context = (*target, self.controller.task_id, self.controller.last_task_id)
        if context != self._context:
            self._context_revision += 1
            self._context = context
        if target != self._target:
            self._target = target
            self._local_error = ""
            self._initial_requested = False
            self._clear_editor()
        serial, state = target
        self.device_input.setText(f"{serial} · {state or '未知状态'}" if serial else "")
        fallback = "尚未读取属性" if serial and state == "device" else "请在顶部选择在线设备"
        self.status_label.setText(self.controller.status or fallback)
        self._update_editor_chrome()
        if self._active:
            if self._table_snapshot != self.controller.properties:
                self._refresh_table()
            self._update_current_value()
        self._update_enabled()
        self._display_error()

    def _selected_name(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _refresh_table(self, *unused) -> None:
        if not self._active:
            return
        properties = self.controller.properties
        previous = self._table_snapshot or {}
        if self._table_snapshot == properties:
            return
        members_changed = previous.keys() != properties.keys()
        if members_changed:
            self._names = tuple(sorted(properties))
        selected_name = self._selected_name()
        scroll = self.table.verticalScrollBar().value()
        blocked = self.table.blockSignals(True)
        try:
            if members_changed:
                for name, row in sorted(self._rows.items(), key=lambda entry: entry[1], reverse=True):
                    if name not in properties:
                        self.table.removeRow(row)
                        self._search_text.pop(name, None)
            for row, name in enumerate(self._names):
                value = properties[name]
                if name not in previous:
                    self.table.insertRow(row)
                    name_item = QTableWidgetItem(name)
                    name_item.setData(Qt.ItemDataRole.UserRole, name)
                    name_item.setToolTip(name)
                    self.table.setItem(row, 0, name_item)
                    type_item = QTableWidgetItem(_property_type(name))
                    type_item.setToolTip("仅说明前缀惯例，实际权限 / 持久化取决于设备。")
                    self.table.setItem(row, 2, type_item)
                elif name in previous and previous[name] == value:
                    continue
                value_item = self.table.item(row, 1)
                if value_item is None:
                    value_item = QTableWidgetItem()
                    self.table.setItem(row, 1, value_item)
                value_item.setText(value if value else "（空值）")
                value_item.setData(Qt.ItemDataRole.UserRole, value)
                value_item.setToolTip(f"原始值（JSON 转义显示）：\n{_quoted(value)}")
                self._search_text[name] = name.casefold() + "\n" + value.casefold()
            if members_changed:
                self._rows = {name: row for row, name in enumerate(self._names)}
            if selected_name in self._rows:
                self.table.selectRow(self._rows[selected_name])
            self.table.verticalScrollBar().setValue(scroll)
        finally:
            self.table.blockSignals(blocked)
        if properties and not self._column_widths_initialized:
            header = self.table.horizontalHeader()
            for column, maximum in enumerate((300, 240, 120)):
                width = max(header.sectionSizeHint(column), self.table.sizeHintForColumn(column))
                self.table.setColumnWidth(column, min(maximum, max(88, width)))
            self._column_widths_initialized = True
        self._table_snapshot = dict(properties)
        self._apply_filter()

    def _choose_type_filter(self, section: int) -> None:
        if section != 2:
            return
        menu = QMenu(self)
        for index in range(self.type_filter.count()):
            action = menu.addAction(self.type_filter.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.type_filter.currentIndex())
            action.triggered.connect(lambda checked=False, index=index: self.type_filter.setCurrentIndex(index))
        header = self.table.horizontalHeader()
        menu.exec(header.mapToGlobal(QPoint(header.sectionViewportPosition(section), header.height())))
        menu.deleteLater()

    def _apply_filter(self) -> None:
        if not self._active:
            return
        query = self.search.text().casefold()
        selected_type = self.type_filter.currentData()
        visible = 0
        blocked = self.table.blockSignals(True)
        try:
            for name, row in self._rows.items():
                hidden = (query not in self._search_text[name] or
                          bool(selected_type and _property_type(name) != selected_type))
                self.table.setRowHidden(row, hidden)
                visible += not hidden
        finally:
            self.table.blockSignals(blocked)
        self.count_label.setText(f"显示 {visible} / 共 {len(self._rows)} 条")

    def _selection_changed(self) -> None:
        name = self._selected_name()
        if name not in self.controller.properties:
            return
        self._local_error = ""
        self.name_field.setText(name)
        self._set_proposed_value(self.controller.properties[name])
        self._update_current_value()
        self._display_error()

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
        self._clear_editor()
        self._display_error()
        self.name_field.setFocus()

    def _name_changed(self, *unused) -> None:
        self._context_revision += 1
        self._local_error = ""
        self._update_current_value()
        self._update_enabled()
        self._display_error()

    def _set_proposed_value(self, value: str) -> None:
        if value != self._proposed_value:
            self._context_revision += 1
        self._loading_value = True
        try:
            self.value_field.setPlainText(value)
            # QTextDocument normalizes paragraph separators. An untouched value
            # selected from the snapshot must still be submitted literally.
            self._proposed_value = value
        finally:
            self._loading_value = False
        self._update_proposal_note()

    def _proposal_changed(self) -> None:
        if self._loading_value:
            return
        self._proposed_value = self.value_field.toPlainText()
        self._context_revision += 1
        self._local_error = ""
        self._update_proposal_note()
        self._display_error()

    def _update_proposal_note(self) -> None:
        value = self._proposed_value
        if not value:
            text = "拟写入：空字符串（不是删除属性）。"
        elif value.isspace():
            text = f"拟写入：{len(value)} 个空白字符，按原样保留。"
        else:
            text = f"拟写入：{len(value)} 个字符，包含的空格和换行按原样保留。"
        self.value_note.setText(text)

    def _update_current_value(self) -> None:
        name = self.name_field.text()
        properties = self.controller.properties
        if not name:
            value = ""
            placeholder = "选择属性或输入名称后读取"
            note = "当前值来自设备读取的快照；拟写入值不会自动替换它。"
        elif name not in properties:
            value = ""
            placeholder = "当前快照未列出此属性；可点击读取确认"
            note = f"{_property_type(name)} · 未列出与空值不同；输入值后可尝试新增。"
        else:
            value = properties[name]
            placeholder = "（空字符串）" if not value else ""
            if not value:
                detail = "当前实际值为空字符串，属性仍然存在。"
            elif value.isspace():
                detail = f"当前实际值包含 {len(value)} 个空白字符。"
            else:
                detail = f"当前实际值共 {len(value)} 个字符。"
            note = f"{_property_type(name)} · {detail}"
        self.current_value.setPlaceholderText(placeholder)
        if self.current_value.toPlainText() != value:
            self.current_value.setPlainText(value)
        self.current_value.setToolTip(f"原始值（JSON 转义显示）：\n{_quoted(value)}" if name in properties else placeholder)
        self.current_value_note.setText(note)

    def _ready(self) -> bool:
        return bool(self.controller.serial) and self.controller.device_state == "device" and not self.controller.busy

    def _update_enabled(self) -> None:
        ready = self._ready()
        actions_ready = ready and not self._confirmation_open
        self.name_field.setEnabled(ready)
        self.value_field.setEnabled(ready)
        self.refresh_button.setEnabled(actions_ready)
        self.new_button.setEnabled(actions_ready)
        has_name = bool(self.name_field.text())
        self.read_button.setEnabled(actions_ready and has_name)
        self.write_button.setEnabled(actions_ready and has_name)
        self.output_button.setEnabled(bool(self.controller.last_task_id))
        for button in (self.editor_toggle_button, self.editor_popout_button, self.editor_close_button):
            button.setEnabled(not self._confirmation_open)

    def _display_error(self) -> None:
        text = "\n".join(message for message in (self.controller.error, self._local_error) if message)
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.error_scroll.setVisible(bool(text))
        self.editor_error_label.setText(text)
        self.editor_error_label.setVisible(self._editor_floating and bool(text))

    def _set_local_error(self, message: str) -> None:
        self._local_error = message
        self._display_error()

    def _refresh(self) -> None:
        if self._confirmation_open:
            return
        self._local_error = ""
        try:
            self.controller.refresh()
        except ValueError as exc:
            self._set_local_error(str(exc))
        else:
            self._display_error()

    def _read(self) -> None:
        if self._confirmation_open:
            return
        self._local_error = ""
        try:
            self.controller.read(self.name_field.text())
        except ValueError as exc:
            self._set_local_error(str(exc))
        else:
            self._display_error()

    def _write(self) -> None:
        if self._confirmation_open:
            return
        if not self._ready():
            self._set_local_error("当前设备不在线或正在执行属性任务；未写入。")
            return
        serial = self.controller.serial
        state = self.controller.device_state
        name = self.name_field.text()
        value = self._proposed_value
        revision = self._context_revision
        old_value = (
            _quoted(self.controller.properties[name])
            if name in self.controller.properties
            else "（当前快照未列出此属性；不等于空值）"
        )
        confirmation = QMessageBox(self.editor_window if self._editor_floating else self)
        confirmation.setObjectName("propWriteConfirmation")
        confirmation.setWindowTitle("确认写入 Android 属性")
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText(
            f"设备：{_quoted(serial)}\n属性：{_quoted(name)}\n\n"
            f"当前值：{old_value}\n拟写入值：{_quoted(value)}"
        )
        confirmation.setInformativeText(
            "引号内按 JSON 转义显示，空字符串为 \"\"；空格 / 换行会原样提交。\n"
            "ro.* 通常只读，persist.* 可能持久化；实际权限取决于设备。"
            "任何属性都可能影响系统服务。空值不是删除。\n"
            "仅提交上述已捕获的设备、名称和值，写入后读取设备验证。继续？"
        )
        confirmation.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        confirmation.setDefaultButton(QMessageBox.StandardButton.No)
        confirmation.setEscapeButton(QMessageBox.StandardButton.No)
        self._local_error = ""
        self._display_error()
        self._confirmation_open = True
        self._update_enabled()
        try:
            answer = confirmation.exec()
        finally:
            self._confirmation_open = False
            confirmation.deleteLater()
            self._update_enabled()
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Nested modal events may switch away and back, or start and finish a
        # task. Compare a revision as well as the current target/busy state.
        if (
            revision != self._context_revision
            or (serial, state) != (self.controller.serial, self.controller.device_state)
            or self.controller.busy
            or name != self.name_field.text()
            or value != self._proposed_value
        ):
            self._set_local_error("设备或任务状态在确认期间已变化；未写入，请重新确认。")
            return
        try:
            self.controller.write(name, value)
        except ValueError as exc:
            self._set_local_error(str(exc))
        else:
            self._display_error()

    def open_output(self) -> None:
        if self.controller.last_task_id:
            self.show_output.emit(self.controller.last_task_id)

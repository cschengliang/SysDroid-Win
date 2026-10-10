from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from sysdroid.core.backend import OUTPUT_LIMIT, TaskRunner
from sysdroid.core.processes import SAMPLE_INTERVALS, ProcessController, ProcessInfo, app_package
from sysdroid.ui import kit as ui_kit
from sysdroid.ui.tables import TableTools
_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1


@dataclass(frozen=True)
class _Column:
    field: str
    label: str
    minimum: int
    maximum: int
    tooltip: str = ""
    visible: bool = True


_COLUMNS = (
    _Column("pid", "PID", 52, 76),
    _Column("name", "名称", 120, 220, "名称过长时省略显示；悬停查看全文"),
    _Column("uid", "UID", 60, 86),
    _Column("ppid", "PPID", 52, 76),
    _Column("state", "状态", 48, 60),
    _Column("cpu_percent", "CPU %", 68, 86, "进程 ticks 差 / 整机总 ticks 差，不乘核心数"),
    _Column("rss_bytes", "RSS", 82, 108, "驻留内存，不是 PSS"),
    _Column("vss_bytes", "VSS", 82, 112, "虚拟地址空间大小"),
    _Column("threads", "线程数", 60, 80),
    _Column("started_at", "启动时间", 140, 185, "设备启动时间 + 启动 ticks / CLK_TCK，按主机本地时区显示"),
    _Column("elapsed_seconds", "运行时长", 82, 108),
    _Column("command_line", "命令行（ps）", 150, 260, "ps 提供的命令行；悬停查看全文，双击加载 cmdline argv"),
    _Column("swap_bytes", "Swap", 82, 108, "status 的 VmSwap，不是 dumpsys 的 SwapPss"),
    _Column("cpu_seconds", "CPU 时间", 82, 108, "累计用户态 + 内核态 CPU 时间，按设备 CLK_TCK 换算"),
    _Column("priority", "优先级", 60, 80, "/proc/PID/stat 的内核 priority 字段"),
    _Column("nice", "Nice", 52, 72, "/proc/PID/stat 的 nice 字段，可为负数"),
    _Column("processor", "CPU 核", 60, 80, "最近运行的逻辑 CPU 编号，从 0 开始"),
    _Column("start_ticks", "启动 ticks", 90, 132, "内核启动 ticks，和 PID 一起确定进程身份", False),
    _Column("cpu_ticks", "CPU ticks", 90, 132, "累计用户态 + 内核态 CPU ticks，未换算单位", False),
)


def _note(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _bytes(value: int | None) -> str:
    return "—" if value is None else f"{value / 1048576:.2f} MiB"


def _duration(value: float | None) -> str:
    if value is None:
        return "—"
    seconds = int(value)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + f"{hours:02}:{minutes:02}:{seconds:02}"


class _ProcessItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left, right = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        if left is None or right is None:
            if left is None and right is None:
                return False
            table = self.tableWidget()
            descending = table is not None and table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
            return left is None if descending else right is None
        return left < right


class ProcessPage(QWidget):
    show_output = Signal(str)
    log_message = Signal(str)

    def __init__(self, runner: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = ProcessController(runner, self)
        self._active = False
        self._dirty = True
        self._rendered: dict[tuple[int, int | None], ProcessInfo] = {}
        self._rows: dict[tuple[int, int | None], int] = {}
        self._search_text: dict[tuple[int, int | None], str] = {}
        self._target = ("", "")
        self._logged_state: tuple[str, str, str] | None = None
        self._logged_error = ""
        self._local_error = ""
        self._detail_view_key = object()
        self._column_widths_initialized = False
        self._filtered_query: str | None = None
        self._build_ui()
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda: self._filter_timer.start())
        self.controller.changed.connect(self._controller_changed)
        self.controller.action_finished.connect(self._action_finished)
        self._update_controls()

    def _build_ui(self) -> None:
        layout = ui_kit.page_layout(QVBoxLayout(self))
        first = QHBoxLayout()
        self.device_label = _note("请在顶部选择在线设备")
        self.device_label.setObjectName("processDevice")
        first.addWidget(self.device_label, 1)
        self.auto_box = QCheckBox("自动采样")
        self.auto_box.setObjectName("processAutoRefresh")
        self.auto_box.setChecked(True)
        self.auto_box.toggled.connect(self.controller.set_auto_refresh)
        first.addWidget(self.auto_box)
        self.interval_combo = QComboBox()
        self.interval_combo.setObjectName("processInterval")
        self.interval_combo.setToolTip("自动采样间隔；上一次采样未返回时跳过本轮，不会叠加请求。")
        for milliseconds in SAMPLE_INTERVALS:
            self.interval_combo.addItem(f"每 {milliseconds // 1000} 秒", milliseconds)
        self.interval_combo.setCurrentIndex(self.interval_combo.findData(self.controller.interval))
        self.interval_combo.currentIndexChanged.connect(
            lambda _index: self.controller.set_interval(self.interval_combo.currentData()))
        first.addWidget(self.interval_combo)
        self.refresh_button = QPushButton("立即采样")
        self.refresh_button.setObjectName("processRefresh")
        self.refresh_button.clicked.connect(self._refresh)
        first.addWidget(self.refresh_button)
        layout.addLayout(first)
        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("processSearch")
        self.search.setPlaceholderText("搜索名称 / PID / UID / 状态")
        actions.addWidget(self.search, 1)
        self.columns_button = QPushButton("显示列")
        self.columns_button.setObjectName("processColumns")
        self.columns_menu = QMenu(self.columns_button)
        self._column_actions = []
        for column, spec in enumerate(_COLUMNS):
            action = self.columns_menu.addAction(spec.label + ("（固定）" if column == 0 else ""))
            action.setCheckable(True)
            action.setChecked(spec.visible)
            action.setEnabled(column != 0)
            action.setToolTip(spec.tooltip)
            action.toggled.connect(lambda visible, index=column: self.table.setColumnHidden(index, not visible))
            self._column_actions.append(action)
        self.columns_menu.addSeparator()
        self.columns_menu.addAction("显示全部列", lambda: self.set_visible_columns([spec.field for spec in _COLUMNS]))
        self.columns_menu.addAction("恢复默认列", lambda: self.set_visible_columns([spec.field for spec in _COLUMNS if spec.visible]))
        self.columns_menu.addAction("按内容收紧列宽", self._fit_columns)
        self.columns_button.setMenu(self.columns_menu)
        actions.addWidget(self.columns_button)
        self.output_button = QPushButton("最后采样 / 请求输出")
        self.output_button.setObjectName("processOutput")
        self.output_button.clicked.connect(self.open_output)
        actions.addWidget(self.output_button)
        layout.addLayout(actions)
        self.summary_label = _note("尚未采样 · CPU % 使用整机容量，不乘核心数")
        self.summary_label.setObjectName("processSummary")
        ui_kit.set_role(self.summary_label, "hint")
        layout.addWidget(self.summary_label)
        self.status_label = _note()
        self.status_label.setObjectName("processStatus")
        ui_kit.set_role(self.status_label, "hint")
        layout.addWidget(self.status_label)
        self.error_label = _note()
        ui_kit.set_role(self.error_label, "error")
        self.error_scroll = QScrollArea()
        self.error_scroll.setObjectName("processErrors")
        ui_kit.set_role(self.error_scroll, "banner")
        self.error_scroll.setWidgetResizable(True)
        self.error_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.error_scroll.setMinimumHeight(50)
        self.error_scroll.setMaximumHeight(110)
        self.error_scroll.setWidget(self.error_label)
        self.error_scroll.hide()
        layout.addWidget(self.error_scroll)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setObjectName("processTable")
        self.table.setHorizontalHeaderLabels([spec.label for spec in _COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.setMinimumWidth(240)
        self.table.setMinimumHeight(100)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setResizeContentsPrecision(200)
        for column, spec in enumerate(_COLUMNS):
            self.table.setColumnWidth(column, spec.minimum)
            self.table.setColumnHidden(column, not spec.visible)
            self.table.horizontalHeaderItem(column).setToolTip(spec.tooltip)
        self.table.setSortingEnabled(True)
        self.table.sortItems(5, Qt.SortOrder.DescendingOrder)
        # Hidden flags belong to view rows; re-filter after a user re-sort.
        header.sortIndicatorChanged.connect(lambda *_: QTimer.singleShot(0, self._apply_filter))
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._load_details())
        self.table_tools = TableTools(self.table, export_name="processes", menu=self._extend_menu,
                                      refresh=self.refresh_button.click, search=self.search)
        self.splitter.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: ui_kit.device_empty_text(
            self.controller.serial, self.controller.device_state, self.table, self.status_label.text(), "等待首次采样"),
            self.controller.changed)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumWidth(260)
        detail_scroll.setMinimumHeight(150)
        group = QGroupBox("进程详情（按需加载，只读）")
        detail_layout = QVBoxLayout(group)
        self.identity_label = _note("双击进程加载详情；无可靠启动 ticks 时不执行可能误配的 dumpsys。")
        detail_layout.addWidget(self.identity_label)
        tabs = QTabWidget()
        tabs.setObjectName("processDetailTabs")
        self.detail_text = QPlainTextEdit()
        self.detail_text.setObjectName("processDetails")
        self.detail_text.setReadOnly(True)
        self.raw_text = QPlainTextEdit()
        self.raw_text.setObjectName("processDetailsRaw")
        self.raw_text.setReadOnly(True)
        for edit in (self.detail_text, self.raw_text):
            edit.setMinimumHeight(edit.fontMetrics().lineSpacing() * 3 + 16)
            edit.document().setMaximumBlockCount(5000)
        tabs.addTab(self.detail_text, "身份 / 内存 / 启动记录")
        tabs.addTab(self.raw_text, "详情原文")
        detail_layout.addWidget(tabs, 1)
        self.details_output_button = QPushButton("详情完整任务输出")
        self.details_output_button.clicked.connect(self._open_details_output)
        detail_layout.addWidget(self.details_output_button)
        detail_layout.addWidget(ui_kit.info_note("右键进程可结束进程 / 强行停止应用（均需确认）；RSS 与 PSS 分开展示。", "RSS 与 PSS / SwapPss 分别展示。AM 启动字段只取同 PID + 数值 UID 的真实记录；未提供时不推断启动来源。结束进程前会在设备上复核 PID + 启动 ticks，PID 已被复用时不发送信号；不提权，shell 用户只能结束自身进程。"))
        detail_scroll.setWidget(group)
        self.splitter.addWidget(detail_scroll)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([800, 320])
        layout.addWidget(self.splitter, 1)

    def visible_columns(self) -> list[str]:
        return [spec.field for column, spec in enumerate(_COLUMNS) if not self.table.isColumnHidden(column)]

    def set_visible_columns(self, fields: list[str]) -> None:
        visible = set(fields) | {"pid"}
        for column, spec in enumerate(_COLUMNS):
            shown = spec.field in visible
            self.table.setColumnHidden(column, not shown)
            self._column_actions[column].setChecked(shown)

    def _fit_columns(self) -> None:
        header = self.table.horizontalHeader()
        for column, spec in enumerate(_COLUMNS):
            width = max(header.sectionSizeHint(column), self.table.sizeHintForColumn(column), spec.minimum)
            self.table.setColumnWidth(column, min(width, spec.maximum))
        self._column_widths_initialized = self.table.rowCount() > 0

    def set_device(self, serial: str, state: str = "device") -> None:
        self.controller.set_device(serial, state)

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self.controller.set_active(active)
        if self._active and self._dirty:
            self._render()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([max(300, self.height() - 280), 240] if orientation == Qt.Orientation.Vertical else [800, 320])

    def _controller_changed(self) -> None:
        self._dirty = True
        self._log_transitions()
        if self._active:
            self._render()

    def _log_transitions(self) -> None:
        controller = self.controller
        target = (controller.serial, controller.device_state)
        if target != self._target:
            self._target = target
            self._logged_error = ""
            self._local_error = ""
        if not controller.active:
            phase = "隐藏，采样已暂停"
        elif not controller.serial or controller.device_state != "device":
            phase = "设备不在线，采样已暂停"
        elif not controller.auto_refresh:
            phase = "自动采样已暂停"
        else:
            phase = f"每 {controller.interval // 1000} 秒采样已开启"
        state = (*target, phase)
        if state != self._logged_state:
            if controller.active or self._logged_state is not None:
                self.log_message.emit(f"INFO 进程：{phase}" + (f" [{controller.serial}]" if controller.serial else ""))
            self._logged_state = state
        # Ignore changing nonce-bearing stdout, but distinguish real device /
        # permission diagnostics rather than just the common exit-code header.
        error = controller.error.split("\nstdout：", 1)[0]
        if error and error != self._logged_error:
            self.log_message.emit(f"ERROR 进程：{error}")
        elif not error and self._logged_error:
            self.log_message.emit("INFO 进程：请求已恢复")
        self._logged_error = error

    def _render(self) -> None:
        controller = self.controller
        self.device_label.setText(controller.serial or "请在顶部选择在线设备")
        self.device_label.setToolTip(f"{controller.serial}\n{controller.device_state}")
        self.status_label.setText(controller.status)
        error = "\n".join(value for value in (self._local_error, controller.error) if value)
        self.error_label.setText(error)
        self.error_scroll.setVisible(bool(error))
        sample = controller.sample
        if sample is None:
            summary = "尚未采样 · CPU % 使用整机容量，不乘核心数"
        else:
            timestamp = datetime.fromtimestamp(controller.sampled_at).strftime("%H:%M:%S") if controller.sampled_at is not None else "—"
            cpu = "—" if controller.total_cpu_percent is None else f"{controller.total_cpu_percent:.1f}%"
            summary = f"采样 {timestamp}（主机时间） · {len(sample.processes)} 个进程 · 整机 CPU {cpu} · MemAvailable {_bytes(sample.mem_available_bytes)} / 总内存 {_bytes(sample.mem_total_bytes)}"
            if sample.warnings:
                summary += "\n" + "；".join(sample.warnings)
        self.summary_label.setText(summary)
        self.summary_label.setToolTip(summary)
        # Rows are updated in place; filtering only re-runs when rows or the
        # query changed, not on every identical tick.
        if self._render_table() or self._filtered_query != self.search.text().casefold():
            self._apply_filter()
        self._render_details()
        self._update_controls()
        self._dirty = False

    def _selected_identity(self) -> tuple[int, int | None] | None:
        item = self.table.item(self.table.currentRow(), 0)
        return tuple(item.data(_ID_ROLE)) if item is not None else None

    def _index_rows(self) -> None:
        self._rows = {tuple(self.table.item(row, 0).data(_ID_ROLE)): row for row in range(self.table.rowCount())}

    def _render_table(self) -> bool:
        processes = self.controller.sample.processes if self.controller.sample else {}
        incoming = {(info.pid, info.start_ticks): info for info in processes.values()}
        if incoming == self._rendered:
            return False
        selected = self._selected_identity()
        vertical, horizontal = self.table.verticalScrollBar().value(), self.table.horizontalScrollBar().value()
        sorting = self.table.isSortingEnabled()
        blocked = self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self.table.setSortingEnabled(False)
        try:
            self._index_rows()
            for key, row in sorted(self._rows.items(), key=lambda item: item[1], reverse=True):
                if key not in incoming:
                    self.table.removeRow(row)
                    self._search_text.pop(key, None)
            self._index_rows()
            for key, info in incoming.items():
                if key not in self._rows:
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self._rows[key] = row
                    for column, spec in enumerate(_COLUMNS):
                        item = _ProcessItem()
                        item.setData(_ID_ROLE, key)
                        if spec.field not in {"name", "state", "started_at", "command_line"}:
                            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        self.table.setItem(row, column, item)
                if self._rendered.get(key) == info:
                    continue
                row = self._rows[key]
                for column, spec in enumerate(_COLUMNS):
                    field = spec.field
                    value = getattr(info, field)
                    if field in {"rss_bytes", "vss_bytes", "swap_bytes"}:
                        text = _bytes(value)
                    elif field == "cpu_percent":
                        text = "—" if value is None else f"{value:.1f}%"
                    elif field == "started_at":
                        text = "—" if value is None else datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
                    elif field in {"elapsed_seconds", "cpu_seconds"}:
                        text = _duration(value)
                    else:
                        text = "—" if value is None else str(value)
                    tooltip = info.unavailable.get(field, str(value)) if value is None else str(value)
                    if field == "cpu_percent" and value is not None:
                        tooltip = f"{value:.6g}%（进程 ticks 差 / 整机总 ticks 差，不乘核心数）"
                    elif field == "started_at" and value is not None:
                        tooltip = text
                    elif field in {"rss_bytes", "vss_bytes", "swap_bytes"} and value is not None:
                        tooltip = f"{value} 字节 · {spec.tooltip}"
                    elif field in {"elapsed_seconds", "cpu_seconds"} and value is not None:
                        tooltip = f"{value:.6g} 秒"
                    sort_value = value.casefold() if isinstance(value, str) else value
                    item = self.table.item(row, column)
                    if item.text() != text:
                        item.setText(text)
                    if item.data(Qt.ItemDataRole.UserRole) != sort_value:
                        item.setData(Qt.ItemDataRole.UserRole, sort_value)
                    if item.toolTip() != tooltip:
                        item.setToolTip(tooltip)
                self._search_text[key] = " ".join(str(value) if value is not None else "" for value in (
                    info.pid, info.uid, info.name, info.state, info.command_line)).casefold()
            self._rendered = incoming
            self.table.setSortingEnabled(sorting)
            self._index_rows()
            if selected in self._rows:
                self.table.setCurrentCell(self._rows[selected], 0)
            elif selected is not None:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
            self.table.verticalScrollBar().setValue(vertical)
            self.table.horizontalScrollBar().setValue(horizontal)
        finally:
            self.table.setSortingEnabled(sorting)
            self.table.blockSignals(blocked)
            self.table.setUpdatesEnabled(True)
        if incoming and not self._column_widths_initialized:
            self._fit_columns()
        return True

    def _apply_filter(self) -> None:
        if not self._active:
            self._dirty = True
            return
        query = self.search.text().casefold()
        self._filtered_query = query
        self._index_rows()
        for key, row in self._rows.items():
            hidden = query not in self._search_text.get(key, "")
            if self.table.isRowHidden(row) != hidden:
                self.table.setRowHidden(row, hidden)

    def _selection_changed(self) -> None:
        self._local_error = ""
        self._render_details()
        self._update_controls()

    def _render_details(self) -> None:
        identity = self._selected_identity()
        info = self._rendered.get(identity)
        details = self.controller.details
        if info is None:
            key = None
            text, raw = "双击进程加载详情。", ""
            identity_text = "没有选中的进程"
        else:
            matching = details is not None and (details.pid, details.start_ticks) == identity
            key = (identity, info, details if matching else None)
            identity_text = f"PID {info.pid} · UID {info.uid if info.uid is not None else '—'} · 启动 ticks {info.start_ticks if info.start_ticks is not None else '—'}"
            if not matching:
                text = f"ps 名称：{info.name or '—'}\nps 命令：{info.command_line or '设备未提供'}\n"
                text += "身份不可确认；不能加载 dumpsys 详情。" if info.start_ticks is None else "双击该行读取 cmdline / status / dumpsys。"
                raw = ""
            else:
                argv = "不可读取（与空 argv 不同）" if details.argv is None else json.dumps(details.argv, ensure_ascii=False)
                text = f"cmdline argv（JSON 转义）：{argv}\nPSS：{_bytes(details.pss_bytes)}\nSwapPss：{_bytes(details.swap_pss_bytes)}\n"
                text += f"采样 RSS（不是 PSS）：{_bytes(info.rss_bytes)}\n"
                text += f"AM 启动序号：{details.start_sequence if details.start_sequence is not None else '设备未提供'}\n"
                text += f"AM 启动原因：{details.start_reason or '设备未提供'}\nAM hostingType：{details.hosting_type or '设备未提供'}\n"
                text += "\n" + "\n".join(f"{field}：{reason}" for field, reason in details.unavailable.items())
                raw = "\n\n".join(f"[{section}]\n{value}" for section, value in details.sections.items())
        if key != self._detail_view_key:
            self.identity_label.setText(identity_text)
            self.detail_text.setPlainText(text[-OUTPUT_LIMIT:])
            self.raw_text.setPlainText(raw[-OUTPUT_LIMIT:])
            self._detail_view_key = key

    def _update_controls(self) -> None:
        controller = self.controller
        identity = self._selected_identity()
        self.output_button.setEnabled(bool(controller.last_task_id))
        self.refresh_button.setEnabled(bool(controller.serial) and controller.device_state == "device" and not controller.busy)
        matching = controller.details is not None and identity == (controller.details.pid, controller.details.start_ticks)
        self.details_output_button.setEnabled(matching and bool(controller.details_task_id))

    def interval(self) -> int:
        return self.controller.interval

    def set_interval(self, milliseconds: int) -> None:
        index = self.interval_combo.findData(milliseconds)
        if index >= 0:
            self.interval_combo.setCurrentIndex(index)

    def _refresh(self) -> None:
        self._local_error = ""
        try:
            self.controller.refresh()
        except ValueError as exc:
            self._local_error = str(exc)
            self._render()

    def _load_details(self) -> None:
        identity = self._selected_identity()
        if identity is None or identity[1] is None:
            return
        self._local_error = ""
        try:
            # Queued behind an in-flight sample instead of being dropped.
            self.controller.load_details(*identity)
        except ValueError as exc:
            self._local_error = str(exc)
            self._render()

    def _extend_menu(self, menu: QMenu, row: int) -> None:
        if self.table.currentRow() != row:
            self.table.setCurrentCell(row, 0)
        identity = self._selected_identity()
        info = self._rendered.get(identity)
        if info is None:
            return
        reliable = info.start_ticks is not None
        online = bool(self.controller.serial) and self.controller.device_state == "device"
        details = menu.addAction("读取详情", self._load_details)
        details.setEnabled(reliable and online)
        menu.addSeparator()
        menu.addAction("复制 PID", lambda: self._copy(str(info.pid)))
        if info.name:
            menu.addAction("复制名称", lambda: self._copy(info.name))
        if info.command_line:
            menu.addAction("复制命令行", lambda: self._copy(info.command_line))
        package = app_package(info)
        if package:
            menu.addAction("复制包名", lambda: self._copy(package))
        menu.addSeparator()
        for title, force in (("结束进程（SIGTERM）…", False), ("强制结束进程（SIGKILL）…", True)):
            action = menu.addAction(title, lambda force=force: self.kill_selected(force=force))
            action.setEnabled(reliable and online)
            if not reliable:
                action.setToolTip("没有可靠启动 ticks，不能确认进程身份")
        if package:
            menu.addAction(f"强行停止应用 {package}…", self.force_stop_selected).setEnabled(online)

    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def kill_selected(self, *, force: bool = False) -> None:
        identity = self._selected_identity()
        info = self._rendered.get(identity)
        if info is None or info.start_ticks is None:
            return
        serial = self.controller.serial
        signal = "SIGKILL（不可被进程捕获）" if force else "SIGTERM"
        if not self._confirm("确认结束进程", f"设备：{serial}\nPID：{info.pid}\n名称：{info.name or '—'}\n"
                             f"UID：{info.uid if info.uid is not None else '—'}\n\n发送 {signal}？"
                             "\n发送前会在设备上复核启动 ticks；PID 已被复用时不发送。"):
            return
        if self.controller.serial != serial:
            self._local_error = "确认期间设备已变化；未发送信号。"
            self._render()
            return
        self._local_error = ""
        try:
            self.controller.kill_process(info.pid, info.start_ticks, force=force)
        except ValueError as exc:
            self._local_error = str(exc)
        self._render()

    def force_stop_selected(self) -> None:
        identity = self._selected_identity()
        info = self._rendered.get(identity)
        package = app_package(info) if info is not None else None
        if package is None:
            return
        serial, user_id = self.controller.serial, info.uid // 100000
        if not self._confirm("确认强行停止应用", f"设备：{serial}\n用户：{user_id}\n包：{package}\n\n"
                             "am force-stop 会结束该应用的全部进程并取消其闹钟 / 任务。继续？"):
            return
        if self.controller.serial != serial:
            self._local_error = "确认期间设备已变化；未提交。"
            self._render()
            return
        self._local_error = ""
        try:
            self.controller.force_stop(package, user_id)
        except ValueError as exc:
            self._local_error = str(exc)
        self._render()

    def _action_finished(self, message: str, ok: bool) -> None:
        self.log_message.emit(("INFO 进程：" if ok else "ERROR 进程：") + message)
        self._local_error = "" if ok else message
        self._dirty = True
        if self._active:
            self._render()

    def open_output(self) -> None:
        if self.controller.last_task_id:
            self.show_output.emit(self.controller.last_task_id)

    def _open_details_output(self) -> None:
        if self.details_output_button.isEnabled():
            self.show_output.emit(self.controller.details_task_id)

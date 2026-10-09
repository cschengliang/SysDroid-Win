from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from sysdroid.core.apks import PackageController
from sysdroid.core.backend import TaskRunner
from sysdroid.core.users import AndroidUserController
from sysdroid.ui import kit as ui_kit
class NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        left, right = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        if left is None:
            return right is not None and self.tableWidget().horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        if right is None:
            return self.tableWidget().horizontalHeader().sortIndicatorOrder() != Qt.SortOrder.DescendingOrder
        return left < right


class ApkPage(QWidget):
    def __init__(self, runner: TaskRunner, users: AndroidUserController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = PackageController(runner, self)
        self.users = users
        self._active = False
        self._dirty = True
        self._revision = 0
        self._confirming = False
        self._user_context = None
        self._refresh_pending = False
        self._rendered = {}
        self._filter_cache = {}
        self._row_by_package = {}
        self._details_rendered = None
        self._local_error = ""
        self._task_state = (False, "")
        layout = ui_kit.page_layout(QVBoxLayout(self))
        first = QHBoxLayout()
        first.addWidget(QLabel("用户"))
        self.user_combo = QComboBox()
        first.addWidget(self.user_combo)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索包名 / UID / 路径 / 已读版本及来源")
        first.addWidget(self.search, 1)
        self.type_filter = QComboBox()
        self.type_filter.addItems(["全部类型", "系统", "第三方"])
        self.state_filter = QComboBox()
        self.state_filter.addItems(["全部状态", "启用", "禁用"])
        first.addWidget(self.type_filter)
        first.addWidget(self.state_filter)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setToolTip("刷新 Android 用户及当前用户的包列表。")
        self.refresh_button.clicked.connect(self._refresh)
        first.addWidget(self.refresh_button)
        self.export_button = QPushButton("导出 APK…")
        self.export_button.clicked.connect(self._export)
        first.addWidget(self.export_button)
        layout.addLayout(first)
        self.splitter = QSplitter()
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("apkPackageTable")
        self.table.setHorizontalHeaderLabels(["包名", "UID", "版本码", "类型", "状态", "安装来源", "APK 路径"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setToolTip("双击包名查看基本信息、权限与组件、原始输出。")
        self.table.setSortingEnabled(True)
        for column, width in enumerate((225, 90, 100, 90, 75, 150, 240)):
            self.table.setColumnWidth(column, width)
        self.splitter.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: ui_kit.device_empty_text(
            self.controller.serial, self.controller.device_state, self.table, self.status.text(), "暂无应用包 · 选择用户后点击「刷新」"),
            self.controller.changed)
        self.tabs = QTabWidget()
        self.basic = QTextEdit()
        self.permissions = QTextEdit()
        self.raw = QTextEdit()
        for title, editor in (("基本信息", self.basic), ("权限与组件", self.permissions), ("原始输出", self.raw)):
            editor.setReadOnly(True)
            self.tabs.addTab(editor, title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tabs)
        self.splitter.addWidget(scroll)
        self.splitter.setSizes([700, 320])
        layout.addWidget(self.splitter, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        error_scroll = QScrollArea()
        ui_kit.set_role(error_scroll, "statusBox")
        error_scroll.setWidgetResizable(True)
        error_scroll.setMinimumHeight(50)
        error_scroll.setMaximumHeight(110)
        self.error = ui_kit.set_role(QLabel(), "error")
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        messages = QWidget()
        messages.setObjectName("statusBoxContent")
        messages_layout = QVBoxLayout(messages)
        messages_layout.setContentsMargins(6, 4, 6, 4)
        messages_layout.addWidget(self.status)
        messages_layout.addWidget(self.error)
        error_scroll.setWidget(messages)
        layout.addWidget(error_scroll)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._filter)
        self.search.textChanged.connect(lambda: self._debounce.start())
        self.type_filter.currentIndexChanged.connect(self._filter)
        self.state_filter.currentIndexChanged.connect(self._filter)
        self.user_combo.currentIndexChanged.connect(self._user_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _: self._details())
        self.controller.changed.connect(self._changed)
        users.changed.connect(self._users_changed)
        self._changed()

    def _revise(self, *_):
        self._revision += 1

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) == (self.controller.serial, self.controller.device_state):
            return
        self._revise()
        self._user_context = None
        self._refresh_pending = False
        self.controller.set_device(serial, state)
        self.controller.user_id = None
        self._users_changed()
        if self._active:
            self.users.ensure_loaded()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._revise()
        self._active = active
        if active:
            self.users.ensure_loaded()
            self._users_changed()
            self._changed()

    def _users_changed(self) -> None:
        if (self.users.serial, self.users.device_state) != (self.controller.serial, self.controller.device_state):
            return
        context = (self.users.serial, self.users.device_state)
        current = self.user_combo.currentData() if context == self._user_context else self.users.current_user
        self._user_context = context
        self.user_combo.blockSignals(True)
        self.user_combo.clear()
        for user, name in self.users.users.items():
            self.user_combo.addItem(f"{user} — {name}", user)
        selected = self.user_combo.findData(current)
        self.user_combo.setCurrentIndex(selected if selected >= 0 else self.user_combo.findData(self.users.current_user))
        self.user_combo.blockSignals(False)
        self._user_changed()

    def _user_changed(self, *_):
        user = self.user_combo.currentData()
        if user is None:
            if self.controller.user_id is not None:
                self.controller.user_id = None
                self.controller._reset()
        elif user != self.controller.user_id:
            self._revise()
            self.controller.set_user(user)
        refresh_requested = self._refresh_pending and not self.users.busy
        if refresh_requested:
            self._refresh_pending = False
        if ((self._active or refresh_requested) and user is not None and
                not self.users.busy and not self.controller.busy and
                (refresh_requested or not self.controller.status)):
            self._call(self.controller.refresh)
        self._changed()

    def _call(self, callback):
        self._local_error = ""
        try:
            callback()
        except (ValueError, OSError) as exc:
            self._local_error = str(exc)
        self._changed()

    def _refresh(self):
        if self._confirming or self.controller.busy or self.users.busy:
            return
        self._refresh_pending = True
        self._revise()
        self._call(self.users.refresh)

    def _selected(self) -> str:
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 and self.table.item(row, 0) else ""

    def _selection_changed(self):
        self._revise()
        self._details_rendered = None
        self._changed()

    def _details(self):
        if self._selected():
            self._call(lambda: self.controller.load_details(self._selected()))

    def _capture(self):
        return (self._revision, self.controller.serial, self.controller.device_state, self.controller.user_id,
                self._selected())

    def _confirm(self, title: str, text: str, callback):
        if self._confirming or self.controller.busy or self.users.busy or self.controller.user_id is None:
            return
        captured = self._capture()
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"设备：{captured[1]}\n用户：{captured[3]}\n包：{captured[4]}\n\n{text}")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setEscapeButton(QMessageBox.StandardButton.No)
        self._confirming = True
        self._changed()
        try:
            answer = box.exec()
        finally:
            self._confirming = False
            box.deleteLater()
        if answer == QMessageBox.StandardButton.Yes:
            if captured != self._capture() or self.controller.busy:
                self._local_error = "确认期间上下文已变化；未提交。"
            else:
                self._call(callback)
        self._changed()

    def _export(self):
        package, captured = self._selected(), self._capture()
        if not package:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if directory and captured == self._capture():
            self._confirm("确认导出 APK", f"导出 base 和全部 split 到独立目录：\n{directory}",
                lambda: self.controller.export(package, Path(directory)))

    def _changed(self):
        task_state = (self.controller.busy, self.controller.task_id)
        if task_state != self._task_state:
            self._task_state = task_state
            self._revise()
        self._dirty = True
        if not self._active:
            return
        self.status.setText(self.controller.status or "选择用户后按需读取包信息")
        self.error.setText(self._local_error or self.controller.error or self.users.error)
        online = bool(self.controller.serial and self.controller.device_state == "device")
        idle = not self.controller.busy and not self.users.busy and not self._confirming
        self.refresh_button.setEnabled(online and idle)
        supported = "path" not in self.controller.unsupported_actions
        self.export_button.setEnabled(online and idle and self.controller.user_id is not None and
                                      self._selected() in self.controller.packages and supported)
        self.export_button.setToolTip("导出所选应用的 base 和全部 split APK。" if supported else
                                      "设备命令不支持 --user；不会切换到其他用户执行。")
        self.user_combo.setEnabled(not self._confirming and not self.users.busy)
        self._render()
        self._render_details()
        self._dirty = False

    def _render(self):
        packages = self.controller.packages
        if packages == self._rendered:
            self._filter()
            return
        selected = self._selected()
        scroll = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        rows = {self.table.item(row, 0).text(): row for row in range(self.table.rowCount())}
        for name, row in sorted(rows.items(), key=lambda pair: pair[1], reverse=True):
            if name not in packages:
                self.table.removeRow(row)
        rows = {self.table.item(row, 0).text(): row for row in range(self.table.rowCount())}
        for name, value in packages.items():
            row = rows.get(name)
            if row is None:
                row = self.table.rowCount()
                self.table.insertRow(row)
            elif self._rendered.get(name) == value:
                continue
            texts = [name, value.uid, value.version_code,
                "系统" if value.system else "第三方" if value.system is not None else None,
                "启用" if value.enabled else "禁用" if value.enabled is not None else None,
                value.installer, value.apk_path]
            for column, text in enumerate(texts):
                item = self.table.item(row, column)
                if item is None:
                    item = NumericItem() if column in (1, 2) else QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText("—" if text is None else str(text))
                item.setToolTip("设备未提供/未识别" if text is None else str(text))
                if column in (1, 2):
                    item.setData(Qt.ItemDataRole.UserRole, text)
        self.table.setSortingEnabled(True)
        self._row_by_package = {self.table.item(row, 0).text(): row for row in range(self.table.rowCount())}
        if selected in self._row_by_package:
            self.table.selectRow(self._row_by_package[selected])
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)
        self._rendered = dict(packages)
        self._filter()

    def _filter(self, *_):
        if not self._active:
            return
        query = self.search.text().casefold()
        kind, state = self.type_filter.currentIndex(), self.state_filter.currentIndex()
        for name in tuple(self._filter_cache):
            if name not in self.controller.packages:
                del self._filter_cache[name]
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text()
            summary = self.controller.packages.get(name)
            if summary is None:
                continue
            detail = self.controller.details.get(name)
            cached = self._filter_cache.get(name)
            if cached is None or cached[0] != summary or cached[1] is not detail:
                text = " ".join(str(v) for v in asdict(summary).values())
                if detail:
                    text += f" {detail.version_name} {detail.installer}"
                cached = (summary, detail, text.casefold())
                self._filter_cache[name] = cached
            visible = query in cached[2] and (kind == 0 or summary.system == (kind == 1)) and (state == 0 or summary.enabled == (state == 1))
            self.table.setRowHidden(row, not visible)

    def _render_details(self):
        name = self._selected()
        detail = self.controller.details.get(name)
        key = (name, detail)
        if key == self._details_rendered:
            return
        self._details_rendered = key
        if detail is None:
            self.basic.setPlainText("双击包名加载详情；未知字段不等于 0 / False。")
            self.permissions.clear()
            self.raw.clear()
            return
        fields = asdict(detail)
        for excluded in ("raw", "permissions", "components"):
            fields.pop(excluded)
        for flag in ("system", "updated_system", "debuggable", "persistent"):
            fields[flag] = getattr(detail, flag)
        text = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False) if value is not None else '设备未提供/未识别'}" for key, value in fields.items())
        if detail.uid is None and detail.app_id is not None:
            text += f"\nappId（非实测 UID）: {detail.app_id}\n推导 UID（非实测）: {detail.user_id * 100000 + detail.app_id}"
        text += "\nflags 包括 SYSTEM / UPDATED_SYSTEM_APP / DEBUGGABLE / PERSISTENT 时才表示相应标志。\n内部 signatures 不是证书 SHA-256。"
        self.basic.setPlainText(text)
        self.permissions.setPlainText("\n\n".join(section for section in (detail.permissions, detail.components) if section) or "设备未提供/未识别")
        self.raw.setPlainText(detail.raw)

    def resizeEvent(self, event):
        self.splitter.setOrientation(Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal)
        super().resizeEvent(event)

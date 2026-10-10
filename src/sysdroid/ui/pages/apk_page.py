from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from sysdroid.core.apks import PackageController, PackageDetails
from sysdroid.core.backend import TaskRunner
from sysdroid.core.users import AndroidUserController
from sysdroid.ui import kit as ui_kit
from sysdroid.ui.tables import TableTools
class NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        left, right = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        if left is None:
            return right is not None and self.tableWidget().horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        if right is None:
            return self.tableWidget().horizontalHeader().sortIndicatorOrder() != Qt.SortOrder.DescendingOrder
        return left < right


def _yes_no(value: bool | None) -> str | None:
    return None if value is None else ("是" if value else "否")


_ENABLED_STATES = {0: "默认（0）", 1: "已启用（1）", 2: "已禁用（2）", 3: "用户禁用（3）", 4: "禁用直到使用（4）"}


def detail_rows(detail: PackageDetails) -> list[tuple[str, str | None, str]]:
    """(label, value or None when unknown, tooltip) rows for the basic-info table."""
    rows: list[tuple[str, str | None, str]] = [
        ("包名", detail.package, ""),
        ("用户", str(detail.user_id), ""),
        ("版本名", detail.version_name, ""),
        ("版本码", None if detail.version_code is None else str(detail.version_code), ""),
        ("UID", None if detail.uid is None else str(detail.uid), "dumpsys 的实测 uid 字段"),
        ("appId", None if detail.app_id is None else str(detail.app_id), "userId / appId 字段，不等于实测 UID"),
    ]
    if detail.uid is None and detail.app_id is not None:
        rows.append(("推导 UID（非实测）", str(detail.user_id * 100000 + detail.app_id), "用户 ID × 100000 + appId"))
    rows += [
        ("minSdk", None if detail.min_sdk is None else str(detail.min_sdk), ""),
        ("targetSdk", None if detail.target_sdk is None else str(detail.target_sdk), ""),
        ("系统应用", _yes_no(detail.system), "flags 含 SYSTEM"),
        ("已更新的系统应用", _yes_no(detail.updated_system), "flags 含 UPDATED_SYSTEM_APP"),
        ("可调试", _yes_no(detail.debuggable), "flags 含 DEBUGGABLE"),
        ("常驻", _yes_no(detail.persistent), "flags 含 PERSISTENT"),
        ("已安装（该用户）", _yes_no(detail.installed), ""),
        ("启用状态", None if detail.enabled is None else _ENABLED_STATES.get(detail.enabled, str(detail.enabled)), "User N: enabled= 字段"),
        ("已停止", _yes_no(detail.stopped), ""),
        ("已隐藏", _yes_no(detail.hidden), ""),
        ("已暂停", _yes_no(detail.suspended), ""),
        ("主 ABI", detail.primary_abi, ""),
        ("次 ABI", detail.secondary_abi, ""),
        ("安装来源", detail.installer, ""),
        ("首次安装", detail.first_install_time, ""),
        ("最近更新", detail.last_update_time, ""),
        ("codePath", detail.code_path, ""),
        ("dataDir", detail.data_dir, ""),
        ("flags", None if detail.flags is None else " ".join(detail.flags), ""),
    ]
    for index, path in enumerate(detail.paths):
        rows.append(("APK 路径" if index == 0 else f"split {index}", path, ""))
    return rows


class InstallDialog(QDialog):
    def __init__(self, files: list[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("安装 APK")
        layout = QVBoxLayout(self)
        multiple = len(files) > 1
        layout.addWidget(QLabel("将作为同一应用的 base + split 一次安装（install-multiple）：" if multiple else "将安装："))
        listing = QPlainTextEdit("\n".join(str(path) for path in files))
        listing.setReadOnly(True)
        listing.setMaximumHeight(110)
        layout.addWidget(listing)
        self.replace_box = QCheckBox("允许替换已安装的同包应用（-r，保留数据）")
        self.test_box = QCheckBox("允许安装测试包（-t）")
        layout.addWidget(self.replace_box)
        layout.addWidget(self.test_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("安装")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ApkPage(QWidget):
    def __init__(self, runner: TaskRunner, users: AndroidUserController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
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
        self.install_button = QPushButton("安装 APK…")
        self.install_button.setObjectName("apkInstall")
        self.install_button.setToolTip("选择一个 APK，或同一应用的 base + split 多个 APK；也可把 APK 拖入本页。")
        self.install_button.clicked.connect(self._pick_install)
        first.addWidget(self.install_button)
        self.export_button = QPushButton("导出 APK…")
        self.export_button.clicked.connect(self._export)
        first.addWidget(self.export_button)
        layout.addLayout(first)
        progress_row = QHBoxLayout()
        self.install_progress = QProgressBar()
        self.install_progress.setObjectName("apkInstallProgress")
        self.install_progress.setTextVisible(True)
        self.install_label = QLabel()
        self.install_label.setObjectName("apkInstallOutput")
        self.install_label.setTextFormat(Qt.TextFormat.PlainText)
        self.install_cancel = QPushButton("取消安装")
        self.install_cancel.clicked.connect(self._cancel_install)
        progress_row.addWidget(self.install_progress, 1)
        progress_row.addWidget(self.install_label, 2)
        progress_row.addWidget(self.install_cancel)
        self.install_row = QWidget()
        self.install_row.setLayout(progress_row)
        progress_row.setContentsMargins(0, 0, 0, 0)
        self.install_row.hide()
        layout.addWidget(self.install_row)
        self.setAcceptDrops(True)
        self.splitter = QSplitter()
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("apkPackageTable")
        self.table.setHorizontalHeaderLabels(["包名", "UID", "版本码", "类型", "状态", "安装来源", "APK 路径"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setToolTip("双击包名查看详情；右键可启动、停止、启用 / 禁用、卸载、复制包名。")
        self.table.setSortingEnabled(True)
        for column, width in enumerate((225, 90, 100, 90, 75, 150, 240)):
            self.table.setColumnWidth(column, width)
        self.table_tools = TableTools(self.table, export_name="packages", menu=self._extend_menu,
                                      refresh=self.refresh_button.click, search=self.search)
        self.splitter.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: ui_kit.device_empty_text(
            self.controller.serial, self.controller.device_state, self.table, self.status.text(), "暂无应用包 · 选择用户后点击「刷新」"),
            self.controller.changed)
        self.tabs = QTabWidget()
        basic_page = QWidget()
        basic_layout = QVBoxLayout(basic_page)
        basic_layout.setContentsMargins(0, 0, 0, 0)
        self.basic_hint = QLabel()
        self.basic_hint.setWordWrap(True)
        ui_kit.set_role(self.basic_hint, "hint")
        self.basic = QTableWidget(0, 2)
        self.basic.setObjectName("apkBasicInfo")
        self.basic.setHorizontalHeaderLabels(["字段", "值"])
        self.basic.verticalHeader().hide()
        self.basic.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.basic.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.basic.setWordWrap(False)
        self.basic.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.basic.horizontalHeader().setStretchLastSection(True)
        basic_layout.addWidget(self.basic, 1)
        basic_layout.addWidget(self.basic_hint)
        self.tabs.addTab(basic_page, "基本信息")
        self.permissions = QTextEdit()
        self.raw = QTextEdit()
        for title, editor in (("权限与组件", self.permissions), ("原始输出", self.raw)):
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
        self.controller.install_progress.connect(self._install_progress)
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
        self.install_button.setEnabled(online and idle and self.controller.user_id is not None)
        installing = self.controller.busy and self.controller.status == "安装 APK"
        if installing and not self.install_row.isVisible():
            self.install_progress.setRange(0, 0)
            self.install_label.setText("正在通过 adb 传输并安装…")
        self.install_row.setVisible(installing)
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
            self.basic.setRowCount(0)
            self.basic_hint.setText("双击包名加载详情；未知字段不等于 0 / 否。")
            self.permissions.clear()
            self.raw.clear()
            return
        rows = detail_rows(detail)
        self.basic.setRowCount(len(rows))
        for row, (label, value, tooltip) in enumerate(rows):
            key = QTableWidgetItem(label)
            key.setToolTip(tooltip or label)
            item = QTableWidgetItem("设备未提供/未识别" if value is None else value)
            item.setToolTip("设备未提供/未识别" if value is None else value)
            if value is None:
                item.setForeground(self.palette().placeholderText())
            self.basic.setItem(row, 0, key)
            self.basic.setItem(row, 1, item)
        self.basic_hint.setText("「是 / 否」来自 flags 与用户区段；未提供的字段不等于否。内部 signatures 不是证书 SHA-256。")
        self.permissions.setPlainText("\n\n".join(section for section in (detail.permissions, detail.components) if section) or "设备未提供/未识别")
        self.raw.setPlainText(detail.raw)

    # -- context menu ------------------------------------------------------
    def _extend_menu(self, menu: QMenu, row: int) -> None:
        package = self._selected()
        summary = self.controller.packages.get(package)
        if summary is None:
            return
        online = bool(self.controller.serial and self.controller.device_state == "device")
        idle = online and not self.controller.busy and not self.users.busy and not self._confirming \
            and self.controller.user_id is not None
        menu.addAction("查看详情", self._details).setEnabled(idle)
        menu.addAction("复制包名", lambda: QApplication.clipboard().setText(package))
        menu.addSeparator()
        menu.addAction("启动", self._launch).setEnabled(idle)
        menu.addAction("强行停止…", self._force_stop).setEnabled(idle)
        if summary.enabled is False:
            menu.addAction("启用", self._enable).setEnabled(idle)
        else:
            menu.addAction("禁用…", self._disable).setEnabled(idle)
        menu.addAction("卸载…", self._uninstall).setEnabled(idle)
        menu.addSeparator()
        menu.addAction("导出 APK…", self._export).setEnabled(self.export_button.isEnabled())

    def _launch(self) -> None:
        package = self._selected()
        if package and not self._confirming:
            self._call(lambda: self.controller.launch(package))

    def _enable(self) -> None:
        package = self._selected()
        if package and not self._confirming:
            self._call(lambda: self.controller.set_enabled(package, True))

    def _force_stop(self) -> None:
        package = self._selected()
        if package:
            self._confirm("确认强行停止", "am force-stop 会结束该应用的全部进程，并取消其闹钟、任务与通知。",
                          lambda: self.controller.force_stop(package))

    def _disable(self) -> None:
        package = self._selected()
        if package:
            self._confirm("确认禁用应用", "pm disable-user 会在所选用户中禁用该应用（图标消失、不再运行）；可随时右键「启用」恢复。",
                          lambda: self.controller.set_enabled(package, False))

    def _uninstall(self) -> None:
        package = self._selected()
        summary = self.controller.packages.get(package)
        if summary is None:
            return
        extra = ("\n系统应用只会从所选用户移除，出厂 APK 仍保留在系统分区。" if summary.system else "")
        self._confirm("确认卸载", "pm uninstall 会删除该应用及其在所选用户中的数据，无法撤销。" + extra,
                      lambda: self.controller.uninstall(package))

    # -- install -------------------------------------------------------------
    def _pick_install(self) -> None:
        if not self.install_button.isEnabled():
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择 APK（多选 = 同一应用的 base + split）", "", "Android 安装包 (*.apk)")
        if files:
            self.install_files([Path(path) for path in files])

    def _ask_install_options(self, files: list[Path]) -> tuple[bool, bool] | None:
        dialog = InstallDialog(files, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.replace_box.isChecked(), dialog.test_box.isChecked()
        finally:
            dialog.deleteLater()

    def install_files(self, files: list[Path]) -> None:
        if self._confirming or self.controller.busy or self.users.busy or self.controller.user_id is None:
            self._local_error = "请先选择在线设备和用户，并等待当前操作完成后再安装。"
            self._changed()
            return
        bad = [str(path) for path in files if path.suffix.lower() != ".apk" or not path.is_file()]
        if bad:
            self._local_error = "只能安装存在的 .apk 文件：\n" + "\n".join(bad)
            self._changed()
            return
        captured = (self.controller.serial, self.controller.device_state, self.controller.user_id)
        self._confirming = True
        self._changed()
        try:
            options = self._ask_install_options(files)
        finally:
            self._confirming = False
        if options is None:
            self._changed()
            return
        if captured != (self.controller.serial, self.controller.device_state, self.controller.user_id) or self.controller.busy:
            self._local_error = "确认期间设备或用户已变化；未安装。"
            self._changed()
            return
        replace, allow_test = options
        self._call(lambda: self.controller.install(files, replace=replace, allow_test=allow_test))

    def _install_progress(self, line: str, percent: int) -> None:
        if percent >= 0:
            self.install_progress.setRange(0, 100)
            self.install_progress.setValue(percent)
        if line:
            self.install_label.setText(line[-160:])
            self.install_label.setToolTip(line)

    def _cancel_install(self) -> None:
        if self.controller.busy and self.controller.status == "安装 APK" and self.controller.task_id:
            self._runner.cancel(self.controller.task_id)

    @staticmethod
    def _dropped_apks(event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
        if not paths or len(paths) != len(mime.urls()) or any(path.suffix.lower() != ".apk" for path in paths):
            return []
        return paths

    def dragEnterEvent(self, event) -> None:
        if self._dropped_apks(event) and self.install_button.isEnabled():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        files = self._dropped_apks(event)
        if not files:
            event.ignore()
            return
        event.acceptProposedAction()
        # Leave the drag-and-drop handler before opening the modal dialog.
        QTimer.singleShot(0, lambda: self.install_files(files))

    def resizeEvent(self, event):
        self.splitter.setOrientation(Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal)
        super().resizeEvent(event)

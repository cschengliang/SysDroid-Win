from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

# Embedded Python does not add the script directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QCheckBox, QComboBox, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMenuBar, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStatusBar, QStyle, QStyleFactory,
    QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget,
)

from android_backend import DATA_DIR, STATUS_LABELS, Device, Task, TaskRunner, parse_devices
from command_library import CommandLibraryPage
from prop_page import PropPage
from scrcpy_page import ScrcpyPage
from task_panel import TaskPanel
from runtime_paths import configure_runtime
from android_users import AndroidUserController
from settings_page import SettingsPage
from apk_page import ApkPage
from process_page import ProcessPage


APP_NAME = "Android Toolbox"


_PAGE_ICON_PATHS = {
    "home": (((7, 2), (17, 2), (17, 22), (7, 22), (7, 2)),
             ((10, 5), (14, 5)), ((11, 19), (13, 19))),
    "commands": (((2, 4), (22, 4), (22, 20), (2, 20), (2, 4)),
                 ((6, 9), (9, 12), (6, 15)), ((12, 15), (17, 15))),
    "props": (((3, 3), (21, 3), (21, 21), (3, 21), (3, 3)),
              ((6, 7), (9, 7)), ((12, 7), (18, 7)),
              ((6, 12), (9, 12)), ((12, 12), (18, 12)),
              ((6, 17), (9, 17)), ((12, 17), (18, 17))),
    "settings": (((10, 2), (14, 2), (14.5, 5), (16, 6), (18.5, 5), (20.5, 8.5),
                  (18, 10.5), (18, 13.5), (20.5, 15.5), (18.5, 19), (16, 18),
                  (14.5, 19), (14, 22), (10, 22), (9.5, 19), (8, 18), (5.5, 19),
                  (3.5, 15.5), (6, 13.5), (6, 10.5), (3.5, 8.5), (5.5, 5),
                  (8, 6), (9.5, 5), (10, 2)),),
    "apks": (((12, 2), (21, 7), (21, 17), (12, 22), (3, 17), (3, 7), (12, 2)),
             ((3, 7), (12, 12), (21, 7)), ((12, 12), (12, 22)), ((8, 4.2), (17, 9.2))),
    "processes": (((2, 3), (22, 3), (22, 21), (2, 21), (2, 3)),
                  ((5, 14), (8, 14), (10, 8), (13, 17), (15, 11), (19, 11))),
    "scrcpy": (((3, 3), (21, 3), (21, 16), (3, 16), (3, 3)),
               ((12, 16), (12, 21)), ((8, 21), (16, 21)),
               ((10, 7), (15, 9.5), (10, 12), (10, 7))),
    "output": (((4, 2), (15, 2), (21, 8), (21, 22), (4, 22), (4, 2)),
               ((15, 2), (15, 8), (21, 8)), ((8, 12), (17, 12)),
               ((8, 16), (17, 16)), ((8, 19), (14, 19))),
}


def _page_icon(key: str) -> QIcon:
    paths = [QPolygonF([QPointF(x, y) for x, y in points]) for points in _PAGE_ICON_PATHS[key]]
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, "#425c70"), (QIcon.Mode.Selected, "#ffffff")):
        for ratio in (1, 2, 3):
            pixmap = QPixmap(24 * ratio, 24 * ratio)
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for path in paths:
                painter.drawPolyline(path)
            if key == "settings":
                painter.drawEllipse(QRectF(9, 9, 6, 6))
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


class AndroidToolboxWindow(QMainWindow):
    PAGE_INFO = {
        "home": ("设备连接", "连接、刷新并选择当前 ADB 设备；查看设备信息与特权状态。"),
        "commands": ("ADB 命令库", "管理与执行命令；预览参数后提交，查看真实任务输出与历史。"),
        "props": ("系统属性", "读取、搜索与编辑 getprop / setprop 系统属性；写入后读回确认，不自动提权。"),
        "settings": ("系统设置", "按 Android 用户读取与编辑 Settings 的 system / secure / global 值，确认实际读回。"),
        "apks": ("应用包信息", "按用户查看应用包信息、权限与组件，双击加载详情并导出 APK；不修改设备应用。"),
        "processes": ("进程监控", "每 2 秒采样进程 CPU、内存与启动信息，双击读取详情；权限缺失明确显示未知。"),
        "scrcpy": ("投屏与录屏", "通过 Scrcpy 配置独立投屏窗口、音频与录制；软件内不嵌入设备画面。"),
        "output": ("任务输出", "查看 ADB、Scrcpy 等真实任务的 stdout、stderr、状态和退出码。"),
    }

    def __init__(self, runtime_error: str = "") -> None:
        super().__init__()
        QApplication.styleHints().setColorScheme(Qt.ColorScheme.Light)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(980, 650)
        self.resize(1450, 900)
        self.runner = TaskRunner(self)
        self._runtime_error = runtime_error
        self.users = AndroidUserController(self.runner, self)
        self._device_state = ""
        self._active_page_key = ""
        self._dock_collapsed = False
        self._log_collapsed = False
        self._dock_ratio = 0.25
        self._dock_initialized = False
        self._device_serial = ""
        self._devices: dict[str, Device] = {}
        self._pending: dict[str, Callable[[Task], None]] = {}
        self._refresh_id = ""
        self._logs: list[tuple[str, str]] = []
        self._closing = False
        self._settings = QSettings(str(DATA_DIR / "workspace.ini"), QSettings.Format.IniFormat)
        self._page_icons = {key: _page_icon(key) for key in self.PAGE_INFO}
        self._build_menu_bar()
        self._build_tool_bar()
        self._build_body()
        self._build_status_bar()
        self._apply_style()
        self.runner.error.connect(lambda message: self._write_log("[ERROR] " + message))
        self.runner.task_added.connect(self._task_added)
        self.runner.task_finished.connect(self._task_finished)
        self._select_page("home")
        process_columns = self._settings.value("processes/columns")
        if isinstance(process_columns, str):
            process_columns = [process_columns]
        if isinstance(process_columns, list):
            self.process_page.set_visible_columns(process_columns)
        geometry = self._settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = self._settings.value("splitter")
        if splitter_state is not None:
            self.workspace_splitter.restoreState(splitter_state)
            self._dock_initialized = True
        ratio = self._settings.value("dock_ratio", 0.25)
        try:
            self._dock_ratio = max(0.1, min(0.7, float(ratio)))
        except (ValueError, TypeError):
            pass
        self._constrain_geometry()
        QTimer.singleShot(0, self._initialize_dock)
        if self._runtime_error:
            self._write_log("[ERROR] " + self._runtime_error)
            self.command_page.set_runtime_error(self._runtime_error)
            self.home_page.setEnabled(False)
            self.prop_page.setEnabled(False)
            self.scrcpy_page.setEnabled(False)
            for page in (self.settings_page, self.apk_page, self.process_page):
                page.setEnabled(False)
            self.device_selector.setEnabled(False)
            self.connected.setEnabled(False)
            for action in self.menuBar().actions():
                if action.text() in {"设备(&D)", "工具(&T)"}:
                    action.setEnabled(False)
            for toolbar in self.findChildren(QToolBar):
                for action in toolbar.actions():
                    if action.text() in {"刷新设备", "ADB 终端", self.PAGE_INFO["scrcpy"][0]}:
                        action.setEnabled(False)
        else:
            self._write_log("[INFO] Android Toolbox 已启动；正在查询真实设备。")
            QTimer.singleShot(0, self._refresh_devices)

    def _standard_icon(self, pixmap: QStyle.StandardPixmap) -> QIcon:
        return self.style().standardIcon(pixmap)

    def _action(self, text: str, pixmap: QStyle.StandardPixmap | QIcon, callback: Callable) -> QAction:
        icon = pixmap if isinstance(pixmap, QIcon) else self._standard_icon(pixmap)
        action = QAction(icon, text, self)
        action.triggered.connect(callback)
        return action

    def _button(self, text: str, callback: Callable, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        if primary:
            button.setObjectName("primaryButton")
        return button

    def _build_menu_bar(self) -> None:
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        file_menu = menu_bar.addMenu("文件(&F)")
        file_menu.addAction("保存工作区", self._save_workspace)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)
        device_menu = menu_bar.addMenu("设备(&D)")
        device_menu.addAction("获取 ADB 设备", self._refresh_devices)
        device_menu.addAction("连接无线 ADB", self._focus_wireless)
        device_menu.addAction("断开当前无线设备", self._disconnect_device)
        view_menu = menu_bar.addMenu("视图(&V)")
        for key, (title, _) in self.PAGE_INFO.items():
            action = QAction(self._page_icons[key], title, self)
            action.setToolTip(self.PAGE_INFO[key][1])
            action.triggered.connect(lambda checked=False, page=key: self._select_page(page))
            view_menu.addAction(action)
        view_menu.addAction("展开 / 收起活动日志", self._toggle_log)
        tools_menu = menu_bar.addMenu("工具(&T)")
        tools_menu.addAction("ADB Root", self._adb_root)
        tools_menu.addAction("打开内置 ADB 终端", self._open_terminal)
        help_menu = menu_bar.addMenu("帮助(&H)")
        help_menu.addAction("关于 Android Toolbox", self._show_about)
        self.page_context = QLabel()
        self.page_context.setObjectName("pageContext")
        self.page_context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        menu_bar.setCornerWidget(self.page_context, Qt.Corner.TopRightCorner)

    def _build_tool_bar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(toolbar)
        brand = QLabel("  ▸  Android Toolbox")
        brand.setObjectName("brandLabel")
        toolbar.addWidget(brand)
        toolbar.addSeparator()
        for text, icon, callback in [
            ("刷新设备", QStyle.StandardPixmap.SP_BrowserReload, self._refresh_devices),
            ("ADB 终端", QStyle.StandardPixmap.SP_CommandLink, self._open_terminal),
            (self.PAGE_INFO["scrcpy"][0], self._page_icons["scrcpy"], lambda: self._select_page("scrcpy")),
            (self.PAGE_INFO["output"][0], self._page_icons["output"], lambda: self._select_page("output")),
        ]:
            toolbar.addAction(self._action(text, icon, callback))
        toolbar.addSeparator()
        self.device_selector = QComboBox()
        self.device_selector.setObjectName("deviceSelector")
        self.device_selector.setMinimumWidth(210)
        self.device_selector.addItem("未发现设备", "")
        self.device_selector.currentIndexChanged.connect(self._device_changed)
        toolbar.addWidget(self.device_selector)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        toolbar.addWidget(spacer)
        self.connected = QPushButton("设备未连接")
        self.connected.setObjectName("connectedButton")
        self.connected.clicked.connect(self._refresh_devices)
        toolbar.addWidget(self.connected)
        toolbar.addAction(self._action("关于", QStyle.StandardPixmap.SP_MessageBoxInformation, self._show_about))

    def _build_body(self) -> None:
        shell = QWidget()
        shell.setObjectName("shell")
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        nav_panel = QWidget()
        nav_panel.setObjectName("navPanel")
        nav_panel.setMinimumWidth(160)
        nav_panel.setMaximumWidth(220)
        self.nav_panel = nav_panel
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.addWidget(QLabel("  功能"))
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.setIconSize(QSize(20, 20))
        self._nav_items = {}
        groups = [
            ("设备工具", ("home", "commands", "props", "settings", "apks", "processes", "scrcpy")),
            ("运行记录", ("output",)),
        ]
        for group, entries in groups:
            heading = QListWidgetItem(group)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            heading.setSizeHint(QSize(200, 28))
            self.navigation.addItem(heading)
            for key in entries:
                title, description = self.PAGE_INFO[key]
                item = QListWidgetItem(self._page_icons[key], title)
                item.setToolTip(f"{title}\n{description}")
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setSizeHint(QSize(200, 34))
                self.navigation.addItem(item)
                self._nav_items[key] = item
        self.navigation.currentItemChanged.connect(self._switch_page)
        nav_layout.addWidget(self.navigation, 1)
        shell_layout.addWidget(nav_panel)
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")
        self.home_page = self._build_adb_home_page()
        self.command_page = CommandLibraryPage(self.runner)
        self.prop_page = PropPage(self.runner)
        self.scrcpy_page = ScrcpyPage(self.runner)
        self.settings_page = SettingsPage(self.runner, self.users)
        self.apk_page = ApkPage(self.runner, self.users)
        self.process_page = ProcessPage(self.runner)
        self._device_pages = {"props": self.prop_page, "settings": self.settings_page,
                              "apks": self.apk_page, "processes": self.process_page, "scrcpy": self.scrcpy_page}
        self._page_indices = {"home": self.pages.addWidget(self.home_page),
                              "commands": self.pages.addWidget(self.command_page),
                              "props": self.pages.addWidget(self.prop_page),
                              "settings": self.pages.addWidget(self.settings_page),
                              "apks": self.pages.addWidget(self.apk_page),
                              "processes": self.pages.addWidget(self.process_page),
                              "scrcpy": self.pages.addWidget(self.scrcpy_page)}
        self.task_panel = TaskPanel(self.runner)
        self.command_page.show_output.connect(self._show_task)
        self.prop_page.show_output.connect(self._show_task)
        for page in (self.settings_page, self.process_page):
            page.show_output.connect(self._show_task)
        self.process_page.log_message.connect(self._write_log)
        if hasattr(self.scrcpy_page, "show_output"):
            self.scrcpy_page.show_output.connect(self._show_task)
        self.bottom = QStackedWidget()
        self.log_panel = self._build_log_panel()
        self.bottom.addWidget(self.log_panel)
        self.bottom.addWidget(self.task_panel)
        self.workspace_splitter.addWidget(self.pages)
        self.workspace_splitter.addWidget(self.bottom)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.splitterMoved.connect(self._dock_moved)
        workspace_layout.addWidget(self.workspace_splitter)
        shell_layout.addWidget(workspace, 1)
        self.setCentralWidget(shell)
        quick = QAction("快速执行", self)
        quick.setShortcut(QKeySequence("Ctrl+K"))
        quick.triggered.connect(self._quick_command)
        self.addAction(quick)

    def _build_adb_home_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        page.setObjectName("adbHomePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 8)
        top = QHBoxLayout()
        summary = QGroupBox("当前设备")
        summary_layout = QVBoxLayout(summary)
        self.device_model = QLabel("未选择设备")
        self.device_model.setObjectName("deviceModel")
        summary_layout.addWidget(self.device_model)
        self.current_device_input = QLineEdit()
        self.current_device_input.setReadOnly(True)
        summary_layout.addWidget(self.current_device_input)
        self.device_details = QLabel("Product / Device / Transport：—")
        self.device_details.setWordWrap(True)
        summary_layout.addWidget(self.device_details)
        self.android_version = QLabel("Android：未检测")
        summary_layout.addWidget(self.android_version)
        actions = QHBoxLayout()
        for text, callback in [("刷新", self._refresh_devices), ("无线连接", self._focus_wireless),
                               ("打开终端", self._open_terminal)]:
            actions.addWidget(self._button(text, callback))
        summary_layout.addLayout(actions)
        top.addWidget(summary, 1)
        connection = QGroupBox("ADB 连接 · 全局上下文")
        connection_layout = QVBoxLayout(connection)
        connection_layout.addWidget(QLabel("无线 ADB 地址"))
        row = QHBoxLayout()
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("例如 192.168.1.10:5555")
        self.address_input.returnPressed.connect(self._connect_wireless)
        row.addWidget(self.address_input, 1)
        row.addWidget(self._button("连接", self._connect_wireless, True))
        connection_layout.addLayout(row)
        self.connection_note = QLabel("尚未刷新 ADB Server")
        self.connection_note.setWordWrap(True)
        connection_layout.addWidget(self.connection_note)
        connection_layout.addStretch()
        top.addWidget(connection, 1)
        layout.addLayout(top)
        self._home_columns = [top]
        lower = QHBoxLayout()
        privilege = QGroupBox("ADB 特权状态 · 按当前设备检测")
        privilege_layout = QVBoxLayout(privilege)
        self.privilege_labels = {}
        pills = QHBoxLayout()
        for name in ("Root", "Remount", "Debuggable"):
            label = QLabel(f"{name}：未检测")
            label.setObjectName("statusPill")
            self.privilege_labels[name] = label
            pills.addWidget(label)
        privilege_layout.addLayout(pills)
        row = QHBoxLayout()
        row.addWidget(self._button("获取状态", self._query_status))
        row.addWidget(self._button("ADB Root", self._adb_root))
        self.remount_button = self._button("ADB Remount", self._adb_remount)
        self.remount_button.setEnabled(False)
        row.addWidget(self.remount_button)
        privilege_layout.addLayout(row)
        lower.addWidget(privilege, 1)
        shortcuts = QGroupBox("快捷操作")
        shortcut_layout = QVBoxLayout(shortcuts)
        for buttons in [[("获取 ADB 设备", self._refresh_devices), ("断开无线设备", self._disconnect_device)],
                        [("启动 Server", lambda: self._server_action(False)),
                         ("重启 Server", lambda: self._server_action(True)), ("ADB 版本", self._adb_version)]]:
            row = QHBoxLayout()
            for text, callback in buttons:
                row.addWidget(self._button(text, callback))
            shortcut_layout.addLayout(row)
        lower.addWidget(shortcuts, 1)
        layout.addLayout(lower)
        self._home_columns.append(lower)
        box = QGroupBox("设备列表 · 双击设为当前设备")
        box_layout = QVBoxLayout(box)
        tools = QHBoxLayout()
        self.device_hint = QLabel("正在查询设备")
        tools.addWidget(self.device_hint, 1)
        for text, callback in [("复制 Serial", self._copy_serial), ("设为当前设备", self._select_highlighted),
                               ("刷新", self._refresh_devices)]:
            tools.addWidget(self._button(text, callback))
        box_layout.addLayout(tools)
        self.device_table = QTableWidget(0, 6)
        self.device_table.setObjectName("deviceTable")
        self.device_table.setHorizontalHeaderLabels(["Serial", "State", "Model", "Product", "Device", "Transport"])
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.device_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.device_table.setAlternatingRowColors(True)
        self.device_table.verticalHeader().hide()
        self.device_table.horizontalHeader().setStretchLastSection(True)
        self.device_table.horizontalHeader().setDefaultSectionSize(140)
        self.device_table.setMinimumHeight(130)
        self.device_table.cellDoubleClicked.connect(lambda row, column: self._select_highlighted())
        self.device_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.device_table.customContextMenuRequested.connect(self._device_context_menu)
        box_layout.addWidget(self.device_table)
        layout.addWidget(box, 1)
        hint = QLabel("仅 state=device 可执行设备命令。USB 断开需拔线；无线连接使用 adb connect / disconnect。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll.setWidget(page)
        return scroll

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 5, 10, 7)
        header = QHBoxLayout()
        header.addWidget(QLabel("活动日志"))
        self.log_count = QLabel("0 条记录")
        header.addWidget(self.log_count)
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部", "INFO", "OK", "WARN", "ERROR", "VIEW"])
        self.log_filter.currentTextChanged.connect(self._render_log)
        header.addWidget(self.log_filter)
        self.log_auto = QCheckBox("自动滚动")
        self.log_auto.setChecked(True)
        header.addWidget(self.log_auto)
        header.addStretch()
        header.addWidget(self._button("清空", self._clear_log))
        self.log_toggle = self._button("收起", self._toggle_log)
        header.addWidget(self.log_toggle)
        layout.addLayout(header)
        self.log_output = QPlainTextEdit()
        self.log_output.setObjectName("logOutput")
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(1000)
        layout.addWidget(self.log_output)
        return panel

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self.connection_status = QLabel("设备未连接")
        self.status_serial = QLabel("ADB：未选择设备")
        self.task_status = QLabel("活动任务 0")
        bar.addWidget(self.connection_status)
        bar.addPermanentWidget(self.task_status)
        bar.addPermanentWidget(self.status_serial)
        bar.addPermanentWidget(QLabel("v0.2.0"))
        self.runner.active_count_changed.connect(lambda count: self.task_status.setText(f"活动任务 {count}"))

    def _select_page(self, key: str) -> None:
        self.navigation.setCurrentItem(self._nav_items[key])

    def _switch_page(self, item: QListWidgetItem | None, previous=None) -> None:
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        old_page = self._device_pages.get(self._active_page_key)
        if old_page is not None and self._active_page_key != key:
            old_page.set_active(False)
        self._active_page_key = key
        title, description = self.PAGE_INFO[key]
        self._page_description = f"{title}  ·  {description}"
        self._update_title()
        self.pages.setVisible(key != "output")
        if key in self._page_indices:
            self.pages.setCurrentIndex(self._page_indices[key])
        page = self._device_pages.get(key)
        if page is not None and not self._runtime_error and not self._closing:
            page.set_active(True)
        self.bottom.setCurrentIndex(1 if key in {"commands", "props", "settings", "apks", "processes", "output"} else 0)
        self._sync_dock_height()
        self._write_log("[VIEW] 已打开：" + title)

    def _update_title(self) -> None:
        menu = self.menuBar()
        occupied = max((menu.actionGeometry(action).right() for action in menu.actions()), default=0)
        available = max(0, menu.width() - occupied - 24)
        self.page_context.setMaximumWidth(available)
        text = getattr(self, "_page_description", "")
        self.page_context.setText(self.page_context.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(0, available - 16)))
        self.page_context.setToolTip(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "page_context"):
            self._update_title()
        if hasattr(self, "_home_columns"):
            direction = QBoxLayout.Direction.TopToBottom if self.home_page.viewport().width() < 900 else QBoxLayout.Direction.LeftToRight
            for layout in self._home_columns:
                layout.setDirection(direction)
        if hasattr(self, "nav_panel"):
            self.nav_panel.setMaximumWidth(184 if self.width() >= 1100 else 160)

    def _constrain_geometry(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.setMinimumSize(min(980, area.width()), min(650, max(1, area.height() - 40)))
        self.resize(min(self.width(), area.width()), min(self.height(), max(1, area.height() - 40)))
        self.move(max(area.left(), min(self.x(), area.right() - self.width() + 1)),
                  max(area.top(), min(self.y(), area.bottom() - self.height() - 30)))

    def _initialize_dock(self) -> None:
        height = self.workspace_splitter.height()
        if not self._dock_initialized:
            bottom = min(max(140, int(height * 0.25)), 220, max(0, height - 300))
            self.workspace_splitter.setSizes([height - bottom, bottom])
        self._dock_initialized = True
        self._dock_moved()

    def _dock_moved(self, *unused) -> None:
        if self._dock_collapsed:
            return
        sizes = self.workspace_splitter.sizes()
        if sum(sizes) and sizes[0] > 0:
            self._dock_ratio = sizes[1] / sum(sizes)

    def _show_task(self, task_id: str) -> None:
        # Device pages keep their editor/list when viewing raw task output.
        if self.pages.currentWidget() not in (self.command_page, *self._device_pages.values()) or not self.pages.isVisible():
            self._select_page("output")
        self.bottom.setCurrentIndex(1)
        self._sync_dock_height()
        self.task_panel.show_task(task_id)

    def _quick_command(self) -> None:
        self._select_page("commands")
        self.command_page.open_execution()

    def _device_changed(self, index: int) -> None:
        previous = (self._device_serial, self._device_state)
        self._device_serial = self.device_selector.itemData(index) or ""
        device = self._devices.get(self._device_serial)
        state = device.state if device else ""
        self._device_state = state
        changed = previous != (self._device_serial, state)
        self.current_device_input.setText(self._device_serial)
        self.device_model.setText(device.model or device.serial if device else "未选择设备")
        self.device_details.setText(f"Product：{device.product or '—'}  ·  Device：{device.device or '—'}  ·  Transport：{device.transport or '—'}" if device else "Product / Device / Transport：—")
        self.connected.setText("设备已连接" if state == "device" else (state or "设备未连接"))
        self.connection_status.setText("设备已连接" if state == "device" else (state or "设备未连接"))
        self.status_serial.setText("ADB：" + (self._device_serial or "未选择设备"))
        self.command_page.set_device(self._device_serial, state)
        self.prop_page.set_device(self._device_serial, state)
        self.scrcpy_page.set_device(self._device_serial, state, device.model if device else "")
        self.users.set_device(self._device_serial, state)
        for page in (self.settings_page, self.apk_page, self.process_page):
            page.set_device(self._device_serial, state)
        if changed:
            self.android_version.setText("Android：未检测")
            for name, label in self.privilege_labels.items():
                label.setText(f"{name}：未检测")
            self.remount_button.setEnabled(False)
            if self._device_serial:
                self._write_log(f"[INFO] 当前设备：{self._device_serial} · {state}")

    def _refresh_devices(self) -> None:
        if self._runtime_error:
            self._write_log("[ERROR] " + self._runtime_error)
            return
        if self._refresh_id and any(task.id == self._refresh_id for task in self.runner.active()):
            return
        task = self._run_adb("获取设备列表", ["devices", "-l"], callback=self._devices_received)
        self._refresh_id = task.id

    def _devices_received(self, task: Task) -> None:
        if task.status != "succeeded":
            self.connection_note.setText("刷新失败，请查看任务错误输出")
            self.device_hint.setText("设备列表已失效：刷新失败")
            self._set_devices([])
            return
        devices = parse_devices(task.stdout)
        self._set_devices(devices)
        self.connection_note.setText(f"ADB Server 已响应 · 上次刷新 {datetime.now():%H:%M:%S}")
        self.device_hint.setText(f"已发现 {len(devices)} 台设备 · 在线 {sum(device.state == 'device' for device in devices)}")
        self._write_log(f"[OK] 获取到 {len(devices)} 台真实设备")

    def _set_devices(self, devices: list[Device]) -> None:
        previous = self._device_serial
        self._devices = {device.serial: device for device in devices}
        self.device_selector.blockSignals(True)
        self.device_selector.clear()
        self.device_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            self.device_selector.addItem(f"{device.serial} · {device.state}", device.serial)
            for col, value in enumerate((device.serial, device.state, device.model, device.product, device.device, device.transport)):
                self.device_table.setItem(row, col, QTableWidgetItem(value))
        if not devices:
            self.device_selector.addItem("未发现设备", "")
        index = self.device_selector.findData(previous)
        if index < 0:
            index = next((i for i, device in enumerate(devices) if device.state == "device"), 0)
        self.device_selector.setCurrentIndex(index)
        self.device_selector.blockSignals(False)
        self._device_changed(index)
        if devices:
            self.device_table.selectRow(index)

    def _highlighted_serial(self) -> str:
        row = self.device_table.currentRow()
        return self.device_table.item(row, 0).text() if row >= 0 else ""

    def _select_highlighted(self) -> None:
        serial = self._highlighted_serial()
        if serial:
            self.device_selector.setCurrentIndex(self.device_selector.findData(serial))

    def _copy_serial(self) -> None:
        serial = self._highlighted_serial() or self._device_serial
        if serial:
            QApplication.clipboard().setText(serial)
            self._write_log("[OK] 已复制 Serial：" + serial)

    def _device_context_menu(self, position) -> None:
        row = self.device_table.rowAt(position.y())
        if row < 0:
            return
        self.device_table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("复制 Serial", self._copy_serial)
        menu.addAction("设为当前设备", self._select_highlighted)
        serial = self._highlighted_serial()
        action = menu.addAction("断开无线设备", lambda: self._disconnect_device(serial))
        action.setEnabled(":" in serial)
        menu.exec(self.device_table.viewport().mapToGlobal(position))

    def _require_device(self) -> bool:
        device = self._devices.get(self._device_serial)
        if not device or device.state != "device":
            self._write_log("[WARN] 请先选择 state=device 的在线设备。")
            return False
        return True

    def _focus_wireless(self) -> None:
        self._select_page("home")
        self.address_input.setFocus()

    def _connect_wireless(self) -> None:
        address = self.address_input.text().strip()
        if not address or any(char.isspace() for char in address) or ":" not in address:
            self._write_log("[WARN] 请输入 host:port 格式的无线 ADB 地址。")
            self._focus_wireless()
            return
        self._run_adb("连接无线 ADB", ["connect", address], timeout=30,
                      callback=lambda task: self._refresh_devices() if task.status == "succeeded" else None)

    def _disconnect_device(self, serial=None) -> None:
        serial = serial if isinstance(serial, str) else self._device_serial
        if ":" not in serial:
            self._write_log("[WARN] adb disconnect 仅支持无线连接；USB 设备需拔线。")
            return
        self._run_adb("断开无线 ADB", ["disconnect", serial], callback=lambda task: self._refresh_devices())

    def _query_status(self) -> None:
        if not self._require_device():
            return
        script = "id -u; getprop ro.debuggable; getprop ro.build.version.release; cat /proc/mounts"
        self._run_adb("检测设备特权状态", ["shell", script], self._device_serial, callback=self._status_received)

    def _status_received(self, task: Task) -> None:
        if task.status != "succeeded" or task.serial != self._device_serial:
            return
        lines = task.stdout.splitlines()
        if len(lines) < 3:
            self._write_log("[ERROR] 设备特权状态输出不完整，请查看任务输出。")
            return
        root = lines[0].strip() == "0"
        self.privilege_labels["Root"].setText("Root：" + ("是" if root else "否"))
        self.privilege_labels["Debuggable"].setText("Debuggable：" + lines[1].strip())
        writable = any(len(parts := line.split()) >= 4 and parts[1] in {"/", "/system", "/vendor"}
                       and "rw" in parts[3].split(",") for line in lines[3:])
        self.privilege_labels["Remount"].setText("Remount：" + ("系统分区可写" if writable else "未见可写系统分区"))
        self.remount_button.setEnabled(root)
        self.android_version.setText("Android：" + lines[2].strip())

    def _adb_root(self) -> None:
        if self._require_device() and QMessageBox.question(self, "ADB Root", "将重启当前设备的 adbd；仅调试构建支持。继续？") == QMessageBox.StandardButton.Yes:
            self._run_adb("ADB Root", ["root"], self._device_serial,
                          callback=lambda task: self._refresh_devices())

    def _adb_remount(self) -> None:
        if self._require_device() and QMessageBox.question(self, "ADB Remount", "请求将系统分区重新挂载为可写。继续？") == QMessageBox.StandardButton.Yes:
            self._run_adb("ADB Remount", ["remount"], self._device_serial, timeout=30,
                          callback=lambda task: self._query_status())

    def _adb_version(self) -> None:
        task = self._run_adb("ADB 版本", ["version"])
        self._show_task(task.id)

    def _server_action(self, restart: bool) -> None:
        if restart:
            if QMessageBox.question(self, "重启 ADB Server", "将中断此电脑上的 ADB 连接，包括其他程序的连接。继续？") != QMessageBox.StandardButton.Yes:
                return
            self._run_adb("停止 ADB Server", ["kill-server"],
                          callback=lambda task: self._server_action(False) if task.status == "succeeded" else None)
        else:
            self._run_adb("启动 ADB Server", ["start-server"], timeout=30,
                          callback=lambda task: self._refresh_devices() if task.status == "succeeded" else None)

    def _run_adb(self, title: str, args: list[str], serial: str = "", timeout: int = 10,
                 callback: Callable[[Task], None] | None = None) -> Task:
        task = self.runner.start_adb(title, args, serial=serial, timeout=timeout)
        if callback:
            self._pending[task.id] = callback
        return task

    def _open_terminal(self) -> None:
        if not self._require_device():
            return
        try:
            pid = self.runner.open_adb_terminal(self._device_serial)
        except (OSError, ValueError) as exc:
            self._write_log(f"[ERROR] 打开内置 ADB 终端失败：{exc}")
            return
        self._write_log(f"[INFO] 已启动内置 ADB 终端：{self._device_serial} · PID {pid}")

    def _task_added(self, task: Task) -> None:
        if task.transient:
            return
        self._write_log(f"[INFO] 启动任务：{task.title} · {task.serial or 'ADB Server'}")

    def _task_finished(self, task: Task) -> None:
        if not task.transient:
            level = "OK" if task.status == "succeeded" else "ERROR" if task.status == "failed" else "WARN"
            self._write_log(f"[{level}] {task.title}：{STATUS_LABELS[task.status]} · 退出码 {task.exit_code if task.exit_code is not None else '—'}")
            if task.status == "failed" and task.stderr:
                self._write_log("[ERROR] " + task.stderr.strip()[-1500:])
        callback = self._pending.pop(task.id, None)
        if callback:
            callback(task)
        if self._closing and not self.runner.active_count:
            QTimer.singleShot(0, self.close)

    def _save_workspace(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._write_log(f"[ERROR] 无法保存工作区布局：{exc}")
            return
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("splitter", self.workspace_splitter.saveState())
        self._settings.setValue("dock_ratio", self._dock_ratio)
        self._settings.setValue("processes/columns", self.process_page.visible_columns())
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            self._write_log("[ERROR] 无法保存工作区布局。")
        else:
            self._write_log("[OK] 工作区布局已保存。")

    def _toggle_log(self) -> None:
        self._log_collapsed = not self._log_collapsed
        self.log_output.setVisible(not self._log_collapsed)
        self.log_toggle.setText("展开" if self._log_collapsed else "收起")
        self._sync_dock_height()

    def _sync_dock_height(self) -> None:
        collapsed = self.bottom.currentWidget() == self.log_panel and self._log_collapsed
        if collapsed == self._dock_collapsed:
            return
        if collapsed:
            self._dock_moved()
            self._dock_collapsed = True
            height = self.log_toggle.sizeHint().height() + 12
            self.bottom.setMaximumHeight(height)
            self.workspace_splitter.setSizes([self.workspace_splitter.height() - height, height])
        else:
            self._dock_collapsed = False
            self.bottom.setMaximumHeight(16777215)
            total = self.workspace_splitter.height()
            height = max(140, int(total * self._dock_ratio))
            self.workspace_splitter.setSizes([max(300, total - height), height])

    def _clear_log(self) -> None:
        self._logs.clear()
        self._render_log()

    def _write_log(self, message: str) -> None:
        if not hasattr(self, "log_output"):
            return
        level = next((value for value in ("ERROR", "WARN", "OK", "VIEW", "INFO") if f"[{value}]" in message), "INFO")
        text = f"[{datetime.now():%H:%M:%S}] {message}"
        self._logs.append((level, text))
        self._logs = self._logs[-1000:]
        if self.log_filter.currentText() in {"全部", level}:
            scrollbar = self.log_output.verticalScrollBar()
            position = scrollbar.value()
            self.log_output.appendPlainText(text)
            scrollbar.setValue(scrollbar.maximum() if self.log_auto.isChecked() else position)
        self.log_count.setText(f"{len(self._logs)} 条记录")

    def _render_log(self, *args) -> None:
        selected = self.log_filter.currentText()
        self.log_output.setPlainText("\n".join(text for level, text in self._logs if selected in {"全部", level}))
        self.log_count.setText(f"{len(self._logs)} 条记录")
        if self.log_auto.isChecked():
            self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def _show_about(self) -> None:
        QMessageBox.information(self, "关于 Android Toolbox", "Android Toolbox v0.2.0\nPySide6 桌面安卓工具箱\n\n设备连接、ADB 命令库、系统属性与设置、应用包信息、只读进程监控、任务输出与独立投屏录制窗口。")

    def closeEvent(self, event) -> None:
        if self.runner.active_count:
            if not self._closing:
                answer = QMessageBox.question(self, "退出", "仍有任务或 Scrcpy 会话运行。停止后退出？")
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._closing = True
                for page in self._device_pages.values():
                    page.set_active(False)
                for task in self.runner.active():
                    self.runner.cancel(task.id)
                self.setEnabled(False)
            event.ignore()
            return
        for page in self._device_pages.values():
            page.set_active(False)
        self._save_workspace()
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget { color: #1f1f1f; }
            QMainWindow, QWidget#shell, QWidget#adbHomePage, QStackedWidget#pages { background: #f3f3f3; color: #1f1f1f; }
            QMenuBar { background: #f3f3f3; border-bottom: 1px solid #d1d1d1; padding: 0 4px; }
            QMenuBar::item { padding: 4px 8px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #d9ecfa; color: #1f1f1f; }
            QLabel#pageContext { padding: 0 8px; color: #476173; }
            QToolBar#mainToolbar { background: #f3f3f3; border: 0; border-bottom: 1px solid #c8c8c8; spacing: 2px; padding: 1px 4px; }
            QToolBar#mainToolbar QToolButton { min-width: 28px; min-height: 26px; border: 1px solid transparent; padding: 2px; }
            QToolBar#mainToolbar QToolButton:hover { background: #e5f3fb; border-color: #b7d9ec; }
            QLabel#brandLabel { font-weight: 600; }
            QLabel#deviceModel { font-size: 18px; font-weight: 600; }
            QWidget#navPanel, QListWidget#navigation { background: #e9edef; border: 0; }
            QListWidget#navigation::item { padding: 5px 10px; }
            QListWidget#navigation::item:selected { background: #2878b5; color: white; }
            QGroupBox { background: #fafafa; border: 1px solid #bcbcbc; margin-top: 7px; padding-top: 9px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 5px; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 24px; border: 1px solid #9e9e9e; background: white; padding: 0 6px; }
            QLineEdit:focus, QComboBox:focus { border-color: #0078d4; }
            QTableWidget { background: white; gridline-color: #c7c7c7; selection-background-color: #cde6f7; selection-color: #202020; alternate-background-color: #f7f7f7; }
            QHeaderView::section { background: #dcebf5; border: 1px solid #c1cbd2; padding: 5px; color: #304554; }
            QPushButton { min-height: 26px; padding: 0 10px; border: 1px solid #9e9e9e; background: #f5f5f5; color: #202020; }
            QPushButton:hover { background: #e5f3fb; border-color: #5b9bd5; }
            QPushButton:disabled { color: #999; background: #ededed; border-color: #c7c7c7; }
            QPushButton#primaryButton { background: #2878b5; border-color: #1f5f8c; color: white; font-weight: 600; }
            QLabel#statusPill { background: #eee; border: 1px solid #bdbdbd; padding: 4px 6px; color: #555; }
            QPlainTextEdit { background: white; border: 1px solid #9e9e9e; font-family: Consolas, "Microsoft YaHei UI"; font-size: 12px; }
            QStatusBar { background: #e9edef; border-top: 1px solid #bdbdbd; color: #50575c; }
            QStatusBar::item { border: 0; padding: 0 7px; }
        """)


def main() -> int:
    app = QApplication(sys.argv)
    if "windows11" in QStyleFactory.keys():
        app.setStyle("windows11")
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    runtime_error = ""
    try:
        configure_runtime()
    except ValueError as exc:
        runtime_error = str(exc)
    window = AndroidToolboxWindow(runtime_error)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

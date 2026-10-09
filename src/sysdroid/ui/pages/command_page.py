from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from sysdroid.core.backend import STATUS_LABELS, TERMINAL_STATUSES, Task, TaskRunner, powershell_command
from sysdroid.core.commands import (
    CATEGORIES, EXECUTION_TYPES, Command, CommandStore, PreparedCommand, StorageError,
    is_remote_shell, parse_payload, prepare_command, variable_names,
)
from sysdroid.ui import kit as ui_kit
from sysdroid.ui.tables import TableTools
def command_preview(prepared: PreparedCommand, program: str = "adb") -> str:
    return powershell_command(program, (["-s", prepared.serial] if prepared.serial else []) + list(prepared.args))


def _table(headers: list[str], name: str) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _check_text(table: QTableWidget):
    """Copy / export column 0 checkboxes as 是 / 否 instead of an empty cell."""
    def text(row: int, column: int) -> str | None:
        item = table.item(row, column) if column == 0 else None
        if item is None:
            return None
        return "是" if item.checkState() == Qt.CheckState.Checked else "否"
    return text


def _button(text: str, callback, layout) -> QPushButton:
    button = QPushButton(text)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return button


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ExecutionDialog(QDialog):
    """Parameter fields are data, except the explicitly acknowledged remote script."""
    submitted = Signal(object)

    def __init__(self, page: CommandLibraryPage, commands: list[Command], command_id: str | None = None) -> None:
        super().__init__(page)
        self.page = page
        self.commands = commands
        self.fields: dict[tuple[int, str], QLineEdit] = {}
        self.prepared: list[PreparedCommand] = []
        self.setWindowTitle("执行命令 · 参数预览")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(760, 610)
        layout = QVBoxLayout(self)
        self.device_label = _note("")
        layout.addWidget(self.device_label)
        layout.addWidget(_note("执行类型仅为分类元数据，不改变超时或停止行为；任意 Shell 脚本需手动选择类型，无法可靠推断。"))
        self.selector = QComboBox()
        self.selector.setObjectName("executionCommand")
        self.selector.setEditable(True)
        self.selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for command in commands:
            self.selector.addItem(command.name, command.id)
        self.selector.setCurrentIndex(max(0, self.selector.findData(command_id)))
        layout.addWidget(QLabel("选择命令（可输入名称查找）"))
        layout.addWidget(self.selector)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.parameter_widget = QWidget()
        self.parameter_layout = QVBoxLayout(self.parameter_widget)
        scroll.setWidget(self.parameter_widget)
        layout.addWidget(scroll, 1)
        self.remote_ack = QCheckBox("我确认远程 Shell 脚本可执行管道、重定向和多个命令，并可能修改设备")
        self.remote_ack.toggled.connect(self.update_preview)
        layout.addWidget(self.remote_ack)
        self.settings_label = _note("")
        layout.addWidget(self.settings_label)
        layout.addWidget(QLabel("实际 argv · PowerShell 可复制预览（不会使用本地 Shell）"))
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("executionPreview")
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(125)
        layout.addWidget(self.preview, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        self.run_button = buttons.addButton("执行命令", QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_button.clicked.connect(self._submit)
        layout.addWidget(buttons)
        self.selector.currentIndexChanged.connect(self._build_fields)
        self._build_fields()

    def _selected_commands(self) -> list[Command]:
        command_id = self.selector.currentData()
        return [command for command in self.commands if command.id == command_id]

    def _build_fields(self) -> None:
        while self.parameter_layout.count():
            item = self.parameter_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.fields.clear()
        self.remote_ack.setChecked(False)
        has_remote = False
        for index, command in enumerate(self._selected_commands()):
            box = QGroupBox(command.name)
            form = QFormLayout(box)
            form.addRow(_note(command.description or "此命令无描述"))
            form.addRow(_note(f"执行类型：{EXECUTION_TYPES[command.execution_type]}"))
            remote = is_remote_shell(command)
            has_remote |= remote
            for name in variable_names(command.template):
                field = QLineEdit()
                field.setObjectName(f"parameter_{index}_{name}")
                field.setPlaceholderText("原样传递值；无需再加引号")
                field.textChanged.connect(self.update_preview)
                self.fields[index, name] = field
                label = f"{name}（Android 远程 Shell 脚本，不是普通数据）" if remote else name
                form.addRow(label, field)
            if not variable_names(command.template):
                form.addRow(_note("此命令无需参数。"))
            self.parameter_layout.addWidget(box)
        self.parameter_layout.addStretch()
        self.remote_ack.setVisible(has_remote)
        self.update_preview()

    def update_preview(self) -> None:
        commands = self._selected_commands()
        self.device_label.setText(f"当前设备：{self.page.serial or '未选择'} · {self.page.device_state or '无设备'}（提交时固定）")
        self.prepared = []
        if self.page.runtime_error:
            self.preview.setPlainText(self.page.runtime_error)
            self.settings_label.clear()
            self.run_button.setEnabled(False)
            return
        errors: list[str] = []
        lines: list[str] = []
        settings: list[str] = []
        try:
            program = self.page.runner.resolve("adb")
        except ValueError as exc:
            self.preview.setPlainText(f"ADB 程序不可用：{exc}")
            self.settings_label.clear()
            self.run_button.setEnabled(False)
            return
        for index, command in enumerate(commands):
            settings.append(f"{index + 1}. {EXECUTION_TYPES[command.execution_type]} · {'Server，不指定设备' if command.scope == 'host' else '设备'} · 超时 {command.timeout} 秒 · {'需要 Root（未检测、未授权）' if command.permission == 'root' else '普通用户'}")
            try:
                if command.scope == "device" and self.page.device_state != "device":
                    raise ValueError("设备当前不可用，请选择 state=device 的设备")
                parameters = {name: field.text() for (step, name), field in self.fields.items() if step == index}
                prepared = prepare_command(command, parameters, self.page.serial)
                self.prepared.append(prepared)
                lines.append(f"{index + 1}. {command.name} · {EXECUTION_TYPES[command.execution_type]}\n{command_preview(prepared, program)}")
            except ValueError as exc:
                errors.append(f"{index + 1}. {command.name}：{exc}")
                lines.append(f"{index + 1}. {command.name} · {EXECUTION_TYPES[command.execution_type]}\n{command.template}\n待完善：{exc}")
        self.preview.setPlainText("\n\n".join(lines) or "请选择命令")
        self.settings_label.setText("\n".join(settings))
        remote_ok = not any(is_remote_shell(command) for command in commands) or self.remote_ack.isChecked()
        self.run_button.setEnabled(bool(commands) and not errors and remote_ok)

    def _submit(self) -> None:
        self.update_preview()
        if not self.run_button.isEnabled():
            return
        plan = list(self.prepared)
        root_names = [step.title for step in plan if step.permission == "root"]
        if root_names:
            answer = QMessageBox.warning(self, "需要 Root · 明确确认", "以下命令标记为需要 Root：\n" + "\n".join(root_names) + "\n\n此标记不是权限检测；程序不会自动执行 adb root / su，也不保证设备已有 Root。仍按预览原样执行？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.submitted.emit(plan)
        self.accept()


class CommandLibraryPage(QWidget):
    show_output = Signal(str)

    def __init__(self, runner: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runner = runner
        self.serial = ""
        self.device_state = ""
        self.runtime_error = ""
        self.store = CommandStore()
        self.editing_id: str | None = None
        self.execution_dialog: ExecutionDialog | None = None
        self._latest: dict[str, Task] = {}
        self._library_rows: dict[str, int] = {}
        self._management_rows: dict[str, int] = {}
        self._history_rows: dict[str, int] = {}
        self._command_snapshots: dict[str, Command] = {}
        self._search_text: dict[str, str] = {}
        self._library_members: set[str] = set()
        self._management_members: set[str] = set()
        self._visible_commands: set[str] = set()
        self._history_dirty = True
        for task in [*runner.history, *runner.tasks.values()]:
            if task.command_id and not task.transient:
                self._latest[task.command_id] = task
        layout = ui_kit.page_layout(QVBoxLayout(self))
        self.storage_error = ui_kit.set_role(_note(self.store.error), "error")
        self.storage_error.setObjectName("commandStorageError")
        self.storage_error.setVisible(bool(self.store.error))
        layout.addWidget(self.storage_error)
        self.recover_button = QPushButton("备份原文件并恢复默认命令库…")
        self.recover_button.setVisible(bool(self.store.error))
        self.recover_button.clicked.connect(self._recover)
        layout.addWidget(self.recover_button)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("commandTabs")
        layout.addWidget(self.tabs)
        self._build_library()
        self._build_editor()
        self._build_command_management()
        self._build_history()
        self.tabs.currentChanged.connect(self._tab_changed)
        runner.task_added.connect(self._task_added)
        runner.task_changed.connect(self._task_update)
        runner.history_changed.connect(self._refresh_history)
        self._refresh_all()
        self._load_editor(None)

    def _build_library(self) -> None:
        page = QWidget()
        layout = ui_kit.tab_layout(QVBoxLayout(page))
        row = QHBoxLayout()
        row.addWidget(QLabel("当前设备"))
        self.device_input = QLineEdit("未选择设备")
        self.device_input.setReadOnly(True)
        row.addWidget(self.device_input, 1)
        self.run_selected = _button("执行所选", lambda: self.open_execution(self._selected_id()), row)
        _button("快速执行 Ctrl+K", self.open_execution, row)
        self.manage_button = _button("管理命令", self._open_management, row)
        layout.addLayout(row)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("commandSearch")
        self.search.setPlaceholderText("搜索名称、描述、标签或模板")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh_library)
        self.search.textChanged.connect(lambda: self._search_timer.start())
        filters.addWidget(self.search, 1)
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部分类", "")
        self.category_filter.currentIndexChanged.connect(self._refresh_library)
        filters.addWidget(self.category_filter)
        self.execution_type_filter = QComboBox()
        self.execution_type_filter.setObjectName("commandExecutionTypeFilter")
        self.execution_type_filter.addItem("全部类型", "")
        for key, label in EXECUTION_TYPES.items():
            self.execution_type_filter.addItem(label, key)
        self.execution_type_filter.currentIndexChanged.connect(self._refresh_library)
        filters.addWidget(self.execution_type_filter)
        self.favorites_only = QCheckBox("仅收藏")
        self.favorites_only.toggled.connect(self._refresh_library)
        filters.addWidget(self.favorites_only)
        layout.addLayout(filters)
        self.count_label = ui_kit.set_role(QLabel(), "hint")
        layout.addWidget(self.count_label)
        self.table = _table(["收藏", "名称", "分类", "执行类型", "模式", "命令摘要", "最近状态"], "commandTable")
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(48)
        for column, width in enumerate((48, 144, 72, 88, 56, 350, 88)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemChanged.connect(self._library_item_changed)
        self.table.cellDoubleClicked.connect(lambda row, col: self.open_execution(self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)) if col else None)
        self.library_tools = TableTools(self.table, export_name="commands", menu=self._library_menu,
                                        refresh=self._refresh_library, search=self.search,
                                        text=_check_text(self.table))
        layout.addWidget(self.table, 1)
        ui_kit.install_empty_state(self.table, lambda: "没有可显示的命令\n调整搜索、分类或「仅收藏」筛选，或点击「新建命令」")
        bottom = QHBoxLayout()
        self.selection_label = _note("选择命令后可执行；双击名称打开参数预览。")
        bottom.addWidget(self.selection_label, 1)
        self.new_command_button = _button("新建命令", lambda: self.edit_command(), bottom)
        self.edit_selected = _button("编辑所选", lambda: self.edit_command(self._selected_id()), bottom)
        layout.addLayout(bottom)
        self.tabs.addTab(page, "命令库")

    def _build_editor(self) -> None:
        page = QWidget()
        layout = ui_kit.tab_layout(QVBoxLayout(page))
        header = QHBoxLayout()
        self.editor_title = QLabel("创建命令")
        header.addWidget(self.editor_title, 1)
        self.editor_new_button = _button("新建命令", lambda: self.edit_command(), header)
        layout.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(12)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setSpacing(6)
        content_layout.addLayout(form)
        self.name_field = QLineEdit()
        self.name_field.setMaxLength(100)
        self.category_field = QComboBox()
        self.category_field.setEditable(True)
        self.category_field.addItems(list(CATEGORIES))
        self.execution_type_field = QComboBox()
        self.execution_type_field.setObjectName("commandExecutionType")
        for key, label in EXECUTION_TYPES.items():
            self.execution_type_field.addItem(label, key)
        self.template_field = QPlainTextEdit()
        self.template_field.setObjectName("commandTemplate")
        self.template_field.setPlaceholderText('adb push "C:\\My Files\\file.txt" {path}')
        self.template_field.setMaximumHeight(110)
        self.description_field = QLineEdit()
        self.tags_field = QLineEdit()
        self.tags_field.setPlaceholderText("逗号分隔，例如：调试,属性")
        self.scope_field = QComboBox()
        self.scope_field.addItem("设备命令（指定 Serial）", "device")
        self.scope_field.addItem("ADB Server 命令（不加 Serial）", "host")
        self.timeout_field = QSpinBox()
        self.timeout_field.setRange(1, 3600)
        self.timeout_field.setSuffix(" 秒")
        self.permission_field = QComboBox()
        self.permission_field.addItem("普通用户", "user")
        self.permission_field.addItem("需要 Root（执行前明确确认）", "root")
        self.favorite_field = QCheckBox("收藏此命令")
        self.show_in_library_field = QCheckBox("在命令库显示")
        self.show_in_library_field.setObjectName("commandShowInLibrary")
        for label, field in (("名称", self.name_field), ("分类", self.category_field), ("执行类型", self.execution_type_field), ("命令模板", self.template_field), ("描述", self.description_field), ("标签", self.tags_field), ("模式", self.scope_field), ("超时", self.timeout_field), ("权限", self.permission_field)):
            form.addRow(label, field)
        form.addRow(self.favorite_field)
        form.addRow(self.show_in_library_field)
        help_box = QGroupBox("填写说明")
        help_box.setObjectName("commandEditorHelp")
        help_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        help_form = QFormLayout(help_box)
        help_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        help_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        help_form.setVerticalSpacing(8)
        self.editor_preview = _note("")
        for heading, note in (
            ("模板参数", self.editor_preview),
            ("显示范围", _note("只影响命令库和单命令快速选择，不是权限控制。\n隐藏后需在“命令管理”重新开启显示；不取消已提交的任务。")),
            ("执行类型", _note("仅用于分类，独立于功能分类、模式、权限和超时，不改变超时或停止行为。\nShell 脚本请手动选择类型，无法可靠推断；修改模板不会自动更改类型。")),
            ("参数规则", _note("用 {变量名} 定义参数，名称不能含空白或花括号。\n单 / 双引号用于分组，组内双写引号表示字面引号；反斜杠原样保留。\n普通参数直接填写数据，无需自行加引号。\n远程 Shell 脚本只用 adb shell {command}，执行前须明确确认。")),
        ):
            title = QLabel(heading)
            title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            ui_kit.set_role(title, "section")
            note.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            note.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ui_kit.set_role(note, "hint")
            help_form.addRow(title, note)
        content_layout.addWidget(help_box)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        _button("取消", lambda: self.tabs.setCurrentIndex(0), actions)
        self.save_command_button = _button("保存命令", self._save_command, actions)
        layout.addLayout(actions)
        self.template_field.textChanged.connect(self._editor_preview)
        self.permission_field.currentIndexChanged.connect(self._editor_preview)
        self.tabs.addTab(page, "创建/编辑命令")

    def _build_command_management(self) -> None:
        page = QWidget()
        layout = ui_kit.tab_layout(QVBoxLayout(page))
        layout.addWidget(ui_kit.info_note("取消显示不会删除命令，也不是权限控制。", "管理全部已保存命令。取消显示不会删除命令；隐藏命令需先启用显示才能执行。这不是权限控制，也不会取消已提交的任务。"))
        self.management_search = QLineEdit()
        self.management_search.setObjectName("commandManagementSearch")
        self.management_search.setPlaceholderText("搜索名称、描述、标签或模板")
        self._management_search_timer = QTimer(self)
        self._management_search_timer.setSingleShot(True)
        self._management_search_timer.setInterval(150)
        self._management_search_timer.timeout.connect(self._refresh_management)
        self.management_search.textChanged.connect(lambda: self._management_search_timer.start())
        layout.addWidget(self.management_search)
        self.management_count = QLabel()
        layout.addWidget(self.management_count)
        self.management_table = _table(["在命令库显示", "名称", "分类", "执行类型", "模式", "命令模板"], "commandManagementTable")
        self.management_table.horizontalHeader().setStretchLastSection(False)
        self.management_table.horizontalHeader().setMinimumSectionSize(48)
        for column, width in enumerate((115, 144, 72, 88, 56, 350)):
            self.management_table.setColumnWidth(column, width)
        self.management_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.management_table.itemSelectionChanged.connect(self._management_selection_changed)
        self.management_table.itemChanged.connect(self._management_item_changed)
        self.management_table.cellDoubleClicked.connect(self._edit_managed_row)
        self.management_tools = TableTools(self.management_table, export_name="commands-all",
                                           menu=self._management_menu, refresh=self._refresh_management,
                                           search=self.management_search,
                                           text=_check_text(self.management_table))
        layout.addWidget(self.management_table, 1)
        ui_kit.install_empty_state(self.management_table, lambda: "没有匹配的命令\n调整搜索词，或点击「新建命令」")
        actions = QHBoxLayout()
        self.management_new_button = _button("新建命令", lambda: self.edit_command(), actions)
        self.management_edit_button = _button("编辑所选", lambda: self.edit_command(self._managed_id()), actions)
        self.duplicate_button = _button("复制命令", lambda: self._duplicate(self._managed_id()), actions)
        self.delete_button = _button("删除命令", lambda: self._delete(self._managed_id()), actions)
        actions.addStretch()
        self.import_button = _button("导入 JSON…", self.import_commands, actions)
        self.export_button = _button("导出 JSON…", self.export_commands, actions)
        self.import_button.setToolTip("合并其他 SysDroid 导出的命令和工作流；同 ID 内容不同时询问是否覆盖")
        self.export_button.setToolTip("导出全部命令和工作流，可在其他电脑导入")
        layout.addLayout(actions)
        self.tabs.addTab(page, "命令管理")

    def _build_history(self) -> None:
        page = QWidget()
        layout = ui_kit.tab_layout(QVBoxLayout(page))
        toolbar = QHBoxLayout()
        toolbar.addWidget(ui_kit.info_note("清空只删除已结束的执行历史，不会停止任务。", "来自真实任务执行器；包含运行中的任务。清空仅删除已结束的 ADB 执行历史，不停止任务，也不清除当前会话的任务输出。"), 1)
        self.history_output_button = _button("查看所选输出", self._show_history_output, toolbar)
        self.history_output_button.setEnabled(False)
        self.clear_history_button = _button("清空历史", self._clear_history, toolbar)
        self.clear_history_button.setObjectName("clearExecutionHistory")
        layout.addLayout(toolbar)
        self.history_table = _table(["时间", "命令", "设备", "状态", "退出码"], "commandHistory")
        self.history_table.setColumnWidth(0, 150)
        self.history_table.setColumnWidth(1, 350)
        self.history_table.setColumnWidth(2, 150)
        self.history_table.horizontalHeader().setStretchLastSection(False)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history_table.cellDoubleClicked.connect(lambda row, col: self._show_history_output())
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        self.history_tools = TableTools(self.history_table, export_name="history", menu=self._history_menu,
                                        refresh=self._refresh_history)
        layout.addWidget(self.history_table, 1)
        ui_kit.install_empty_state(self.history_table, lambda: "暂无执行历史\n在命令库执行命令后会显示在这里")
        self.tabs.addTab(page, "执行历史")

    @property
    def table_tools(self) -> TableTools | None:
        """The table that answers F5 / Ctrl+F for the visible tab."""
        return {0: self.library_tools, 2: self.management_tools, 3: self.history_tools}.get(self.tabs.currentIndex())

    def _library_menu(self, menu, row: int) -> None:
        if self.table.currentRow() != row:
            self.table.selectRow(row)
        command_id = self._selected_id()
        if command_id is None:
            return
        menu.addAction("执行…", lambda: self.open_execution(command_id))
        menu.addAction("编辑", lambda: self.edit_command(command_id))

    def _management_menu(self, menu, row: int) -> None:
        if self.management_table.currentRow() != row:
            self.management_table.selectRow(row)
        command_id = self._managed_id()
        if command_id is None:
            return
        menu.addAction("编辑", lambda: self.edit_command(command_id))
        menu.addAction("复制为新命令", lambda: self._duplicate(command_id))

    def _history_menu(self, menu, row: int) -> None:
        if self.history_table.currentRow() != row:
            self.history_table.selectRow(row)
        if self._history_id() is None:
            return
        menu.addAction("查看输出", self._show_history_output)
        item = self.history_table.item(row, 1)
        command = item.toolTip() if item is not None else ""
        if command:
            menu.addAction("复制命令", lambda: QApplication.clipboard().setText(command))

    def set_runtime_error(self, message: str) -> None:
        self.runtime_error = message
        self._selection_changed()
        if self.execution_dialog:
            self.execution_dialog.update_preview()

    def set_device(self, serial: str, state: str = "device") -> None:
        self.serial = serial
        self.device_state = state
        self.device_input.setText(f"{serial} · {state}" if serial else "未选择设备")
        if self.execution_dialog:
            self.execution_dialog.update_preview()

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 and not self.table.isRowHidden(row) else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _managed_id(self) -> str | None:
        row = self.management_table.currentRow()
        item = self.management_table.item(row, 1) if row >= 0 and not self.management_table.isRowHidden(row) else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _open_management(self, command_id: str | None = None) -> None:
        if not isinstance(command_id, str):
            command_id = self._selected_id()
        command = self.store.commands.get(command_id)
        query = self.management_search.text().strip().casefold()
        if command and query not in f"{command.name} {command.description} {command.tags} {command.template}".casefold():
            self.management_search.blockSignals(True)
            self.management_search.clear()
            self.management_search.blockSignals(False)
        self._refresh_management(selected_id=command_id)
        self.tabs.setCurrentIndex(2)

    def _management_selection_changed(self) -> None:
        enabled = self._managed_id() in self.store.commands and not self.store.error
        for button in (self.management_edit_button, self.duplicate_button, self.delete_button):
            button.setEnabled(enabled)

    def _edit_managed_row(self, row: int, column: int) -> None:
        if column and not self.store.error:
            item = self.management_table.item(row, 1)
            if item:
                self.edit_command(item.data(Qt.ItemDataRole.UserRole))

    def _refresh_management(self, *unused, selected_id: str | None = None) -> None:
        selected_id = selected_id or self._managed_id()
        self._management_search_timer.stop()
        self._sync_command_tables()
        query = self.management_search.text().strip().casefold()
        with QSignalBlocker(self.management_table):
            for command_id, row in self._management_rows.items():
                matches = query in self._search_text[command_id]
                self.management_table.setRowHidden(row, not matches)
                if matches:
                    self._management_members.add(command_id)
                else:
                    self._management_members.discard(command_id)
            self._restore_selection(self.management_table, self._management_rows, selected_id)
        self._update_counts()
        self._management_selection_changed()

    def _management_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._set_visibility(item.data(Qt.ItemDataRole.UserRole), item.checkState() == Qt.CheckState.Checked)

    def _library_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._favorite(item.data(Qt.ItemDataRole.UserRole), item.checkState() == Qt.CheckState.Checked)

    def _set_visibility(self, command_id: str, checked: bool) -> None:
        self._set_command_flag(command_id, "show_in_library", checked)

    def _set_command_flag(self, command_id: str, field: str, checked: bool) -> None:
        command = self.store.commands.get(command_id)
        if command is None:
            self._sync_command_tables()
            return
        error = None
        if getattr(command, field) != checked:
            try:
                self.store.save_command(replace(command, **{field: checked}))
            except (ValueError, StorageError) as exc:
                error = str(exc)
        self._command_changed(command_id)
        if error:
            self._error(error)

    @staticmethod
    def _set_cell(table: QTableWidget, row: int, column: int, text: str, tooltip: str | None = None) -> QTableWidgetItem:
        item = table.item(row, column)
        if item is None:
            item = QTableWidgetItem(text)
            table.setItem(row, column, item)
        elif item.text() != text:
            item.setText(text)
        tooltip = text if tooltip is None else tooltip
        if item.toolTip() != tooltip:
            item.setToolTip(tooltip)
        return item

    def _set_check_cell(self, table: QTableWidget, row: int, command: Command, checked: bool, label: str) -> None:
        item = self._set_cell(table, row, 0, "", f"{label} {command.name}（空格键切换）")
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        if not self.store.error:
            flags |= Qt.ItemFlag.ItemIsEnabled
        if item.flags() != flags:
            item.setFlags(flags)
        if item.data(Qt.ItemDataRole.UserRole) != command.id:
            item.setData(Qt.ItemDataRole.UserRole, command.id)
        accessible = f"{label} {command.name}"
        if item.data(Qt.ItemDataRole.AccessibleTextRole) != accessible:
            item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        if item.checkState() != state:
            item.setCheckState(state)

    def _sync_command_row(self, command: Command) -> None:
        self._search_text[command.id] = f"{command.name} {command.description} {command.tags} {command.template}".casefold()
        if command.show_in_library:
            self._visible_commands.add(command.id)
        else:
            self._visible_commands.discard(command.id)
        for table, rows, library in ((self.table, self._library_rows, True), (self.management_table, self._management_rows, False)):
            with QSignalBlocker(table):
                row = rows.get(command.id)
                if row is None:
                    row = table.rowCount()
                    table.insertRow(row)
                    rows[command.id] = row
                self._set_check_cell(table, row, command, command.favorite if library else command.show_in_library, "收藏" if library else "在命令库显示")
                values = (command.name, command.category, EXECUTION_TYPES[command.execution_type], "Server" if command.scope == "host" else "设备", command.template)
                if library:
                    last = self._latest.get(command.id)
                    values += (STATUS_LABELS.get(last.status, last.status) if last else "未执行",)
                for column, value in enumerate(values, 1):
                    self._set_cell(table, row, column, value)
                item = table.item(row, 1)
                if item.data(Qt.ItemDataRole.UserRole) != command.id:
                    item.setData(Qt.ItemDataRole.UserRole, command.id)
        self._command_snapshots[command.id] = command

    def _remove_command_row(self, command_id: str) -> None:
        for table, rows in ((self.table, self._library_rows), (self.management_table, self._management_rows)):
            row = rows.pop(command_id, None)
            if row is not None:
                with QSignalBlocker(table):
                    table.removeRow(row)
                for other_id, other_row in rows.items():
                    if other_row > row:
                        rows[other_id] = other_row - 1
        self._command_snapshots.pop(command_id, None)
        self._search_text.pop(command_id, None)
        self._visible_commands.discard(command_id)
        self._library_members.discard(command_id)
        self._management_members.discard(command_id)

    def _sync_command_tables(self) -> None:
        for command_id in self._command_snapshots.keys() - self.store.commands.keys():
            self._remove_command_row(command_id)
        for command in self.store.commands.values():
            if self._command_snapshots.get(command.id) != command:
                self._sync_command_row(command)

    def _library_matches(self, command: Command) -> bool:
        category = self.category_filter.currentData()
        execution_type = self.execution_type_filter.currentData()
        return (command.show_in_library and (not category or command.category == category)
                and (not execution_type or command.execution_type == execution_type)
                and (not self.favorites_only.isChecked() or command.favorite)
                and self.search.text().strip().casefold() in self._search_text[command.id])

    @staticmethod
    def _restore_selection(table: QTableWidget, rows: dict[str, int], selected_id: str | None) -> None:
        if selected_id in rows and not table.isRowHidden(rows[selected_id]):
            if table.currentRow() != rows[selected_id]:
                table.setCurrentCell(rows[selected_id], 1)
        elif selected_id is not None or (table.currentRow() >= 0 and table.isRowHidden(table.currentRow())):
            table.clearSelection()
            table.setCurrentCell(-1, -1)

    def _update_counts(self) -> None:
        total = len(self.store.commands)
        visible_count = len(self._visible_commands)
        self.count_label.setText(f"筛选显示 {len(self._library_members)} / {visible_count} 条命令 · 隐藏 {total - visible_count} 条（在命令管理中设置）")
        self.management_count.setText(f"共 {total} 条命令 · 命令库显示 {visible_count} 条 · 隐藏 {total - visible_count} 条 · 搜索结果 {len(self._management_members)} 条")

    def _command_changed(self, command_id: str, *, selected_id: str | None = None, managed_id: str | None = None) -> None:
        selected_id = selected_id or self._selected_id()
        managed_id = managed_id or self._managed_id()
        old = self._command_snapshots.get(command_id)
        command = self.store.commands.get(command_id)
        if command is None:
            self._remove_command_row(command_id)
        else:
            self._sync_command_row(command)
            library_matches = self._library_matches(command)
            management_matches = self.management_search.text().strip().casefold() in self._search_text[command_id]
            for table, rows, members, matches in ((self.table, self._library_rows, self._library_members, library_matches), (self.management_table, self._management_rows, self._management_members, management_matches)):
                with QSignalBlocker(table):
                    table.setRowHidden(rows[command_id], not matches)
                if matches:
                    members.add(command_id)
                else:
                    members.discard(command_id)
        if old is None or command is None or old.category != command.category:
            self._refresh_categories()
        for table, rows, selection in ((self.table, self._library_rows, selected_id), (self.management_table, self._management_rows, managed_id)):
            with QSignalBlocker(table):
                self._restore_selection(table, rows, selection)
        self._update_counts()
        self._selection_changed()
        self._management_selection_changed()

    def _selection_changed(self) -> None:
        command = self.store.commands.get(self._selected_id())
        self.run_selected.setEnabled(command is not None and not self.runtime_error)
        self.edit_selected.setEnabled(command is not None and not self.store.error)
        self.selection_label.setText(f"{command.name} · {EXECUTION_TYPES[command.execution_type]} · {command.description or '无描述'} · 超时 {command.timeout} 秒" if command else "选择命令后可执行；双击名称打开参数预览。")

    def _refresh_categories(self) -> None:
        category = self.category_filter.currentData()
        categories = sorted(set(CATEGORIES) | {command.category for command in self.store.commands.values()})
        if categories == [self.category_filter.itemData(index) for index in range(1, self.category_filter.count())]:
            return
        with QSignalBlocker(self.category_filter):
            self.category_filter.clear()
            self.category_filter.addItem("全部分类", "")
            for value in categories:
                self.category_filter.addItem(value, value)
            self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(category)))
        if category != self.category_filter.currentData():
            self._refresh_library()

    def _refresh_all(self, selected_id: str | None = None, managed_id: str | None = None) -> None:
        self._refresh_categories()
        self._refresh_library(selected_id=selected_id)
        self._refresh_management(selected_id=managed_id)
        self._refresh_history()
        for widget in (self.save_command_button, self.new_command_button, self.editor_new_button, self.management_new_button, self.import_button, self.export_button, self.name_field, self.category_field, self.execution_type_field, self.template_field, self.description_field, self.tags_field, self.scope_field, self.timeout_field, self.permission_field, self.favorite_field, self.show_in_library_field):
            widget.setEnabled(not self.store.error)
        for command in self.store.commands.values():
            self._sync_command_row(command)

    def _refresh_library(self, *unused, selected_id: str | None = None) -> None:
        selected_id = selected_id or self._selected_id()
        self._search_timer.stop()
        self._sync_command_tables()
        with QSignalBlocker(self.table):
            for command_id, row in self._library_rows.items():
                matches = self._library_matches(self.store.commands[command_id])
                self.table.setRowHidden(row, not matches)
                if matches:
                    self._library_members.add(command_id)
                else:
                    self._library_members.discard(command_id)
            self._restore_selection(self.table, self._library_rows, selected_id)
        self._update_counts()
        self._selection_changed()

    def _favorite(self, command_id: str, checked: bool) -> None:
        self._set_command_flag(command_id, "favorite", checked)

    def edit_command(self, command_id: str | None = None) -> None:
        if self.store.error:
            return
        self._load_editor(command_id)
        self.tabs.setCurrentIndex(1)
        self.name_field.setFocus()

    def _load_editor(self, command_id: str | None) -> None:
        command = self.store.commands.get(command_id)
        self.editing_id = command.id if command else None
        self.editor_title.setText(f"编辑命令：{command.name}" if command else "创建命令")
        self.name_field.setText(command.name if command else "")
        self.category_field.setCurrentText(command.category if command else "设备")
        self.execution_type_field.setCurrentIndex(self.execution_type_field.findData(command.execution_type if command else "quick"))
        self.template_field.setPlainText(command.template if command else "")
        self.description_field.setText(command.description if command else "")
        self.tags_field.setText(command.tags if command else "")
        self.scope_field.setCurrentIndex(self.scope_field.findData(command.scope if command else "device"))
        self.timeout_field.setValue(command.timeout if command else 10)
        self.permission_field.setCurrentIndex(self.permission_field.findData(command.permission if command else "user"))
        self.favorite_field.setChecked(command.favorite if command else False)
        self.show_in_library_field.setChecked(command.show_in_library if command else True)
        self._editor_preview()

    def _editor_preview(self) -> None:
        variables = variable_names(self.template_field.toPlainText())
        self.editor_preview.setText(("执行前逐项填写：" + "、".join(variables) if variables else "此模板不包含参数。") + ("\n需要 Root：仅为注释，执行前明确确认，不会自动提权。" if self.permission_field.currentData() == "root" else ""))

    def _save_command(self) -> None:
        command = Command(self.editing_id or uuid.uuid4().hex, self.name_field.text().strip(), self.category_field.currentText().strip(), self.template_field.toPlainText().strip(), self.description_field.text(), self.tags_field.text(), self.scope_field.currentData(), self.timeout_field.value(), self.permission_field.currentData(), self.favorite_field.isChecked(), execution_type=self.execution_type_field.currentData(), show_in_library=self.show_in_library_field.isChecked())
        try:
            self.store.save_command(command)
        except (ValueError, StorageError) as exc:
            self._error(str(exc))
            return
        self.editing_id = command.id
        if command.show_in_library:
            with QSignalBlocker(self.search), QSignalBlocker(self.category_filter), QSignalBlocker(self.execution_type_filter):
                self.search.clear()
                self.category_filter.setCurrentIndex(0)
                self.execution_type_filter.setCurrentIndex(0)
            self._command_changed(command.id, selected_id=command.id, managed_id=command.id)
            self._refresh_library(selected_id=command.id)
            self.tabs.setCurrentIndex(0)
        else:
            self._command_changed(command.id, managed_id=command.id)
            self._open_management(command.id)


    def _duplicate(self, command_id: str | None = None) -> None:
        command = self.store.commands.get(command_id or self._selected_id())
        if command:
            duplicate = replace(command, id=uuid.uuid4().hex, name=command.name[:97] + " 副本")
            try:
                self.store.save_command(duplicate)
            except (ValueError, StorageError) as exc:
                self._error(str(exc))
                return
            self._command_changed(duplicate.id, selected_id=duplicate.id, managed_id=duplicate.id)
            if command_id or not duplicate.show_in_library:
                self._open_management(duplicate.id)

    def _delete(self, command_id: str | None = None) -> None:
        command = self.store.commands.get(command_id or self._selected_id())
        if not command:
            return
        references = sum(workflow.steps.count(command.id) for workflow in self.store.workflows.values())
        answer = QMessageBox.question(self, "删除命令", f"删除“{command.name}”？引用它的 {references} 个旧版已保存工作流步骤也会移除。\n已经提交的任务不受影响。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete_command(command.id)
        except (ValueError, StorageError) as exc:
            self._error(str(exc))
            return
        if self.editing_id == command.id:
            self._load_editor(None)
        self._command_changed(command.id)

    def open_execution(self, command_id: str | None = None) -> None:
        if self.runtime_error:
            self.storage_error.setText(self.runtime_error)
            self.storage_error.show()
            return
        if self.execution_dialog and self.execution_dialog.isVisible():
            self.execution_dialog.raise_()
            self.execution_dialog.activateWindow()
            return
        commands = [command for command in self.store.commands.values() if command.show_in_library]
        if not commands:
            self._error(self.store.error or ("所有命令均已隐藏，请在命令管理中启用“在命令库显示”。" if self.store.commands else "请先创建命令，可在命令管理中新建。"))
            return
        if not isinstance(command_id, str):
            command_id = self._selected_id()
        dialog = ExecutionDialog(self, commands, command_id)
        dialog.submitted.connect(self._execute_single)
        self._open_dialog(dialog)

    def _open_dialog(self, dialog: ExecutionDialog) -> None:
        self.execution_dialog = dialog
        dialog.finished.connect(lambda result: self._close_dialog(dialog))
        dialog.open()

    def _close_dialog(self, dialog: ExecutionDialog) -> None:
        if self.execution_dialog is dialog:
            self.execution_dialog = None
        dialog.deleteLater()

    def _execute_single(self, plan: list[PreparedCommand]) -> None:
        step = plan[0]
        task = self.runner.start_adb(step.title, list(step.args), serial=step.serial, command_id=step.command_id,
                                     timeout=step.timeout, source="command")
        self.show_output.emit(task.id)

    def _task_added(self, task: Task) -> None:
        if task.transient:
            return
        if task.command_id:
            self._latest[task.command_id] = task
        self._task_update(task)

    def _task_update(self, task: Task) -> None:
        if task.transient:
            return
        latest = self._latest.get(task.command_id)
        if task.command_id and latest is not None and latest.id == task.id:
            row = self._library_rows.get(task.command_id)
            if row is not None:
                with QSignalBlocker(self.table):
                    self._set_cell(self.table, row, 6, STATUS_LABELS.get(task.status, task.status))
        if task.source != "command":
            return
        if self.tabs.currentIndex() != 3 or not self.isVisible():
            self._history_dirty = True
            return
        row = self._history_rows.get(task.id)
        if row is None:
            self._refresh_history()
        else:
            self._update_history_row(row, task)

    def _tab_changed(self, index: int) -> None:
        if index == 3 and self._history_dirty:
            self._refresh_history()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.tabs.currentIndex() == 3 and self._history_dirty:
            self._refresh_history()

    def _history_id(self) -> str | None:
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _history_selection_changed(self) -> None:
        self.history_output_button.setEnabled(self._history_id() is not None)

    def _show_history_output(self) -> None:
        task_id = self._history_id()
        if task_id is not None:
            self.show_output.emit(task_id)

    def _update_history_row(self, row: int, task: Task) -> None:
        values = (task.started_at.replace("T", " "), task.title, task.serial or "ADB Server", STATUS_LABELS.get(task.status, task.status), "—" if task.exit_code is None else str(task.exit_code))
        for column, value in enumerate(values):
            self._set_cell(self.history_table, row, column, value, task.command if column == 1 else value)
        item = self.history_table.item(row, 0)
        if item.data(Qt.ItemDataRole.UserRole) != task.id:
            item.setData(Qt.ItemDataRole.UserRole, task.id)

    def _index_history_rows(self) -> None:
        self._history_rows = {self.history_table.item(row, 0).data(Qt.ItemDataRole.UserRole): row for row in range(self.history_table.rowCount())}

    def _refresh_history(self) -> None:
        self._history_dirty = True
        if self.tabs.currentIndex() != 3 or not self.isVisible():
            return
        # Only command-library runs belong here; page-internal queries never reach history.
        tasks = {task.id: task for task in [*self.runner.history, *self.runner.active()]
                 if task.kind == "adb" and task.source == "command" and not task.transient}
        self.clear_history_button.setEnabled(any(task.kind == "adb" and task.source == "command" and task.status in TERMINAL_STATUSES
                                                 for task in self.runner.history))
        records = sorted(tasks.values(), key=lambda task: task.started_at, reverse=True)
        selected_id = self._history_id()
        scroll = self.history_table.verticalScrollBar().value()
        with QSignalBlocker(self.history_table):
            for row in sorted((row for task_id, row in self._history_rows.items() if task_id not in tasks), reverse=True):
                self.history_table.removeRow(row)
            self._index_history_rows()
            for row, task in enumerate(records):
                old_row = self._history_rows.get(task.id)
                if old_row is None:
                    self.history_table.insertRow(row)
                    self._update_history_row(row, task)
                    self._index_history_rows()
                elif old_row != row:
                    items = [self.history_table.takeItem(old_row, column) for column in range(self.history_table.columnCount())]
                    self.history_table.removeRow(old_row)
                    self.history_table.insertRow(row)
                    for column, item in enumerate(items):
                        self.history_table.setItem(row, column, item)
                    self._update_history_row(row, task)
                    self._index_history_rows()
                else:
                    self._update_history_row(row, task)
            if selected_id in self._history_rows:
                self.history_table.setCurrentCell(self._history_rows[selected_id], 0)
            elif selected_id is not None:
                self.history_table.clearSelection()
                self.history_table.setCurrentCell(-1, -1)
        self.history_table.verticalScrollBar().setValue(scroll)
        self._history_dirty = False
        self._history_selection_changed()

    def _clear_history(self) -> None:
        answer = QMessageBox.question(self, "清空执行历史", "删除所有已结束的 ADB 执行历史？\n运行中的任务不会停止，当前会话的任务输出也不会清除。此操作不可撤销。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.runner.clear_history("adb")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "清空执行历史失败", f"无法清空执行历史：{exc}")
            return

    def export_commands(self) -> bool:
        if self.store.error:
            self._error(self.store.error)
            return False
        filename, _ = QFileDialog.getSaveFileName(self, "导出命令库", "sysdroid-commands.json", "JSON 文件 (*.json)")
        if not filename:
            return False
        if not filename.lower().endswith(".json"):
            filename += ".json"
        try:
            payload = self.store.export_payload()
            Path(filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, StorageError) as exc:
            self._error(f"导出失败：{exc}")
            return False
        QMessageBox.information(self, "导出命令库", f"已导出 {len(payload['commands'])} 条命令、{len(payload['workflows'])} 个工作流：\n{filename}")
        return True

    def import_commands(self) -> bool:
        if self.store.error:
            self._error(self.store.error)
            return False
        filename, _ = QFileDialog.getOpenFileName(self, "导入命令库", "", "JSON 文件 (*.json)")
        if not filename:
            return False
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
            commands, workflows, _version = parse_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            self._error(f"无法读取导入文件：{exc}")
            return False
        conflicts = [command for command in commands if command.id in self.store.commands and self.store.commands[command.id] != command]
        overwrite = False
        if conflicts:
            names = "\n".join(f"· {command.name}" for command in conflicts[:10]) + ("\n…" if len(conflicts) > 10 else "")
            answer = QMessageBox.question(
                self, "导入命令库",
                f"{len(conflicts)} 条命令与现有命令 ID 相同但内容不同：\n{names}\n\n是：用导入内容覆盖；否：保留现有命令，仅导入新命令。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            overwrite = answer == QMessageBox.StandardButton.Yes
        try:
            stats = self.store.import_payload(payload, overwrite=overwrite)
        except (ValueError, TypeError, StorageError) as exc:
            self._error(f"导入失败，命令库未修改：{exc}")
            return False
        self._refresh_all()
        QMessageBox.information(self, "导入命令库", f"新增 {stats['added']} 条，覆盖 {stats['updated']} 条，跳过 {stats['skipped']} 条；工作流更新 {stats['workflows']} 个（文件含 {len(commands)} 条命令、{len(workflows)} 个工作流）。")
        return True

    def _recover(self) -> None:
        answer = QMessageBox.warning(self, "恢复命令库", "将原命令库改名为唯一备份，恢复默认命令库。不会删除或覆盖原文件内容。继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            backup = self.store.recover_defaults()
        except (OSError, ValueError, StorageError) as exc:
            self._error(str(exc))
            return
        self.storage_error.hide()
        self.recover_button.hide()
        self._refresh_all()
        QMessageBox.information(self, "命令库已恢复", f"已保存默认命令库。原文件备份：{backup or '原文件不存在'}")

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "命令库", message)

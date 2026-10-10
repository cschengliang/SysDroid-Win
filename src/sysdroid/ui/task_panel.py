from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QMessageBox, QHeaderView, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from sysdroid.core.backend import OUTPUT_LIMIT, STATUS_LABELS, TERMINAL_STATUSES, Task, TaskRunner
from sysdroid.ui import kit as ui_kit
from sysdroid.ui.tables import TableTools

# Status filter buckets: label -> statuses (None means all).
STATUS_FILTERS: tuple[tuple[str, frozenset[str] | None], ...] = (
    ("全部状态", None),
    ("进行中", frozenset({"starting", "running", "stopping"})),
    ("成功", frozenset({"succeeded"})),
    ("失败 / 超时", frozenset({"failed", "timed_out"})),
    ("已停止", frozenset({"cancelled"})),
)
KIND_FILTERS: tuple[tuple[str, str | None], ...] = (("全部类型", None), ("ADB 请求", "adb"), ("本地进程", "process"))
_MINIMUM_WIDTHS = (144, 140, 76, 80, 60)


class TaskPanel(QTabWidget):
    def __init__(self, runner: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runner = runner
        self.selected_id = ""
        self._rows: dict[str, int] = {}
        self._dirty_streams: set[str] = set()
        self._rendered = {"stdout": "", "stderr": ""}
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._flush_output)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self.setObjectName("taskPanel")
        tasks_page = QWidget()
        tasks_layout = ui_kit.page_layout(QVBoxLayout(tasks_page), 8, 6)
        toolbar = QHBoxLayout()
        self.summary = ui_kit.set_role(QLabel("暂无活动任务"), "hint")
        toolbar.addWidget(self.summary, 1)
        self.filter_search = QLineEdit()
        self.filter_search.setObjectName("taskFilterSearch")
        self.filter_search.setPlaceholderText("筛选命令 / 设备")
        self.filter_search.setClearButtonEnabled(True)
        self.filter_search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter_search)
        self.status_filter = QComboBox()
        self.status_filter.setObjectName("taskStatusFilter")
        for label, _statuses in STATUS_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.status_filter)
        self.kind_filter = QComboBox()
        self.kind_filter.setObjectName("taskKindFilter")
        for label, _kind in KIND_FILTERS:
            self.kind_filter.addItem(label)
        self.kind_filter.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.kind_filter)
        self.view_button = QPushButton("查看输出")
        self.view_button.clicked.connect(lambda: self.show_task(self.selected_id))
        toolbar.addWidget(self.view_button)
        self.row_stop_button = QPushButton("停止")
        self.row_stop_button.setObjectName("taskRowStop")
        self.row_stop_button.clicked.connect(lambda: self.runner.cancel(self.selected_id))
        toolbar.addWidget(self.row_stop_button)
        self.row_kill_button = QPushButton("强制结束")
        self.row_kill_button.setObjectName("taskRowKill")
        self.row_kill_button.setProperty("force-stop", True)
        self.row_kill_button.clicked.connect(lambda: self._force_stop(self.selected_id))
        toolbar.addWidget(self.row_kill_button)
        clear = QPushButton("清除已结束")
        clear.clicked.connect(self._clear_finished)
        toolbar.addWidget(clear)
        tasks_layout.addLayout(toolbar)
        # Plain items only: per-row button widgets were the panel's main cost.
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("taskTable")
        self.table.setHorizontalHeaderLabels(["命令", "设备", "状态", "已运行", "退出码"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 140), (2, 76), (3, 80), (4, 60)):
            self.table.setColumnWidth(column, width)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header.sectionResized.connect(self._enforce_column_width)
        self.table.itemSelectionChanged.connect(self._select_row)
        self.table.cellDoubleClicked.connect(lambda row, col: self.show_task(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))
        self.table_tools = TableTools(self.table, export_name="tasks", menu=self._row_menu,
                                      search=self.filter_search)
        tasks_layout.addWidget(self.table)
        ui_kit.install_empty_state(self.table, lambda: "暂无任务\n执行命令、刷新设备或启动投屏后会显示在这里")
        self.addTab(tasks_page, "活动任务")
        output_page = QWidget()
        output_layout = ui_kit.page_layout(QVBoxLayout(output_page), 8, 6)
        output_toolbar = QHBoxLayout()
        self.info = QLabel("选择任务或历史记录查看输出")
        self.info.setWordWrap(True)
        output_toolbar.addWidget(self.info, 1)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(lambda: runner.cancel(self.selected_id))
        self.kill_button = QPushButton("强制结束")
        self.kill_button.clicked.connect(lambda: self._force_stop(self.selected_id))
        self.copy_button = QPushButton("复制输出")
        self.copy_button.clicked.connect(self._copy)
        self.save_button = QPushButton("保存到文件…")
        self.save_button.setObjectName("taskSaveOutput")
        self.save_button.clicked.connect(lambda: self.save_output())
        for button in (self.stop_button, self.kill_button, self.copy_button, self.save_button):
            output_toolbar.addWidget(button)
        output_layout.addLayout(output_toolbar)
        find_row = QHBoxLayout()
        self.output_search = QLineEdit()
        self.output_search.setObjectName("taskOutputSearch")
        self.output_search.setPlaceholderText("在输出中查找（Enter 下一个，Shift+Enter 上一个）")
        self.output_search.setClearButtonEnabled(True)
        self.output_search.returnPressed.connect(lambda: self.find_output(backward=bool(
            QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)))
        self.output_search.textChanged.connect(lambda: self.find_output(restart=True))
        find_row.addWidget(self.output_search, 1)
        self.find_prev_button = QPushButton("上一个")
        self.find_prev_button.clicked.connect(lambda: self.find_output(backward=True))
        find_row.addWidget(self.find_prev_button)
        self.find_next_button = QPushButton("下一个")
        self.find_next_button.clicked.connect(lambda: self.find_output())
        find_row.addWidget(self.find_next_button)
        self.find_label = ui_kit.set_role(QLabel(""), "hint")
        self.find_label.setObjectName("taskFindStatus")
        find_row.addWidget(self.find_label)
        self.wrap_toggle = QCheckBox("自动换行")
        self.wrap_toggle.setObjectName("taskWrap")
        self.wrap_toggle.toggled.connect(self._set_wrap)
        find_row.addWidget(self.wrap_toggle)
        output_layout.addLayout(find_row)
        self.command = QLabel()
        self.command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.command.setWordWrap(True)
        output_layout.addWidget(self.command)
        self.output_splitter = splitter = QSplitter()
        for stream, label in (("stdout", "标准输出 · stdout"), ("stderr", "错误输出 · stderr")):
            pane = QWidget()
            layout = QVBoxLayout(pane)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(QLabel(label))
            editor = QPlainTextEdit()
            editor.setObjectName("task" + stream.capitalize())
            editor.setReadOnly(True)
            editor.setMaximumBlockCount(5000)
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            setattr(self, stream, editor)
            layout.addWidget(editor)
            splitter.addWidget(pane)
        output_layout.addWidget(splitter, 1)
        self.addTab(output_page, "任务输出")
        runner.task_added.connect(self._add_task)
        runner.task_changed.connect(self._update_task)
        runner.task_output.connect(self._append_output)
        runner.task_finished.connect(self._task_finished)
        runner.task_removed.connect(self._task_removed)
        runner.active_count_changed.connect(self._update_summary)
        self.currentChanged.connect(self._visibility_changed)
        self._update_summary()
        self._update_controls()

    def _add_task(self, task: Task, *, explicit: bool = False) -> None:
        if task.id in self._rows or (task.transient and not explicit):
            self._update_summary()
            return
        row = self.table.rowCount()
        self._rows[task.id] = row
        self.table.insertRow(row)
        for col in range(5):
            self.table.setItem(row, col, QTableWidgetItem())
        self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, task.id)
        self._update_task(task)
        if not self.selected_id and not task.transient:
            self.table.selectRow(row)
        self._update_summary()

    def _update_task(self, task: Task) -> None:
        row = self._rows.get(task.id)
        if row is not None:
            values = [task.title, task.serial or "ADB Server", STATUS_LABELS[task.status],
                      f"{task.elapsed:.1f} s", "—" if task.exit_code is None else str(task.exit_code)]
            for col, text in enumerate(values):
                item = self.table.item(row, col)
                if col != 3 or task.status in TERMINAL_STATUSES or not item.text():
                    if item.text() != text:
                        item.setText(text)
                    item.setToolTip(text)
            self.table.item(row, 0).setToolTip(task.command)
            self._filter_row(row, task)
        if task.id == self.selected_id:
            self._update_controls()
        self._update_summary()

    def _select_row(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        if item is not None:
            self._load_task(item.data(Qt.ItemDataRole.UserRole))

    def _load_task(self, task_id: str) -> None:
        task = self.runner.get_task(task_id)
        if not task:
            return
        if self.selected_id != task_id:
            previous_id = self.selected_id
            self.selected_id = task_id
            self.runner.pin_task(task_id)
            self.runner.unpin_task(previous_id)
            self._dirty_streams.update(("stdout", "stderr"))
            # Existing widgets belong to the old selection, even if both outputs are empty.
            self._rendered = {"stdout": None, "stderr": None}
        self._schedule_output()
        self._update_controls()

    def show_task(self, task_id: str) -> None:
        task = self.runner.get_task(task_id)
        if task is None:
            return
        self._add_task(task, explicit=True)
        with QSignalBlocker(self.table):
            self.table.selectRow(self._rows[task_id])
        self._load_task(task_id)
        self.setCurrentIndex(1)

    def _update_controls(self, *, force: bool = False) -> None:
        task = self.runner.get_task(self.selected_id)
        state = (task.id, task.title, task.serial, task.status, task.exit_code, task.command) if task else None
        if not force and hasattr(self, "_control_state") and self._control_state == state:
            return
        self._control_state = state
        active = bool(task and task.status not in TERMINAL_STATUSES)
        self.stop_button.setEnabled(active)
        self.kill_button.setEnabled(active and task is not None and task.kind != "adb")
        self.row_stop_button.setEnabled(active)
        self.row_kill_button.setEnabled(active and task is not None and task.kind != "adb")
        self.copy_button.setEnabled(task is not None)
        self.save_button.setEnabled(task is not None)
        self.view_button.setEnabled(task is not None)
        if task:
            self.info.setText(f"{task.title} · {task.serial or 'ADB Server'} · {STATUS_LABELS[task.status]} · "
                              f"{task.elapsed:.1f} s · 退出码 {task.exit_code if task.exit_code is not None else '—'}")
            self.command.setText(task.command)
        else:
            self.info.setText("选择任务或历史记录查看输出")
            self.command.clear()

    def _append_output(self, task_id: str, stream: str, text: str) -> None:
        if task_id == self.selected_id and stream in self._rendered:
            self._dirty_streams.add(stream)
            self._schedule_output()

    def _task_finished(self, task: Task) -> None:
        if task.id == self.selected_id:
            self._dirty_streams.update(("stdout", "stderr"))
            self._schedule_output()
            self._update_controls()

    def _schedule_output(self) -> None:
        if self.isVisible() and self.currentIndex() == 1 and self._dirty_streams:
            if not self._render_timer.isActive():
                self._render_timer.start()

    @staticmethod
    def _visible_tail(text: str) -> str:
        text = text[-OUTPUT_LIMIT:]
        # A final LF creates a block too; retain at most 5000 blocks.
        lines = text.rsplit("\n", 5000)
        return "\n".join(lines[-5000:]) if len(lines) > 5000 else text

    def _flush_output(self) -> None:
        if not self.isVisible() or self.currentIndex() != 1:
            self._render_timer.stop()
            return
        task = self.runner.get_task(self.selected_id)
        for stream in tuple(self._dirty_streams):
            self._dirty_streams.discard(stream)
            text = self._visible_tail(getattr(task, stream)) if task is not None else ""
            previous = self._rendered[stream]
            if text == previous:
                continue
            editor = getattr(self, stream)
            scrollbar = editor.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum()
            position = scrollbar.value()
            if previous is not None and text.startswith(previous):
                cursor = QTextCursor(editor.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text[len(previous):])
            else:
                editor.setPlainText(text)
            self._rendered[stream] = text
            scrollbar.setValue(scrollbar.maximum() if at_bottom else position)
        if not self._dirty_streams:
            self._render_timer.stop()

    def _visibility_changed(self, *_args) -> None:
        if self.isVisible():
            if self.runner.active_count:
                self._elapsed_timer.start()
            if self.currentIndex() == 1:
                self._flush_output()
                return
        self._render_timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._visibility_changed()

    def hideEvent(self, event) -> None:
        self._render_timer.stop()
        self._elapsed_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "output_splitter"):
            orientation = Qt.Orientation.Vertical if self.contentsRect().width() < 660 else Qt.Orientation.Horizontal
            if self.output_splitter.orientation() != orientation:
                self.output_splitter.setOrientation(orientation)

    def _enforce_column_width(self, column: int, _old: int, width: int) -> None:
        minimum = _MINIMUM_WIDTHS[column]
        if width < minimum:
            self.table.horizontalHeader().resizeSection(column, minimum)

    def _update_summary(self, *_args) -> None:
        self.summary.setText(f"运行中 {self.runner.active_count} · 当前会话 {len(self.runner.tasks)} 条任务")
        if self.isVisible() and self.runner.active_count:
            if not self._elapsed_timer.isActive():
                self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()

    def _update_elapsed(self) -> None:
        for task_id, row in self._rows.items():
            task = self.runner.get_task(task_id)
            if task is not None and task.status not in TERMINAL_STATUSES:
                text = f"{task.elapsed:.1f} s"
                self.table.item(row, 3).setText(text)
                self.table.item(row, 3).setToolTip(text)
        self._update_controls(force=True)

    def _task_removed(self, task_id: str) -> None:
        row = self._rows.pop(task_id, None)
        with QSignalBlocker(self.table):
            if row is not None:
                self.table.removeRow(row)
                for remaining_id, remaining_row in self._rows.items():
                    if remaining_row > row:
                        self._rows[remaining_id] = remaining_row - 1
            if self.selected_id in self._rows:
                self.table.selectRow(self._rows[self.selected_id])
            else:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
        if self.selected_id == task_id and self.runner.get_task(task_id) is None:
            self.selected_id = ""
            self.runner.unpin_task(task_id)
            self._dirty_streams.update(("stdout", "stderr"))
            self._render_timer.stop()
            self._rendered = {"stdout": None, "stderr": None}
            self._schedule_output()
            self._update_controls()
        self._update_summary()

    def _copy(self) -> None:
        task = self.runner.get_task(self.selected_id)
        if task:
            QApplication.clipboard().setText(f"{task.command}\n\nstdout:\n{task.stdout}\n\nstderr:\n{task.stderr}")

    def _force_stop(self, task_id: str) -> None:
        task = self.runner.get_task(task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return
        if task.kind == "adb":
            QMessageBox.information(self, "ADB 请求", "adbutils 无法强制中断已发出的 ADB 请求；已请求停止，将等待请求返回或超时。")
            self.runner.cancel(task_id)
            return
        answer = QMessageBox.warning(self, "强制结束任务",
                                     "立即结束进程及其子进程。录制或写入中的文件可能损坏。继续？",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.cancel(task_id, force=True)

    def _clear_finished(self) -> None:
        selected_id = self.selected_id
        with QSignalBlocker(self.table):
            self.runner.clear_finished()
            if self.runner.get_task(selected_id) is not None:
                self.selected_id = selected_id
                if selected_id in self._rows:
                    self.table.selectRow(self._rows[selected_id])
            else:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
        self._update_controls()
        self._update_summary()

    # -- filters ------------------------------------------------------------
    def _matches(self, task: Task) -> bool:
        statuses = STATUS_FILTERS[max(0, self.status_filter.currentIndex())][1]
        kind = KIND_FILTERS[max(0, self.kind_filter.currentIndex())][1]
        if statuses is not None and task.status not in statuses:
            return False
        if kind is not None and (task.kind == "adb") != (kind == "adb"):
            return False
        query = self.filter_search.text().strip().casefold()
        return not query or query in f"{task.title}\n{task.serial or 'ADB Server'}\n{task.command}".casefold()

    def _filter_row(self, row: int, task: Task) -> None:
        hidden = not self._matches(task)
        if self.table.isRowHidden(row) != hidden:
            self.table.setRowHidden(row, hidden)

    def _apply_filter(self, *_args) -> None:
        for task_id, row in self._rows.items():
            task = self.runner.get_task(task_id)
            if task is not None:
                self._filter_row(row, task)
        visible = sum(not self.table.isRowHidden(row) for row in range(self.table.rowCount()))
        self.table.setToolTip("" if visible == self.table.rowCount() else
                              f"筛选后显示 {visible} / {self.table.rowCount()} 条")

    def _row_menu(self, menu: QMenu, row: int) -> None:
        item = self.table.item(row, 0)
        task = self.runner.get_task(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        if task is None:
            return
        if self.selected_id != task.id:
            self.table.selectRow(row)
        active = task.status not in TERMINAL_STATUSES
        menu.addAction("查看输出", lambda: self.show_task(task.id))
        menu.addAction("停止", lambda: self.runner.cancel(task.id)).setEnabled(active)
        menu.addAction("强制结束…", lambda: self._force_stop(task.id)).setEnabled(active and task.kind != "adb")
        menu.addAction("复制命令", lambda: QApplication.clipboard().setText(task.command))

    # -- output tools -------------------------------------------------------
    def focus_search(self) -> bool:
        """Ctrl+F: output search on the output tab, otherwise the task filter."""
        field = self.output_search if self.currentIndex() == 1 else self.filter_search
        field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        field.selectAll()
        return True

    def _set_wrap(self, wrap: bool) -> None:
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        for editor in (self.stdout, self.stderr):
            editor.setLineWrapMode(mode)

    def find_output(self, *, backward: bool = False, restart: bool = False) -> bool:
        """Find the query in stdout, then stderr, wrapping around; highlights via selection."""
        query = self.output_search.text()
        editors = [self.stdout, self.stderr]
        if not query:
            for editor in editors:
                cursor = editor.textCursor()
                cursor.clearSelection()
                editor.setTextCursor(cursor)
            self.find_label.setText("")
            return False
        self._flush_output()
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        focused = self.stderr if self.stderr.hasFocus() or getattr(self, "_find_in", None) is self.stderr else self.stdout
        order = [focused, *[editor for editor in editors if editor is not focused]]
        if backward:
            order = [focused, *reversed([editor for editor in editors if editor is not focused])]
        for index, editor in enumerate(order):
            if restart or index > 0:
                cursor = editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
                editor.setTextCursor(cursor)
            if editor.find(query, flags):
                self._find_in = editor
                total = sum(editor_text.count(query) for editor_text in
                            (self.stdout.toPlainText(), self.stderr.toPlainText()))
                self.find_label.setText(f"共 {total} 处 · {'stderr' if editor is self.stderr else 'stdout'}")
                return True
        # Wrap around within the first editor.
        cursor = focused.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
        focused.setTextCursor(cursor)
        if focused.find(query, flags):
            self._find_in = focused
            self.find_label.setText("已从头继续")
            return True
        self.find_label.setText("未找到")
        return False

    def output_text(self, task: Task) -> str:
        return (f"# {task.title}\n# 设备：{task.serial or 'ADB Server'} · 状态：{STATUS_LABELS[task.status]} · "
                f"退出码：{'—' if task.exit_code is None else task.exit_code}\n# 命令：{task.command}\n\n"
                f"## stdout\n{task.stdout}\n\n## stderr\n{task.stderr}\n")

    def save_output(self, path: str | None = None) -> str:
        task = self.runner.get_task(self.selected_id)
        if task is None:
            return ""
        if path is None:
            safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in task.title)
            path, _ = QFileDialog.getSaveFileName(self, "保存任务输出", str(Path.home() / f"{safe[:60] or 'task'}.txt"),
                                                  "文本文件 (*.txt *.log);;所有文件 (*)")
            if not path:
                return ""
        try:
            Path(path).write_text(self.output_text(task), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法保存任务输出：{exc}")
            return ""
        return path

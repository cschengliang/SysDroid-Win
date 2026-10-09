from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLabel, QMessageBox,
                               QHeaderView, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from android_backend import OUTPUT_LIMIT, STATUS_LABELS, TERMINAL_STATUSES, Task, TaskRunner


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
        tasks_layout = QVBoxLayout(tasks_page)
        toolbar = QHBoxLayout()
        self.summary = QLabel("暂无活动任务")
        toolbar.addWidget(self.summary, 1)
        self.view_button = QPushButton("查看所选输出")
        self.view_button.clicked.connect(lambda: self.show_task(self.selected_id))
        toolbar.addWidget(self.view_button)
        clear = QPushButton("清除已结束")
        clear.clicked.connect(self._clear_finished)
        toolbar.addWidget(clear)
        tasks_layout.addLayout(toolbar)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("taskTable")
        self.table.setHorizontalHeaderLabels(["命令", "设备", "状态", "已运行", "退出码", "操作"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 140), (2, 76), (3, 80), (4, 60), (5, 160)):
            self.table.setColumnWidth(column, width)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header.sectionResized.connect(self._enforce_column_width)
        self.table.itemSelectionChanged.connect(self._select_row)
        self.table.cellDoubleClicked.connect(lambda row, col: self.show_task(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))
        tasks_layout.addWidget(self.table)
        self.addTab(tasks_page, "活动任务")
        output_page = QWidget()
        output_layout = QVBoxLayout(output_page)
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
        for button in (self.stop_button, self.kill_button, self.copy_button):
            output_toolbar.addWidget(button)
        output_layout.addLayout(output_toolbar)
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
        controls = QWidget()
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        for label, callback in (("输出", lambda: self.show_task(task.id)),
                                ("停止", lambda: self.runner.cancel(task.id)),
                                ("强杀", lambda: self._force_stop(task.id))):
            button = QPushButton(label)
            button.setProperty("force-stop", label == "强杀")
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.table.setCellWidget(row, 5, controls)
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
            controls = self.table.cellWidget(row, 5).findChildren(QPushButton)
            for button in controls[1:]:
                force_stop = bool(button.property("force-stop"))
                button.setEnabled(task.status not in TERMINAL_STATUSES and not (force_stop and task.kind == "adb"))
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
        self.copy_button.setEnabled(task is not None)
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
        minimum = (144, 140, 76, 80, 60, 160)[column]
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

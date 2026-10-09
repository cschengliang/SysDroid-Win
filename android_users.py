from __future__ import annotations

import re
import shlex

from PySide6.QtCore import QObject, Signal

from android_backend import Task, TaskRunner


def parse_users(output: str) -> dict[int, str]:
    lines = output.splitlines()
    if not lines or lines[0].strip() != "Users:":
        raise ValueError("pm list users 输出缺少 Users 标头")
    users: dict[int, str] = {}
    for line in lines[1:]:
        match = re.fullmatch(r"\s*UserInfo\{([0-9]+):(.+):[0-9a-fA-F]+\}(?:\s+running)?\s*", line)
        if not match or int(match[1]) in users:
            raise ValueError(f"用户记录无效或重复：{line!r}")
        users[int(match[1])] = match[2]
    if not users:
        raise ValueError("设备未提供可用 Android 用户")
    return users


class AndroidUserController(QObject):
    changed = Signal()

    def __init__(self, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self.serial = ""
        self.device_state = ""
        self.users: dict[int, str] = {}
        self.current_user: int | None = None
        self.error = ""
        self._generation = 0
        self._loaded = False
        self._task_id = ""
        self._submitting_generation: int | None = None
        self._phase = ""
        self._pending_users: dict[int, str] = {}
        runner.task_finished.connect(self._finished)

    @property
    def busy(self) -> bool:
        return self._submitting_generation == self._generation or bool(self._task_id)

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) == (self.serial, self.device_state):
            return
        self._generation += 1
        self.serial, self.device_state = serial, state
        self.users = {}
        self.current_user = None
        self.error = ""
        self._loaded = False
        self._task_id = ""
        self._pending_users = {}
        self.changed.emit()

    def ensure_loaded(self) -> Task | None:
        if self._loaded or self.busy or not self.serial or self.device_state != "device":
            return None
        return self.refresh()

    def refresh(self) -> Task | None:
        if not self.serial or self.device_state != "device":
            raise ValueError("请先选择在线设备")
        if self.busy:
            return None
        self.error = ""
        self._loaded = True
        return self._start("list", ["pm", "list", "users"])

    def _start(self, phase: str, tokens: list[str]) -> Task | None:
        generation = self._generation
        try:
            self._submitting_generation = generation
            try:
                task = self._runner.start_adb("发现 Android 用户" if phase == "list" else "查询 Android 前台用户",
                                              ["shell", shlex.join(tokens)], serial=self.serial)
            finally:
                if self._submitting_generation == generation:
                    self._submitting_generation = None
        except Exception as exc:
            if generation == self._generation:
                self._task_id = ""
                self.error = str(exc)
                self.changed.emit()
            return None
        if generation != self._generation:
            return None
        self._task_id, self._phase = task.id, phase
        self.changed.emit()
        return task

    def _finished(self, task: Task) -> None:
        if task.id != self._task_id:
            return
        valid = task.status == "succeeded" and task.exit_code == 0 and not task.stderr
        details = f"{task.status} / exit {task.exit_code}\n{task.stdout}{task.stderr}"
        if self._phase == "list":
            try:
                if not valid:
                    raise ValueError(details)
                self._pending_users = parse_users(task.stdout)
            except ValueError as exc:
                self._task_id = ""
                self.users = {}
                self.current_user = None
                self.error = f"用户列表不可用：{exc}"
                self._loaded = True
                self.changed.emit()
                return
            self._start("current", ["am", "get-current-user"])
            return
        text = task.stdout.strip()
        current = int(text) if valid and re.fullmatch(r"[0-9]+", text) else None
        self.users = self._pending_users
        if current not in self.users:
            current = 0 if 0 in self.users else min(self.users)
            self.error = f"前台用户不可取，已选 ID {current}\n{details}"
        self.current_user = current
        self._loaded = True
        self._task_id = ""
        self.changed.emit()

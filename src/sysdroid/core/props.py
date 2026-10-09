from __future__ import annotations

import re
import shlex

from PySide6.QtCore import QObject, Signal

from sysdroid.core.backend import Task, TaskRunner


_GETPROP_RECORD = re.compile(r"^\[([^\n]*?)\]: \[", re.MULTILINE)


def parse_getprop(output: str) -> dict[str, str]:
    """Remove getprop record framing, retaining the complete raw values."""
    if not output:
        return {}
    records = list(_GETPROP_RECORD.finditer(output))
    if not records or records[0].start() != 0:
        raise ValueError("getprop 输出缺少完整的属性记录")
    properties = {}
    for index, record in enumerate(records):
        name = record.group(1)
        if not name or any(character.isspace() or character == "\0" for character in name):
            raise ValueError(f"getprop 输出包含无效属性名称：{name!r}")
        if name in properties:
            raise ValueError(f"getprop 输出包含重复属性：{name}")
        end = records[index + 1].start() if index + 1 < len(records) else len(output)
        value = output[record.end():end]
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if not value.endswith("]"):
            raise ValueError(f"getprop 属性记录不完整或含有额外输出：{name}")
        properties[name] = value[:-1]
    return properties


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("请填写属性名称")
    if name.startswith("-"):
        raise ValueError("属性名称不能以 - 开头")
    if any(character.isspace() or character == "\0" for character in name):
        raise ValueError("属性名称不能包含空白或 NUL 字符")


def _task_details(task: Task) -> str:
    details = f"任务状态：{task.status}；退出码：{task.exit_code}"
    if task.stderr:
        details += f"\nstderr：\n{task.stderr}"
    if task.stdout:
        details += f"\nstdout：\n{task.stdout}"
    return details


class PropController(QObject):
    changed = Signal()

    def __init__(self, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self.serial = ""
        self.device_state = ""
        self.properties: dict[str, str] = {}
        self.snapshot_valid = False
        self.status = ""
        self.error = ""
        self.task_id = ""
        self.last_task_id = ""
        self._generation = 0
        self._request: tuple[str, str, str, str] | None = None
        runner.task_finished.connect(self._task_finished)

    @property
    def busy(self) -> bool:
        return bool(self.task_id)

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) == (self.serial, self.device_state):
            return
        self._generation += 1
        self.serial, self.device_state = serial, state
        self.properties = {}
        self.snapshot_valid = False
        self.status = ""
        self.error = ""
        self.task_id = ""
        self.last_task_id = ""
        self._request = None
        self.changed.emit()

    def _require_ready(self) -> None:
        if not self.serial or self.device_state != "device":
            raise ValueError("请先选择已连接的在线设备")
        if self.busy:
            raise ValueError("属性操作正在进行，请等待完成")

    def refresh(self) -> Task | None:
        self._require_ready()
        return self._start("refresh", self.serial)

    def read(self, name: str) -> Task | None:
        self._require_ready()
        _validate_name(name)
        return self._start("read", self.serial, name)

    def write(self, name: str, value: str) -> Task | None:
        self._require_ready()
        _validate_name(name)
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("属性值必须是文本且不能包含 NUL 字符")
        return self._start("write", self.serial, name, value)

    def _start(self, operation: str, serial: str, name: str = "", value: str = "") -> Task | None:
        titles = {
            "refresh": "读取 Android 属性列表",
            "read": f"读取 Android 属性：{name}",
            "write": f"写入 Android 属性：{name}",
            "verify": f"校验 Android 属性：{name}",
        }
        statuses = {
            "refresh": "正在加载属性列表…",
            "read": f"正在读取属性 {name}…",
            "write": f"正在写入属性 {name}…",
            "verify": f"正在校验属性 {name} 的实际值…",
        }
        tokens = ["setprop", name, value] if operation == "write" else ["getprop"]
        generation = self._generation
        try:
            task = self._runner.start_adb(titles[operation], ["shell", shlex.join(tokens)], serial=serial)
        except Exception as exc:
            if generation == self._generation:
                self._fail(operation, name, f"无法启动属性请求：{type(exc).__name__}: {exc}")
            raise ValueError(f"无法启动属性请求：{exc}") from exc
        # start_adb emits task_added before returning; a receiver can change devices.
        if generation != self._generation:
            return None
        self._request = (operation, serial, name, value)
        self.task_id = task.id
        self.last_task_id = task.id
        self.status = statuses[operation]
        self.error = ""
        self.changed.emit()
        return task

    def _fail(self, operation: str, name: str, details: str) -> None:
        if operation == "verify":
            self.status = "写入已返回，校验不可用"
            self.error = f"setprop 已返回成功，但无法确认属性 {name} 的实际值。\n{details}"
        else:
            self.status = {
                "refresh": "读取属性列表失败",
                "read": f"读取属性 {name} 失败",
                "write": f"写入属性 {name} 失败",
            }[operation]
            self.error = details
        self.task_id = ""
        self._request = None
        self.changed.emit()

    def _task_finished(self, task: Task) -> None:
        if task.id != self.task_id or self._request is None:
            return
        operation, serial, name, value = self._request
        if task.status != "succeeded" or task.exit_code != 0:
            self._fail(operation, name, _task_details(task))
            return
        if operation == "write":
            # Keep the pending ID (and busy) until the readback has been submitted.
            try:
                self._start("verify", serial, name, value)
            except ValueError:
                pass  # _start has already published the verification failure.
            return
        try:
            properties = parse_getprop(task.stdout)
        except ValueError as exc:
            self._fail(operation, name, f"解析 getprop 输出失败：{exc}\n{_task_details(task)}")
            return
        self.properties = properties
        self.snapshot_valid = True
        self.error = ""
        if operation == "refresh":
            self.status = f"已加载 {len(properties)} 条属性"
        elif operation == "read":
            self.status = f"已读取属性 {name}" if name in properties else f"属性 {name} 不存在"
        elif name not in properties or properties[name] != value:
            self.status = "写入校验不一致"
            actual = repr(properties[name]) if name in properties else "属性不存在"
            self.error = f"属性 {name} 写入后与请求不一致：请求值 {value!r}，实际值 {actual}。\n{_task_details(task)}"
        else:
            self.status = f"写入成功，已确认属性 {name} 的实际值"
        self.task_id = ""
        self._request = None
        self.changed.emit()

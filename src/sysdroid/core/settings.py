from __future__ import annotations

import re
import secrets
import shlex
from dataclasses import dataclass, replace
from urllib.parse import quote

from PySide6.QtCore import QObject, Signal

from sysdroid.core.backend import OUTPUT_LIMIT, TRUNCATED, Task, TaskRunner, significant_stderr


_NAMESPACES = frozenset({"system", "secure", "global"})
_NAME_RECORD = re.compile(r"Row: ([0-9]+) name=([^\r\n\0]+)")
_VALUE_BATCH_SIZE = 4


@dataclass(frozen=True)
class SettingValue:
    exists: bool
    value: str | None


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name or any(character in name for character in "\0\r\n"):
        raise ValueError("Settings 名称不能为空，也不能包含 NUL、CR 或 LF")


def parse_setting_names(output: str) -> tuple[str, ...]:
    """Accept only complete content-query records, never its exit-zero diagnostics."""
    if not isinstance(output, str) or "\0" in output:
        raise ValueError("content query 输出不是完整的名称记录")
    # AOSP prints records with println. CRLF transport framing is harmless here;
    # setting values themselves never pass through this parser.
    text = output[:-1] if output.endswith("\n") else output
    lines = text.split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    if lines == ["No result found."]:
        return ()
    names: list[str] = []
    indices: set[int] = set()
    seen: set[str] = set()
    for line in lines:
        match = _NAME_RECORD.fullmatch(line)
        if match is None:
            raise ValueError(f"content query 名称记录不完整或包含诊断：{line!r}")
        index, name = int(match[1]), match[2]
        _validate_name(name)
        if name in seen or index in indices:
            raise ValueError(f"content query 返回重复名称或记录：{name!r}")
        names.append(name)
        seen.add(name)
        indices.add(index)
    return tuple(names)


def _parse_setting_value(output: str, expected_exists: bool) -> SettingValue:
    # Remove only Android println's LF; leading/trailing value whitespace is data.
    text = output[:-1] if output.endswith("\n") else output
    exists = text != "No result found."
    if exists and not text.startswith("Row: 0 value="):
        raise ValueError("content query 值记录不完整或包含诊断")
    if exists != expected_exists:
        raise ValueError("读取期间目标存在性发生变化；未发布不完整值快照")
    value = text.removeprefix("Row: 0 value=") if exists else None
    # SQL NULL and literal uppercase NULL share the same text representation.
    return SettingValue(exists, None if value == "NULL" else value)


def _task_details(task: Task) -> str:
    return (f"任务状态：{task.status}；退出码：{task.exit_code}\n"
            f"stdout：\n{task.stdout}\nstderr：\n{task.stderr}")


@dataclass(frozen=True)
class _Request:
    operation: str
    stage: str
    generation: int
    serial: str
    namespace: str
    user_id: int
    name: str = ""
    proposed: str = ""
    names: tuple[str, ...] = ()
    pending_names: tuple[str, ...] = ()
    offset: int = 0
    nonce: str = ""
    verified_values: tuple[tuple[str, SettingValue], ...] = ()
    # (name, value) pairs for a batched write; value None means delete.
    changes: tuple[tuple[str, str | None], ...] = ()


def _parse_value_batch(output: str, request: _Request) -> dict[str, SettingValue]:
    values: dict[str, SettingValue] = {}
    cursor = 0
    for index in range(request.offset, min(len(request.pending_names), request.offset + _VALUE_BATCH_SIZE)):
        name = request.pending_names[index]
        records: dict[str, str] = {}
        for stage in ("before", "get", "after"):
            begin = f"BEGIN {request.nonce} {index} {stage}\n"
            if not output.startswith(begin, cursor):
                raise ValueError(f"{name!r}：自动读取记录缺少 {stage} 起始标记")
            cursor += len(begin)
            end = f"\nEND {request.nonce} {index} {stage} "
            boundary = output.find(end, cursor)
            if boundary < 0:
                raise ValueError(f"{name!r}：自动读取记录不完整")
            records[stage] = output[cursor:boundary]
            status_start = boundary + len(end)
            status_end = output.find("\n", status_start)
            if status_end < 0 or output[status_start:status_end] != "0":
                raise ValueError(f"{name!r}：content query 未成功完成")
            cursor = status_end + 1
        try:
            before = parse_setting_names(records["before"])
            after = parse_setting_names(records["after"])
            if before not in {(), (name,)} or after != before:
                raise ValueError("目标记录不一致或读取期间存在性发生变化")
            values[name] = _parse_setting_value(records["get"], bool(before))
        except ValueError as exc:
            raise ValueError(f"{name!r}：{exc}") from exc
    if cursor != len(output):
        raise ValueError("自动读取包含额外记录或诊断")
    return values


def _parse_mutations(output: str, request: _Request) -> tuple[list[tuple[str, int]], str]:
    """Split the leading mutation records of a change script; returns (responses, rest)."""
    cursor = 0
    responses: list[tuple[str, int]] = []
    for index, _change in enumerate(request.changes):
        begin = f"BEGIN {request.nonce} m{index} mutate\n"
        if not output.startswith(begin, cursor):
            raise ValueError("变更记录缺少起始标记")
        cursor += len(begin)
        end = f"\nEND {request.nonce} m{index} mutate "
        boundary = output.find(end, cursor)
        if boundary < 0:
            raise ValueError("变更记录不完整")
        status_start = boundary + len(end)
        status_end = output.find("\n", status_start)
        status = output[status_start:status_end] if status_end >= 0 else ""
        if not status.isdecimal():
            raise ValueError("变更记录缺少返回码")
        responses.append((output[cursor:boundary], int(status)))
        cursor = status_end + 1
    return responses, output[cursor:]


def _parse_bulk_values(output: str, names: tuple[str, ...]) -> tuple[dict[str, SettingValue], tuple[str, ...]]:
    text = output[:-1] if output.endswith("\n") else output
    lines = text.split("\n")
    prefixes = [f"Row: {index} name={name}, value=" for index, name in enumerate(names)]
    if len(lines) < len(names) or not lines[0].startswith(prefixes[0]):
        raise ValueError("批量值记录不完整或包含诊断")
    starts: list[list[int]] = [[] for _ in names]
    for line_index, line in enumerate(lines):
        match = re.match(r"Row: ([0-9]+) name=", line)
        if match is not None:
            index = int(match[1])
            if index < len(names) and line.startswith(prefixes[index]):
                starts[index].append(line_index)
    if any(not positions for positions in starts):
        raise ValueError("批量值的名称、顺序或记录格式与名称快照不一致")
    if any(len(positions) != 1 for positions in starts):
        # A value can contain a fake row header. Never choose one candidate.
        return {}, names
    boundaries = [positions[0] for positions in starts]
    if boundaries != sorted(boundaries):
        raise ValueError("批量值记录顺序与名称快照不一致")
    boundaries.append(len(lines))
    values: dict[str, SettingValue] = {}
    pending: list[str] = []
    for index, name in enumerate(names):
        start, end = boundaries[index:index + 2]
        if end - start != 1:
            pending.append(name)
            continue
        value = lines[start][len(prefixes[index]):]
        values[name] = SettingValue(True, None if value == "NULL" else value)
    return values, tuple(pending)


class SettingsController(QObject):
    changed = Signal()

    def __init__(self, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self.serial = ""
        self.device_state = ""
        self.namespace = "system"
        self.user_id: int | None = None
        self.names: tuple[str, ...] = ()
        self.values: dict[str, SettingValue] = {}
        self.last_read_name = ""
        self.read_revision = 0
        self.status = ""
        self.error = ""
        self.task_id = ""
        self.last_task_id = ""
        self._generation = 0
        self._request: _Request | None = None
        runner.task_finished.connect(self._task_finished)

    @property
    def busy(self) -> bool:
        # Reserve the slot before start_adb emits task_added, including submission.
        return self._request is not None

    def _invalidate(self) -> None:
        self._generation += 1
        self.names = ()
        self.values = {}
        self.last_read_name = ""
        self.read_revision = 0
        self.status = self.error = self.task_id = self.last_task_id = ""
        self._request = None
        self.changed.emit()

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) == (self.serial, self.device_state):
            return
        self.serial, self.device_state = serial, state
        self.user_id = None
        self._invalidate()

    def set_context(self, namespace: str, user_id: int) -> None:
        if namespace not in _NAMESPACES:
            raise ValueError("Settings namespace 必须是 system、secure 或 global")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 0:
            raise ValueError("请选择已发现的有效 Android 用户 ID")
        if (namespace, user_id) == (self.namespace, self.user_id):
            return
        self.namespace, self.user_id = namespace, user_id
        self._invalidate()

    def _invalidate_user(self) -> None:
        """The page loses its context when shared discovery loses this user."""
        if self.user_id is not None:
            self.user_id = None
            self._invalidate()

    def _require_ready(self) -> None:
        if not self.serial or self.device_state != "device":
            raise ValueError("请先选择已连接的在线设备")
        if self.user_id is None:
            raise ValueError("请先发现并选择 Android 用户")
        if self.busy:
            raise ValueError("Settings 操作正在进行，请等待完成")

    def _begin(self, operation: str, name: str = "", value: str = "") -> Task | None:
        # A read is one script (before / get / after); a write or delete is one
        # script that mutates and then performs the same verified read.
        stage = "list" if operation == "refresh" else "change" if operation in {"write", "delete"} else "read"
        changes = ((name, value if operation == "write" else None),) if stage == "change" else ()
        request = _Request(operation, stage, self._generation, self.serial, self.namespace, self.user_id,
                           name, value, pending_names=(name,) if name else (), nonce=secrets.token_hex(16),
                           changes=changes)
        return self._start(request)

    def refresh(self) -> Task | None:
        self._require_ready()
        return self._begin("refresh")

    def read(self, name: str) -> Task | None:
        self._require_ready()
        _validate_name(name)
        return self._begin("read", name)

    def write(self, name: str, value: str) -> Task | None:
        self._require_ready()
        _validate_name(name)
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Settings 值必须是文本且不能包含 NUL")
        return self._begin("write", name, value)

    def delete(self, name: str) -> Task | None:
        self._require_ready()
        _validate_name(name)
        return self._begin("delete", name)

    def write_many(self, namespace: str, changes: list[tuple[str, str]]) -> Task | None:
        """Write up to four settings and read every one back in a single round trip.

        ``namespace`` may differ from the browsed one (presets); the readback is
        only merged into the visible snapshot when it matches.
        """
        self._require_ready()
        if namespace not in _NAMESPACES:
            raise ValueError("Settings namespace 必须是 system、secure 或 global")
        if not changes or len(changes) > _VALUE_BATCH_SIZE or len({name for name, _ in changes}) != len(changes):
            raise ValueError(f"批量写入需要 1–{_VALUE_BATCH_SIZE} 个不重复名称")
        for name, value in changes:
            _validate_name(name)
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Settings 值必须是文本且不能包含 NUL")
        request = _Request("write-many", "change", self._generation, self.serial, namespace, self.user_id,
                           changes=tuple(changes), pending_names=tuple(name for name, _ in changes),
                           nonce=secrets.token_hex(16))
        return self._start(request)

    def _tokens(self, request: _Request) -> list[str]:
        uri = f"content://settings/{request.namespace}"
        if request.stage not in {"list", "bulk", "list-after"}:
            uri += "/" + quote(request.name, safe="")
        prefix = ["content", "query", "--user", str(request.user_id), "--uri", uri]
        if request.stage in {"list", "bulk", "list-after", "before", "get", "after"}:
            projection = "name:value" if request.stage == "bulk" else "value" if request.stage == "get" else "name"
            return [*prefix, "--projection", projection]
        if request.proposed is not None and request.stage == "insert":
            return ["content", "insert", "--user", str(request.user_id),
                    "--uri", f"content://settings/{request.namespace}",
                    "--bind", "name:s:" + request.name, "--bind", "value:s:" + request.proposed]
        return ["content", "delete", "--user", str(request.user_id), "--uri", uri]

    def _change_script(self, request: _Request) -> str:
        lines: list[str] = []
        for index, (name, value) in enumerate(request.changes):
            mutation = replace(request, stage="insert" if value is not None else "delete", name=name, proposed=value)
            lines.extend((f"printf 'BEGIN {request.nonce} m{index} mutate\\n'",
                          shlex.join(self._tokens(mutation)), "rc=$?",
                          f"printf '\\nEND {request.nonce} m{index} mutate %s\\n' \"$rc\""))
        return "\n".join(lines + [self._batch_script(replace(request, offset=0))])

    def _batch_script(self, request: _Request) -> str:
        lines: list[str] = []
        for index in range(request.offset, min(len(request.pending_names), request.offset + _VALUE_BATCH_SIZE)):
            for stage in ("before", "get", "after"):
                query = replace(request, stage=stage, name=request.pending_names[index])
                lines.extend((f"printf 'BEGIN {request.nonce} {index} {stage}\\n'",
                              shlex.join(self._tokens(query)), "rc=$?",
                              f"printf '\\nEND {request.nonce} {index} {stage} %s\\n' \"$rc\""))
        return "\n".join(lines)

    def _start(self, request: _Request) -> Task | None:
        if request.generation != self._generation:
            return None
        self._request = request
        self.error = ""
        self.status = (f"正在精确读取含换行或记录歧义的 Settings 值… {request.offset} / {len(request.pending_names)}"
                       if request.stage == "batch" else
                       f"正在批量写入 {len(request.changes)} 项并核对…" if request.operation == "write-many" else
                       "正在自动读取 Settings 当前值…" if request.stage == "bulk" else
                       "正在核对 Settings 名称快照…" if request.stage == "list-after" else
                       "正在加载 Settings 名称…" if request.stage == "list" else
                       f"正在{'变更并核对' if request.stage == 'change' else '读取并核对'} {request.name}…")
        try:
            batch = request.stage == "batch"
            label = (f"精确值 {request.offset + 1}–{min(len(request.pending_names), request.offset + _VALUE_BATCH_SIZE)}"
                     if batch else "、".join(request.pending_names) or "名称列表")
            script = (self._batch_script(request) if batch or request.stage == "read" else
                      self._change_script(request) if request.stage == "change" else
                      shlex.join(self._tokens(request)))
            task = self._runner.start_adb(
                f"Settings {request.namespace} · {request.operation} · {label}",
                ["shell", script], serial=request.serial, timeout=30 if batch else 10)
        except Exception as exc:
            if request.generation == self._generation and self._request is request:
                self._fail(request, f"无法启动 Settings 请求：{type(exc).__name__}: {exc}", executed=False)
            raise ValueError(f"无法启动 Settings 请求：{exc}") from exc
        # task_added can synchronously switch namespace/user/device and submit anew.
        if request.generation != self._generation or self._request is not request:
            return None
        self.task_id = self.last_task_id = task.id
        self.changed.emit()
        return task

    def _fail(self, request: _Request, details: str, *, executed: bool = True) -> None:
        # A change script may have mutated before failing: never claim either way.
        verifying = executed and request.stage == "change"
        self.status = "命令已返回，状态未确认" if verifying else "Settings 操作失败"
        self.error = details
        self.task_id = ""
        self._request = None
        self.changed.emit()

    def _next(self, request: _Request, stage: str, **changes) -> None:
        try:
            self._start(replace(request, stage=stage, **changes))
        except ValueError:
            pass  # _start publishes the real submission/verification failure.

    def _publish_value(self, request: _Request, observed: SettingValue) -> None:
        self.values = {**self.values, request.name: observed}
        members = set(self.names)
        if observed.exists:
            members.add(request.name)
        else:
            members.discard(request.name)
        self.names = tuple(sorted(members))
        self.error = ""
        if request.operation == "read":
            self.last_read_name = request.name
            self.read_revision += 1
            self.status = f"已读取 {request.name}" if observed.exists else f"{request.name} 不存在"
        elif request.operation == "write":
            if observed.exists and observed.value == request.proposed:
                self.status = "写入成功，已确认实际值"
            else:
                self.status = "写入读回不一致"
                self.error = (f"请求值：{request.proposed!r}\n实际值：" +
                              ("存在，但 SQL NULL 与文本 NULL 无法由当前接口区分；AOSP 还会归一文本 null，未确认字面值。"
                               if observed.exists and observed.value is None else
                               repr(observed.value) if observed.exists else "不存在"))
        elif not observed.exists:
            self.status = "删除成功，已确认不存在"
        else:
            self.status = "删除读回不一致"
            self.error = f"目标仍然存在；实际值：{observed.value!r}"
        self.task_id = ""
        self._request = None
        self.changed.emit()

    def _publish_many(self, request: _Request, observed: dict[str, SettingValue]) -> None:
        mismatches = [f"{name}：请求 {value!r}，实际 " + (repr(observed[name].value) if observed[name].exists else "不存在")
                      for name, value in request.changes
                      if not (observed[name].exists and observed[name].value == value)]
        if request.namespace == self.namespace:
            self.values = {**self.values, **observed}
            members = set(self.names) | {name for name, value in observed.items() if value.exists}
            self.names = tuple(sorted(members - {name for name, value in observed.items() if not value.exists}))
        if mismatches:
            self.status = "批量写入读回不一致"
            self.error = "\n".join(mismatches)
        else:
            self.status = f"已写入 {len(request.changes)} 项 {request.namespace} 设置，并确认实际值"
            self.error = ""
        self.task_id = ""
        self._request = None
        self.changed.emit()

    def _task_finished(self, task: Task) -> None:
        request = self._request
        if request is None or task.id != self.task_id or request.generation != self._generation:
            return
        if (task.status != "succeeded" or task.exit_code != 0 or significant_stderr(task.stderr) or
                (len(task.stdout) >= OUTPUT_LIMIT and task.stdout.startswith(TRUNCATED))):
            self._fail(request, _task_details(task))
            return
        if request.stage == "bulk":
            try:
                values, pending = _parse_bulk_values(task.stdout, request.names)
            except ValueError as exc:
                self._fail(request, f"解析 Settings 批量值失败：{exc}\n{_task_details(task)}")
                return
            verified = tuple(values.items())
            if pending:
                self._next(request, "batch", pending_names=pending,
                           verified_values=verified, nonce=secrets.token_hex(16))
            else:
                self._next(request, "list-after", verified_values=verified)
            return
        if request.stage == "batch":
            try:
                observed = _parse_value_batch(task.stdout, request)
            except ValueError as exc:
                self._fail(request, f"解析 Settings 自动读值失败：{exc}\n{_task_details(task)}")
                return
            self.values = {**self.values, **observed}
            missing = {name for name, value in observed.items() if not value.exists}
            if missing:
                self.names = tuple(name for name in self.names if name not in missing)
            offset = request.offset + len(observed)
            verified = request.verified_values + tuple(observed.items())
            if offset < len(request.pending_names):
                self._next(request, "batch", offset=offset, verified_values=verified,
                           nonce=secrets.token_hex(16))
            else:
                self._next(request, "list-after", verified_values=verified)
            return
        if request.stage == "read":
            try:
                observed = _parse_value_batch(task.stdout, request)
            except ValueError as exc:
                self._fail(request, f"解析 Settings 读取结果失败：{exc}\n{_task_details(task)}")
                return
            self._publish_value(request, observed[request.name])
            return
        if request.stage == "change":
            try:
                responses, rest = _parse_mutations(task.stdout, request)
                for (name, value), (response, code) in zip(request.changes, responses):
                    # put is silent; delete explicitly reports its affected row count.
                    text = response[:-1] if response.endswith("\n") else response
                    if code != 0 or (text and not (value is None and re.fullmatch(r"Deleted [0-9]+ rows", text))):
                        raise ValueError(f"{name!r}：Settings 命令包含未识别的返回或诊断（返回码 {code}）")
                observed = _parse_value_batch(rest, request)
            except ValueError as exc:
                self._fail(request, f"{exc}\n{_task_details(task)}")
                return
            if request.operation == "write-many":
                self._publish_many(request, observed)
            else:
                self._publish_value(request, observed[request.name])
            return
        try:
            names = parse_setting_names(task.stdout)
        except ValueError as exc:
            self._fail(request, f"解析 content query 失败：{exc}\n{_task_details(task)}")
            return
        if request.stage == "list-after":
            if set(names) != set(self.names):
                self._fail(request, "自动读取期间名称快照发生变化；未发布混合值快照。\n" + _task_details(task))
                return
            self.values = dict(request.verified_values)
            self.status = f"已加载 {len(self.names)} 项 Settings 名称及当前值"
            self.error = self.task_id = ""
            self._request = None
            self.changed.emit()
            return
        if request.stage == "list":
            self.names = tuple(sorted(names))
            self.values = {name: value for name, value in self.values.items() if name in names}
            if names:
                self._next(request, "bulk", names=names)
            else:
                self.status = "已加载 0 项 Settings 名称及当前值"
                self.error = self.task_id = ""
                self._request = None
                self.changed.emit()

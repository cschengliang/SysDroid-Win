from __future__ import annotations

import codecs
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import adbutils
from adbutils import AdbTimeout
from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from runtime_paths import adb_executable, application_dir

DATA_DIR = Path(os.environ.get("ANDROID_TOOLBOX_DATA_DIR") or
                str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AndroidToolbox"))
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
STATUS_LABELS = {"starting": "启动中", "running": "运行中", "stopping": "停止中",
                 "succeeded": "成功", "failed": "失败", "cancelled": "已停止", "timed_out": "超时"}
OUTPUT_LIMIT = 2 * 1024 * 1024
TRUNCATED = "[较早输出已截断；保留最近 2 MiB 字符]\n"


def powershell_command(program: str, args: list[str]) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    return "& " + " ".join(quote(value) for value in [program, *args])


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport: str = ""


def parse_devices(output: str) -> list[Device]:
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] in {"List", "*", "adb:"}:
            continue
        if fields[1] not in {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader", "no"}:
            continue
        details = dict(part.split(":", 1) for part in fields[2:] if ":" in part)
        devices.append(Device(fields[0], "no permissions" if fields[1] == "no" else fields[1],
                              details.get("model", "").replace("_", " "), details.get("product", ""),
                              details.get("device", ""), details.get("transport_id", "")))
    return devices


@dataclass
class Task:
    id: str
    title: str
    program: str
    args: list[str]
    serial: str = ""
    command_id: str = ""
    kind: str = "adb"
    status: str = "starting"
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    elapsed: float = 0.0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    transient: bool = False

    @property
    def command(self) -> str:
        return powershell_command(self.program, self.args)


@dataclass(frozen=True)
class _AdbResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0


@dataclass
class _AdbRunning:
    thread: threading.Thread
    started: float
    timeout: int
    stop_at: float | None = None
    outcome: str | None = None


@dataclass
class _Running:
    process: subprocess.Popen
    started: float
    timeout: int
    closed_streams: int = 0
    stop_at: float | None = None
    outcome: str | None = None
    killing: bool = False


class TaskRunner(QObject):
    task_added = Signal(object)
    task_changed = Signal(object)
    task_output = Signal(str, str, str)
    task_finished = Signal(object)
    history_changed = Signal()
    task_removed = Signal(str)
    active_count_changed = Signal(int)
    error = Signal(str)
    _adb_result = Signal(str, object)
    _stream_closed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tasks: dict[str, Task] = {}
        self.history: list[Task] = []
        self._running: dict[str, _Running] = {}
        self._adb_running: dict[str, _AdbRunning] = {}
        self._active_ids: dict[str, None] = {}
        self._transient_finished: deque[str] = deque()
        self._pinned_id = ""
        self._pending_output: dict[tuple[str, str], deque[str]] = {}
        self._pending_sizes: dict[tuple[str, str], int] = {}
        self._pending_truncated: set[tuple[str, str]] = set()
        self._output_lock = threading.Lock()
        self._last_elapsed_emit = 0.0
        self._history_writable = True
        self._history_path = DATA_DIR / "history.json"
        try:
            if self._history_path.exists():
                records = json.loads(self._history_path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("历史记录必须是列表")
                self.history = [Task(**record) for record in records]
                if any(task.status not in TERMINAL_STATUSES for task in self.history):
                    raise ValueError("历史记录包含未结束任务")
        except (OSError, ValueError, TypeError) as exc:
            self._history_writable = False
            message = f"无法读取 {self._history_path}：{exc}。原文件保留，不会覆盖。"
            QTimer.singleShot(0, lambda: self.error.emit(message))
        self._adb_result.connect(self._receive_adb_result)
        self._stream_closed.connect(self._receive_eof)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush_output)

    def resolve(self, program: str) -> str:
        if program.lower() in {"adb", "adb.exe"}:
            return str(adb_executable())
        if program.lower() in {"scrcpy", "scrcpy.exe"}:
            executable = application_dir() / "tool" / "scrcpy-win64-v5.0" / "scrcpy.exe"
            if not executable.is_file():
                raise ValueError(f"找不到项目自带 Scrcpy：{executable}")
            return str(executable)
        found = shutil.which(program)
        if not found:
            raise ValueError(f"找不到 {program}。请安装程序并加入 PATH。")
        return found

    def start_adb(self, title: str, args: list[str], serial: str = "", command_id: str = "",
                  timeout: int = 10, *, transient: bool = False) -> Task:
        task = Task(uuid.uuid4().hex, title, self._adb_binary(), (["-s", serial] if serial else []) + list(args),
                    serial, command_id, "adb", transient=transient)
        self._register(task)
        QTimer.singleShot(0, lambda: self._spawn_adb(task, list(args), serial, timeout))
        return task

    def _spawn_adb(self, task: Task, args: list[str], serial: str, timeout: int) -> None:
        if task.status in TERMINAL_STATUSES:
            return
        task.status = "running"
        self.task_changed.emit(task)
        if task.status in TERMINAL_STATUSES:
            return
        thread = threading.Thread(target=self._run_adb, args=(task.id, args, serial, timeout), daemon=True)
        self._adb_running[task.id] = _AdbRunning(thread, time.monotonic(), timeout)
        thread.start()

    def _run_adb(self, task_id: str, args: list[str], serial: str, timeout: int) -> None:
        try:
            result = self._execute_adb(args, serial, timeout)
        except subprocess.TimeoutExpired as exc:
            decode = lambda value: value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            result = _AdbResult(stdout=decode(exc.stdout), stderr=decode(exc.stderr) + f"\nTimeoutExpired: {exc}\n", exit_code=None)
        except (AdbTimeout, TimeoutError) as exc:
            result = _AdbResult(stderr=f"{type(exc).__name__}: {exc}\n", exit_code=None)
        except Exception as exc:
            result = _AdbResult(stderr=f"{type(exc).__name__}: {exc}\n", exit_code=1)
        self._adb_result.emit(task_id, result)

    def _receive_adb_result(self, task_id: str, result: _AdbResult) -> None:
        running = self._adb_running.pop(task_id, None)
        task = self.tasks.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        if result.stdout:
            self._receive_output(task_id, "stdout", result.stdout)
        if result.stderr:
            self._receive_output(task_id, "stderr", result.stderr)
        outcome = running.outcome if running else None
        status = outcome or ("succeeded" if result.exit_code == 0 else "timed_out" if result.exit_code is None else "failed")
        self._finish(task, status, None if outcome else result.exit_code)

    @staticmethod
    def _adb_binary() -> str:
        return str(adb_executable())

    def open_adb_terminal(self, serial: str) -> int:
        directory = application_dir()
        terminal = directory / "tool" / "terminal-1.25.2733.0" / "WindowsTerminal.exe"
        if not terminal.is_file():
            raise ValueError(f"找不到项目内置终端：{terminal}")
        adb = self._adb_binary()
        args = ["-w", "new", "new-tab", "--title", f"ADB · {serial}",
                "--suppressApplicationTitle", "-d", str(directory), adb, "-s", serial, "shell"]
        # Interactive stdin and output belong to Terminal, not the task runner's pipes.
        started, pid = QProcess.startDetached(str(terminal), args, str(directory))
        if not started:
            raise OSError(f"无法启动项目内置终端：{terminal}")
        return pid

    def _run_adb_binary(self, args: list[str], timeout: int, serial: str = "") -> _AdbResult:
        completed = subprocess.run(
            [self._adb_binary(), *(["-s", serial] if serial else []), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(float(timeout), 1.0), check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return _AdbResult(
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            exit_code=completed.returncode,
        )

    @staticmethod
    def _text_result(output: object) -> _AdbResult:
        text = "" if output is None else str(output)
        if text and not text.endswith("\n"):
            text += "\n"
        lowered = text.casefold()
        failed = any(marker in lowered for marker in ("error:", "failed", "failure", "cannot", "unable"))
        return _AdbResult(stdout=text, exit_code=1 if failed else 0)

    @staticmethod
    def _transport_command(device, command: str) -> str:
        with device.open_transport() as connection:
            connection.send_command(command)
            connection.check_okay()
            return connection.read_until_close()

    def _execute_adb(self, args: list[str], serial: str, timeout: int) -> _AdbResult:
        if not args:
            raise ValueError("ADB 命令不能为空")
        selected = self._adb_binary()
        os.environ["ADB"] = selected
        os.environ["ADBUTILS_ADB_PATH"] = selected
        client = adbutils.AdbClient(socket_timeout=max(float(timeout), 1.0))
        command = args[0].lower()
        values = args[1:]
        if command == "devices":
            infos = client.list(extended="-l" in values)
            lines = ["List of devices attached"]
            for info in infos:
                tags = " ".join(f"{key}:{value}" for key, value in info.tags.items())
                lines.append(f"{info.serial}\t{info.state}" + (f" {tags}" if tags else ""))
            return _AdbResult(stdout="\n".join(lines) + "\n")
        if command == "connect":
            if len(values) != 1:
                raise ValueError("adb connect 需要一个 host:port 地址")
            return self._text_result(client.connect(values[0], timeout=timeout))
        if command == "disconnect":
            if len(values) != 1:
                raise ValueError("adb disconnect 需要一个无线设备地址")
            return self._text_result(client.disconnect(values[0], raise_error=True))
        if command == "kill-server":
            client.server_kill()
            return _AdbResult(stdout="ADB Server 已停止\n")
        if command in {"start-server", "version"}:
            return self._run_adb_binary([command], timeout)
        if not serial:
            raise ValueError(f"ADB 子命令 {command} 需要设备 Serial")
        if command in {"install", "install-multiple"}:
            return self._run_adb_binary(args, timeout, serial)
        device = client.device(serial)
        if command in {"shell", "exec-out", "logcat"}:
            shell_values = values if command != "logcat" else ["logcat", *values]
            if not shell_values:
                raise ValueError("Shell 命令不能为空")
            result = device.shell2(" ".join(shell_values), timeout=timeout, encoding="utf-8", rstrip=False, v2=True)
            stdout = result.stdout or (result.output if not result.stderr else "")
            return _AdbResult(stdout=stdout, stderr=result.stderr, exit_code=result.returncode)
        if command == "push":
            if len(values) != 2:
                raise ValueError("adb push 需要本地源路径和设备目标路径")
            size = device.sync.push(values[0], values[1])
            return _AdbResult(stdout=f"{size} bytes pushed\n")
        if command == "pull":
            if len(values) != 2:
                raise ValueError("adb pull 需要设备源路径和本地目标路径")
            size = device.sync.pull(values[0], values[1])
            return _AdbResult(stdout=f"{size} bytes pulled\n")
        if command == "uninstall":
            if len(values) != 1:
                raise ValueError("adb uninstall 需要一个包名")
            return self._text_result(device.uninstall(values[0]))
        if command == "root":
            return self._text_result(device.root())
        if command == "remount":
            return self._text_result(self._transport_command(device, "remount:"))
        if command == "reboot":
            result = device.shell2(" ".join(["reboot", *values]), timeout=timeout, encoding="utf-8", rstrip=False, v2=True)
            return _AdbResult(stdout=result.stdout or result.output, stderr=result.stderr, exit_code=result.returncode)
        if command == "forward":
            if len(values) != 2:
                raise ValueError("adb forward 需要本地和设备端点")
            device.forward(values[0], values[1])
            return _AdbResult()
        if command == "reverse":
            if len(values) != 2:
                raise ValueError("adb reverse 需要设备端和本地端点")
            device.reverse(values[0], values[1])
            return _AdbResult()
        if command == "get-state":
            return self._text_result(device.get_state())
        if command == "get-serialno":
            return self._text_result(device.get_serialno())
        if command == "get-devpath":
            return self._text_result(device.get_devpath())
        if command == "get-features":
            return self._text_result(device.get_features())
        raise ValueError(f"暂不支持通过 adbutils 执行 ADB 子命令：{command}")

    def start_process(self, title: str, program: str, args: list[str], serial: str = "",
                      command_id: str = "", timeout: int = 0, kind: str = "process",
                      env: dict | None = None) -> Task:
        task = Task(uuid.uuid4().hex, title, program, list(args), serial, command_id, kind)
        self._register(task)
        QTimer.singleShot(0, lambda: self._spawn(task, timeout, env))
        return task

    def _spawn(self, task: Task, timeout: int, env: dict | None) -> None:
        if task.status in TERMINAL_STATUSES:
            return
        try:
            task.program = self.resolve(task.program)
            environment = os.environ.copy()
            if env:
                environment.update(env)
            process = subprocess.Popen([task.program, *task.args], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
        except (OSError, ValueError) as exc:
            self._receive_output(task.id, "stderr", str(exc) + "\n")
            self._finish(task, "failed", None)
            return
        self._running[task.id] = _Running(process, time.monotonic(), timeout)
        task.status = "running"
        self.task_changed.emit(task)
        for stream in ("stdout", "stderr"):
            threading.Thread(target=self._read_pipe, args=(task.id, stream, getattr(process, stream)),
                             daemon=True).start()

    @property
    def active_count(self) -> int:
        return len(self._active_ids)

    def _register(self, task: Task) -> None:
        self.tasks[task.id] = task
        self._active_ids[task.id] = None
        self._timer.start()
        self._flush_timer.start()
        self.active_count_changed.emit(self.active_count)
        self.task_added.emit(task)

    def _queue_output(self, task_id: str, stream: str, text: str) -> None:
        key = (task_id, stream)
        with self._output_lock:
            chunks = self._pending_output.setdefault(key, deque())
            size = self._pending_sizes.get(key, 0) + len(text)
            chunks.append(text)
            while size > OUTPUT_LIMIT:
                overflow = size - OUTPUT_LIMIT
                first = chunks.popleft()
                if len(first) > overflow:
                    chunks.appendleft(first[overflow:])
                    size -= overflow
                else:
                    size -= len(first)
                self._pending_truncated.add(key)
            self._pending_sizes[key] = size

    def _drain_output(self, task_id: str) -> None:
        drained = []
        with self._output_lock:
            for stream in ("stdout", "stderr"):
                key = (task_id, stream)
                chunks = self._pending_output.pop(key, None)
                self._pending_sizes.pop(key, None)
                truncated = key in self._pending_truncated
                self._pending_truncated.discard(key)
                if chunks:
                    text = "".join(chunks)
                    if truncated:
                        text = TRUNCATED + text[-(OUTPUT_LIMIT - len(TRUNCATED)):]
                    drained.append((stream, text))
        for stream, text in drained:
            self._receive_output(task_id, stream, text)

    def _flush_output(self) -> None:
        for task_id in tuple(self._active_ids):
            self._drain_output(task_id)

    def _read_pipe(self, task_id: str, stream: str, pipe) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := pipe.read1(8192):
                text = decoder.decode(chunk)
                if text:
                    self._queue_output(task_id, stream, text)
            remaining = decoder.decode(b"", final=True)
            if remaining:
                self._queue_output(task_id, stream, remaining)
        finally:
            pipe.close()
            self._stream_closed.emit(task_id)

    def _receive_output(self, task_id: str, stream: str, text: str) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        previous = getattr(task, stream)
        combined = previous + text
        if len(combined) > OUTPUT_LIMIT:
            combined = TRUNCATED + combined[-(OUTPUT_LIMIT - len(TRUNCATED)):]
        setattr(task, stream, combined)
        self.task_output.emit(task_id, stream, text)

    def _receive_eof(self, task_id: str) -> None:
        running = self._running.get(task_id)
        if running:
            running.closed_streams += 1
            self._tick()

    def _tick(self) -> None:
        now = time.monotonic()
        emit_elapsed = now - self._last_elapsed_emit >= 1.0
        if emit_elapsed:
            self._last_elapsed_emit = now
        for task_id, running in list(self._adb_running.items()):
            task = self.tasks.get(task_id)
            if task is None:
                self._adb_running.pop(task_id, None)
                continue
            task.elapsed = now - running.started
            if running.timeout and task.elapsed >= running.timeout and running.outcome is None:
                running.outcome = "timed_out"
                task.status = "stopping"
                self._receive_output(task_id, "stderr", f"\n任务超过 {running.timeout} 秒，正在等待当前 ADB 请求返回。\n")
                self.task_changed.emit(task)
            elif emit_elapsed:
                self.task_changed.emit(task)
        for task_id, running in list(self._running.items()):
            task = self.tasks[task_id]
            task.elapsed = now - running.started
            code = running.process.poll()
            if code is not None:
                if running.closed_streams == 2:
                    self._finish(task, running.outcome or ("succeeded" if code == 0 else "failed"), code)
                continue
            if running.timeout and task.elapsed >= running.timeout and running.outcome is None:
                self._receive_output(task_id, "stderr", f"\n任务超过 {running.timeout} 秒，正在停止。\n")
                self._stop(task, running, "timed_out", False)
            if running.stop_at is not None and now - running.stop_at >= 4 and not running.killing:
                self._receive_output(task_id, "stderr", "\n进程未在 4 秒内响应停止，正在强制结束；写入中的文件可能不完整。\n")
                self._kill(running)
            if emit_elapsed:
                self.task_changed.emit(task)

    def _finish(self, task: Task, status: str, code: int | None) -> None:
        if task.status in TERMINAL_STATUSES:
            return
        self._drain_output(task.id)
        self._running.pop(task.id, None)
        self._adb_running.pop(task.id, None)
        task.status = status
        task.exit_code = code
        if task.id in self._active_ids:
            del self._active_ids[task.id]
            self.active_count_changed.emit(self.active_count)
        if not self._active_ids:
            self._timer.stop()
            self._flush_timer.stop()
        if task.transient:
            self._transient_finished.append(task.id)
        else:
            self.history.append(task)
            self.history = self.history[-200:]
            self._save_history()
        self.task_changed.emit(task)
        if not task.transient:
            self.history_changed.emit()
        self.task_finished.emit(task)
        if task.transient:
            QTimer.singleShot(0, self._prune_transient)

    def pin_task(self, task_id: str) -> None:
        if self.get_task(task_id) is not None:
            self._pinned_id = task_id
            self._prune_transient()

    def unpin_task(self, task_id: str) -> None:
        if self._pinned_id == task_id:
            self._pinned_id = ""
            self._prune_transient()

    def _prune_transient(self) -> None:
        recent = set(tuple(self._transient_finished)[-20:])
        retained = deque()
        for task_id in self._transient_finished:
            if task_id in recent or task_id == self._pinned_id:
                retained.append(task_id)
            elif self.tasks.pop(task_id, None) is not None:
                self.task_removed.emit(task_id)
        self._transient_finished = retained

    def _save_history(self) -> None:
        if not self._history_writable:
            return
        try:
            self._write_history(self.history)
        except (OSError, ValueError) as exc:
            self.error.emit(f"无法保存执行历史：{exc}")

    def _write_history(self, history: list[Task]) -> None:
        if not self._history_writable:
            raise ValueError(f"无法修改执行历史：{self._history_path} 读取失败，原文件保留，不会覆盖。")
        temporary = self._history_path.with_suffix(".tmp")
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps([asdict(task) for task in history],
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._history_path)
        except (OSError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def clear_history(self, kind: str) -> None:
        if not self._history_writable:
            raise ValueError(f"无法修改执行历史：{self._history_path} 读取失败，原文件保留，不会覆盖。")
        retained = [task for task in self.history
                    if task.kind != kind or task.status not in TERMINAL_STATUSES]
        if len(retained) == len(self.history):
            return
        self._write_history(retained)
        self.history = retained
        self.history_changed.emit()

    def cancel(self, task_id: str, force: bool = False) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        running = self._running.get(task_id)
        if running is None:
            adb_running = self._adb_running.get(task_id)
            if adb_running is not None:
                if adb_running.outcome is not None:
                    return
                adb_running.outcome = "cancelled"
                task.status = "stopping"
                self._receive_output(task_id, "stderr", "\n正在等待当前 ADB 请求返回；adbutils 无法中断已发出的请求。\n")
                self.task_changed.emit(task)
            else:
                self._finish(task, "cancelled", None)
        else:
            self._stop(task, running, running.outcome or "cancelled", force)

    def _stop(self, task: Task, running: _Running, outcome: str, force: bool) -> None:
        running.outcome = outcome
        task.status = "stopping"
        if force:
            self._kill(running)
        elif running.stop_at is None:
            running.stop_at = time.monotonic()
            try:
                if os.name == "nt":
                    # A console process group isolates the signal from the toolbox and ADB server.
                    running.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(running.process.pid, signal.SIGINT)
            except (OSError, ProcessLookupError):
                running.process.terminate()
        self.task_changed.emit(task)

    def _kill(self, running: _Running) -> None:
        running.killing = True
        if os.name == "nt":
            subprocess.Popen([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"), "/PID", str(running.process.pid), "/T", "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(running.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def clear_finished(self) -> None:
        for task_id, task in tuple(self.tasks.items()):
            if task.status in TERMINAL_STATUSES and not (task.transient and task_id == self._pinned_id):
                del self.tasks[task_id]
                self.task_removed.emit(task_id)
        self._transient_finished = deque(task_id for task_id in self._transient_finished if task_id in self.tasks)

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id) or next((task for task in reversed(self.history) if task.id == task_id), None)

    def active(self) -> list[Task]:
        return [self.tasks[task_id] for task_id in self._active_ids]

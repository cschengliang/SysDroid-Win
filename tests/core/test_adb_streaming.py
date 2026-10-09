"""ADB execution core: live streaming, real cancellation, timeouts and failure detection.

No device is needed: a fake adbutils client/connection replays shell protocol bytes.
"""
import json
import socket
import struct
import sys
import threading

import pytest

from sysdroid.core import backend
from sysdroid.core.backend import (TERMINAL_STATUSES, TaskRunner, classify_text_result, read_shell_v2,
                                   significant_stderr, sniff_binary, windows_terminal_arg)


def v2_packet(kind, payload=b""):
    return bytes([kind]) + struct.pack("<I", len(payload)) + payload


class FakeSocket:
    """recv() replays chunks; once exhausted it either ends (b"") or keeps 'waiting'."""

    def __init__(self, chunks, hang=False):
        self.chunks = list(chunks)
        self.hang = hang
        self.timeout = None
        self.closed = False
        self.waiting = threading.Event()

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        if self.closed:
            raise OSError("closed")
        if self.chunks:
            return self.chunks.pop(0)
        if self.hang:
            self.waiting.set()
            threading.Event().wait(0.02)
            raise socket.timeout()
        return b""


class FakeConnection:
    def __init__(self, sock):
        self.conn = sock
        self.sent = []

    def send_command(self, command):
        self.sent.append(command)

    def check_okay(self):
        pass

    def close(self):
        self.conn.closed = True


class FakeDevice:
    def __init__(self, script, features="shell_v2,cmd"):
        self.script = script
        self.features = features
        self.connections = []
        self.feature_calls = 0

    def get_features(self):
        self.feature_calls += 1
        return self.features

    def open_transport(self, command=None, timeout=None):
        connection = FakeConnection(None)
        connection.conn = self.script(connection)
        self.connections.append(connection)
        return connection


class FakeClient:
    def __init__(self, device):
        self._device = device

    def device(self, serial):
        return self._device


def install(monkeypatch, runner, device):
    monkeypatch.setattr(runner, "_adb_client", lambda timeout: FakeClient(device))


def test_shell_v2_reader_handles_split_packets_and_separates_streams():
    data = v2_packet(1, "首行\n".encode()) + v2_packet(2, b"warn\n") + v2_packet(1, b"tail") + v2_packet(3, b"\x07")
    chunks = [data[index:index + 3] for index in range(0, len(data), 3)]
    seen = []
    code = read_shell_v2(lambda size: chunks.pop(0) if chunks else b"", lambda stream, payload: seen.append((stream, payload)),
                         lambda: False)
    assert code == 7
    assert b"".join(payload for stream, payload in seen if stream == "stdout").decode() == "首行\ntail"
    assert [payload for stream, payload in seen if stream == "stderr"] == [b"warn\n"]


def test_shell_streams_output_live_and_reports_exit_code(runner, qtbot, monkeypatch):
    payload = "日志🙂\n".encode()
    sock = FakeSocket([v2_packet(1, payload[:4]), v2_packet(1, payload[4:]), v2_packet(2, b"err\n"), v2_packet(3, b"\x02")])
    device = FakeDevice(lambda connection: sock)
    install(monkeypatch, runner, device)
    task = runner.start_adb("shell", ["shell", "echo hi"], serial="s1")
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert (task.status, task.exit_code, task.stdout, task.stderr) == ("failed", 2, "日志🙂\n", "err\n")
    assert device.connections[0].sent == ["shell,v2,raw:echo hi"] and sock.closed


def test_continuous_command_shows_output_before_finishing_and_cancel_closes_connection(runner, qtbot, monkeypatch):
    sock = FakeSocket([v2_packet(1, b"line 1\n"), v2_packet(1, b"line 2\n")], hang=True)
    install(monkeypatch, runner, FakeDevice(lambda connection: sock))
    live = []
    runner.task_output.connect(lambda task_id, stream, text: live.append(text))
    task = runner.start_adb("logcat", ["logcat"], serial="s1", timeout=0)
    qtbot.waitUntil(lambda: "line 2" in task.stdout, timeout=3000)
    assert task.status == "running" and "".join(live) == "line 1\nline 2\n"
    runner.cancel(task.id)
    assert task.status == "stopping"
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert task.status == "cancelled" and task.stdout == "line 1\nline 2\n"
    assert sock.closed and "无法中途中断" not in task.stderr


def test_timeout_stops_stream_and_keeps_partial_output(runner, qtbot, monkeypatch):
    sock = FakeSocket([v2_packet(1, b"partial\n")], hang=True)
    install(monkeypatch, runner, FakeDevice(lambda connection: sock))
    task = runner.start_adb("top", ["shell", "top"], serial="s1", timeout=5)
    qtbot.waitUntil(sock.waiting.is_set, timeout=3000)
    runner._adb_running[task.id].started -= 10
    runner._tick()
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert task.status == "timed_out" and task.stdout == "partial\n" and sock.closed
    assert "已收到的输出会保留" in task.stderr


def test_lost_connection_without_exit_code_is_a_failure(runner, qtbot, monkeypatch):
    install(monkeypatch, runner, FakeDevice(lambda connection: FakeSocket([v2_packet(1, b"half")])))
    task = runner.start_adb("shell", ["shell", "ls"], serial="s1")
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert task.status == "failed" and task.stdout == "half" and "断开" in task.stderr


def test_legacy_shell_without_v2_hides_exit_marker(runner, qtbot, monkeypatch):
    def script(connection):
        class Lazy(FakeSocket):
            def recv(self, size):
                if not self.chunks and not getattr(self, "primed", False):
                    self.primed = True
                    marker = connection.sent[0].rsplit("echo ", 1)[1].replace("$?", "")
                    data = b"out\n" + marker.encode() + b"3\n"
                    self.chunks = [data[index:index + 5] for index in range(0, len(data), 5)]
                return super().recv(size)
        return Lazy([])
    device = FakeDevice(script, features="cmd")
    install(monkeypatch, runner, device)
    task = runner.start_adb("shell", ["shell", "id"], serial="s1")
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert (task.status, task.exit_code, task.stdout) == ("failed", 3, "out\n")
    assert device.connections[0].sent[0].startswith("shell:id\necho SYSDROID-EXIT-")
    second = runner.start_adb("shell", ["shell", "id"], serial="s1")
    qtbot.waitUntil(lambda: second.status in TERMINAL_STATUSES, timeout=3000)
    assert device.feature_calls == 1  # cached per serial


def test_exec_out_binary_is_saved_to_a_file(runner, qtbot, monkeypatch, tmp_path):
    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
    sock = FakeSocket([image[:1000], image[1000:]])
    device = FakeDevice(lambda connection: sock)
    install(monkeypatch, runner, device)
    task = runner.start_adb("screencap", ["exec-out", "screencap -p"], serial="s1")
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    saved = list((tmp_path / "exec-out").glob("*.png"))
    assert task.status == "succeeded" and len(saved) == 1 and saved[0].read_bytes() == image
    assert str(saved[0]) in task.stdout and "\x89" not in task.stdout
    assert device.connections[0].sent == ["exec:screencap -p"]


def test_exec_out_text_is_displayed(runner, qtbot, monkeypatch, tmp_path):
    install(monkeypatch, runner, FakeDevice(lambda connection: FakeSocket(["中文\n".encode()[:2], "中文\n".encode()[2:]])))
    task = runner.start_adb("cat", ["exec-out", "cat /proc/version"], serial="s1")
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
    assert (task.status, task.stdout) == ("succeeded", "中文\n")
    assert not (tmp_path / "exec-out").exists()


@pytest.mark.parametrize("head,suffix", [(b"\x89PNG\r\n\x1a\nxx", ".png"), (b"abc\x00def", ".bin"),
                                         (b"\xff\xfe\xfdabcdef", ".bin"), ("文本".encode(), None),
                                         ("文本".encode()[:-1], None)])
def test_binary_sniffing(head, suffix):
    assert sniff_binary(head) == suffix


def test_cancelling_adb_executable_kills_the_process(runner, qtbot, monkeypatch):
    monkeypatch.setattr(runner, "_adb_binary", lambda: sys.executable)
    control = backend._AdbControl("task", lambda task_id, stream, text: seen.append(text))
    seen = []
    code = "import sys,time; print('Performing Streamed Install', flush=True); time.sleep(30)"
    result = {}
    worker = threading.Thread(target=lambda: result.update(value=runner._run_adb_binary(["-c", code], 0, "", control)))
    worker.start()
    qtbot.waitUntil(lambda: "Performing" in "".join(seen), timeout=10000)
    control.stop.set()
    worker.join(10)
    assert not worker.is_alive() and result["value"].exit_code is None


@pytest.mark.parametrize("command,text,ok", [
    ("connect", "connected to 10.0.0.2:5555", True),
    ("connect", "already connected to 10.0.0.2:5555", True),
    ("connect", "failed to connect to '10.0.0.2:5555': Connection refused", False),
    ("connect", "failed to authenticate to 10.0.0.2:5555", False),
    ("disconnect", "disconnected 10.0.0.2:5555", True),
    ("disconnect", "error: no such device '10.0.0.2:5555'", False),
    ("root", "restarting adbd as root", True),
    ("root", "adbd is already running as root", True),
    ("root", "adbd cannot run as root in production builds", False),
    ("remount", "Using overlayfs for /system\nremount succeeded", True),
    ("remount", "Not running as root. Try \"adb root\" first.", False),
    ("remount", "remount failed", False),
    ("uninstall", "Success", True),
    ("uninstall", "Failure [DELETE_FAILED_INTERNAL_ERROR]", False),
])
def test_text_replies_are_classified_per_command(command, text, ok):
    assert classify_text_result(command, text) is ok


def test_benign_loader_noise_is_not_significant_stderr():
    noise = "WARNING: linker: /system/bin/app_process64: unused DT entry: type 0x6ffffef5\n\n"
    assert significant_stderr(noise) == ""
    assert significant_stderr(noise + "SecurityException: denied\n") == "SecurityException: denied"


def test_terminal_arguments_escape_semicolons(runner, monkeypatch):
    monkeypatch.setattr(runner, "_adb_binary", lambda: r"C:\tools\adb.exe")
    args = runner.adb_terminal_args("evil;new-tab cmd", r"C:\app")
    assert "evil\\;new-tab cmd" in args and "ADB · evil\\;new-tab cmd" in args
    assert all(";" not in arg.replace("\\;", "") for arg in args)
    assert windows_terminal_arg("a;b;c") == "a\\;b\\;c"

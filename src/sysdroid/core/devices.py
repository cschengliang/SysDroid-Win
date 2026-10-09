"""Live device tracking through the ADB server's host:track-devices service."""
from __future__ import annotations

import os
import socket
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal

_POLL = 0.5


class _Stopped(Exception):
    pass


def parse_track_payload(payload: str) -> dict[str, str]:
    """`serial<TAB>state` lines -> {serial: state}."""
    devices = {}
    for line in payload.splitlines():
        serial, separator, state = line.strip().partition("\t")
        if separator and serial:
            devices[serial] = state.strip()
    return devices


def _default_connect() -> socket.socket:
    host = os.environ.get("ANDROID_ADB_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("ANDROID_ADB_SERVER_PORT", "5037"))
    # A plain socket on purpose: adbutils would spawn "adb start-server" when the
    # server is down, undoing an explicit kill-server from the user.
    return socket.create_connection((host, port), timeout=2)


class DeviceTracker(QObject):
    """Background reader of host:track-devices.

    changed(dict) fires with the full {serial: state} map whenever the ADB
    server reports a change; server_changed(bool) reports whether the
    tracking connection is up. The tracker never starts the ADB server: when
    it is not running it simply retries every few seconds.
    """

    changed = Signal(object)
    server_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None, *, connect: Callable[[], socket.socket] | None = None,
                 retry_interval: float = 3.0) -> None:
        super().__init__(parent)
        self._connect = connect or _default_connect
        self._retry_interval = retry_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="adb-track-devices", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait)
            self._thread = None

    def _read_exact(self, sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            if self._stop.is_set():
                raise _Stopped()
            try:
                chunk = sock.recv(size - len(data))
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                raise ConnectionError("ADB Server 关闭了跟踪连接")
            data += chunk
        return bytes(data)

    def _run(self) -> None:
        while not self._stop.is_set():
            sock = None
            connected = False
            try:
                sock = self._connect()
                sock.settimeout(_POLL)
                request = b"host:track-devices"
                sock.sendall(b"%04x" % len(request) + request)
                status = self._read_exact(sock, 4)
                if status != b"OKAY":
                    raise ConnectionError(f"ADB Server 拒绝 track-devices：{status!r}")
                connected = True
                self.server_changed.emit(True)
                while True:
                    length = int(self._read_exact(sock, 4), 16)
                    payload = self._read_exact(sock, length).decode("utf-8", errors="replace")
                    self.changed.emit(parse_track_payload(payload))
            except _Stopped:
                pass
            except (OSError, ValueError):
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if connected and not self._stop.is_set():
                    self.server_changed.emit(False)
            self._stop.wait(self._retry_interval)

import socket
import threading

from sysdroid.core.devices import DeviceTracker, parse_track_payload


def frame(text):
    data = text.encode()
    return b"%04x" % len(data) + data


class ScriptedSocket:
    def __init__(self, chunks, hang=True):
        self.chunks = list(chunks)
        self.hang = hang
        self.sent = b""
        self.closed = False

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        if self.chunks:
            chunk = self.chunks.pop(0)
            if len(chunk) > size:
                self.chunks.insert(0, chunk[size:])
                chunk = chunk[:size]
            return chunk
        if self.hang:
            threading.Event().wait(0.02)
            raise socket.timeout()
        return b""

    def close(self):
        self.closed = True


def test_payload_parsing():
    assert parse_track_payload("a\tdevice\n10.0.0.2:5555\toffline\n\njunk\n") == {"a": "device", "10.0.0.2:5555": "offline"}
    assert parse_track_payload("") == {}


def test_tracker_reports_each_change_and_stops_promptly(qtbot):
    sock = ScriptedSocket([b"OKAY", frame("a\tdevice\n"), frame("a\toffline\nb\tunauthorized\n"), frame("")])
    tracker = DeviceTracker(connect=lambda: sock)
    seen, servers = [], []
    tracker.changed.connect(seen.append)
    tracker.server_changed.connect(servers.append)
    tracker.start()
    try:
        qtbot.waitUntil(lambda: len(seen) == 3, timeout=3000)
    finally:
        tracker.stop()
    assert seen == [{"a": "device"}, {"a": "offline", "b": "unauthorized"}, {}]
    assert servers == [True] and sock.sent == b"0012host:track-devices"
    assert sock.closed and not tracker.running


def test_tracker_retries_when_server_is_down_and_reconnects(qtbot):
    attempts = []
    good = ScriptedSocket([b"OKAY", frame("a\tdevice\n")])

    def connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionRefusedError("no server")
        if len(attempts) == 2:
            return ScriptedSocket([b"OKAY", frame("a\tdevice\n")], hang=False)  # server killed
        return good

    tracker = DeviceTracker(connect=connect, retry_interval=0.01)
    seen, servers = [], []
    tracker.changed.connect(seen.append)
    tracker.server_changed.connect(servers.append)
    tracker.start()
    try:
        qtbot.waitUntil(lambda: len(seen) == 2 and len(servers) == 3, timeout=3000)
    finally:
        tracker.stop()
    assert servers == [True, False, True] and len(attempts) == 3

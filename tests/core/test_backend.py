import json
from pathlib import Path
import sys
import threading

import pytest

from sysdroid.core.backend import TERMINAL_STATUSES, TaskRunner, parse_devices


def test_device_parser_preserves_unusable_states_without_daemon_noise():
    output = """* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
usb-a device product:p model:Pixel_9 device:panther transport_id:2
usb-b unauthorized transport_id:3
127.0.0.1:5555 offline transport_id:4
"""
    devices = parse_devices(output)
    assert [(device.serial, device.state) for device in devices] == [
        ("usb-a", "device"), ("usb-b", "unauthorized"), ("127.0.0.1:5555", "offline")]
    assert devices[0].model == "Pixel 9" and devices[0].transport == "2"


def test_finished_task_contains_complete_unicode_streams_and_real_failure(runner, qtbot):
    code = "import os,sys; data='首行\\n尾部🙂'.encode('utf-8'); os.write(1,data[:2]); os.write(1,data[2:]); os.write(2,'错误'.encode('utf-8')); sys.exit(5)"
    task = runner.start_process("unicode", sys.executable, ["-c", code])
    qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=10000)
    assert (task.status, task.exit_code, task.stdout, task.stderr) == ("failed", 5, "首行\n尾部🙂", "错误")


@pytest.mark.parametrize("outcome", ["cancelled", "timed_out"])
def test_stopping_adb_task_waits_for_result_and_preserves_first_outcome(runner, qtbot, monkeypatch, outcome):
    from sysdroid.core import backend as android_backend
    entered = threading.Event()
    release = threading.Event()

    def execute(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test request was not released")
        return android_backend._AdbResult(stdout="late result\n", exit_code=0)

    monkeypatch.setattr(runner, "_execute_adb", execute)
    task = runner.start_adb("stop adb", ["devices"], timeout=1)
    try:
        qtbot.waitUntil(entered.is_set, timeout=1000)
        if outcome == "timed_out":
            runner._adb_running[task.id].started -= 2
            runner._tick()
        runner.cancel(task.id)
        assert task.status == "stopping"
        assert task in runner.active() and task not in runner.history
        release.set()
        qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES, timeout=3000)
        assert (task.status, task.exit_code, task.stdout) == (outcome, None, "late result\n")
    finally:
        release.set()


def test_cancelling_queued_task_prevents_process_side_effects(runner, qtbot, tmp_path):
    marker = tmp_path / "should-not-exist"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"
    task = runner.start_process("queued", sys.executable, ["-c", code])
    runner.cancel(task.id)
    qtbot.wait(50)
    assert task.status == "cancelled" and task.exit_code is None
    assert not marker.exists()


def test_cleared_task_remains_in_persistent_history(runner):
    task = runner.start_adb("history", ["devices"], source="command")
    runner.cancel(task.id)
    history = list(runner.history)
    active = runner.start_adb("queued adb", ["devices"])
    runner.clear_finished()
    runner.flush_history()
    restored = TaskRunner(runner)
    saved = restored.get_task(task.id)
    assert task.id not in runner.tasks
    assert runner.history == history
    assert runner.tasks == {active.id: active} and runner.active() == [active]
    assert saved is not None and (saved.status, saved.exit_code) == ("cancelled", None)
    runner.cancel(active.id)


def test_clear_history_is_selective_and_keeps_current_tasks(runner):
    adb = runner.start_adb("old adb", ["devices"], source="command")
    runner._receive_output(adb.id, "stdout", "saved output\n")
    runner.cancel(adb.id)
    scrcpy = runner.start_process("other kind", "scrcpy", [], kind="scrcpy", source="command")
    runner.cancel(scrcpy.id)
    runner.flush_history()
    active = runner.start_adb("queued adb", ["devices"], source="command")
    tasks = runner.tasks.copy()
    changes = []
    runner.history_changed.connect(lambda: changes.append((
        [task.id for task in runner.history],
        [record["id"] for record in json.loads(runner._history_path.read_text(encoding="utf-8"))])))

    runner.clear_history("adb")

    assert runner.tasks == tasks
    assert all(runner.tasks[task_id] is task for task_id, task in tasks.items())
    assert runner.active() == [active]
    assert runner.get_task(adb.id) is adb and adb.stdout == "saved output\n"
    assert runner.history == [scrcpy]
    assert changes == [([scrcpy.id], [scrcpy.id])]
    restored = TaskRunner(runner)
    assert restored.get_task(adb.id) is None
    assert restored.get_task(active.id) is None
    assert [task.id for task in restored.history] == [scrcpy.id]

    runner.clear_history("adb")
    assert changes == [([scrcpy.id], [scrcpy.id])]
    runner.cancel(active.id)


def test_clear_history_preserves_running_adb_and_records_its_completion(runner, qtbot, monkeypatch):
    from sysdroid.core import backend as android_backend
    old = runner.start_adb("old adb", ["devices"], source="command")
    runner.cancel(old.id)
    entered = threading.Event()
    release = threading.Event()

    def execute(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test request was not released")
        return android_backend._AdbResult(stdout="new result\n", exit_code=0)

    monkeypatch.setattr(runner, "_execute_adb", execute)
    active = runner.start_adb("running adb", ["devices"], source="command")
    try:
        qtbot.waitUntil(entered.is_set, timeout=1000)
        running = runner._adb_running[active.id]
        runner._receive_output(active.id, "stdout", "existing output\n")
        runner.clear_history("adb")

        assert runner.history == []
        assert runner.tasks[old.id] is old and runner.tasks[active.id] is active
        assert runner.active() == [active] and active.status == "running"
        assert runner._adb_running[active.id] is running and running.thread.is_alive()
        assert active.stdout == "existing output\n"
        assert TaskRunner(runner).history == []

        release.set()
        qtbot.waitUntil(lambda: active.status in TERMINAL_STATUSES, timeout=3000)
        assert (active.status, active.exit_code, active.stdout) == (
            "succeeded", 0, "existing output\nnew result\n")
        assert runner.history == [active]
        runner.flush_history()
        restored = TaskRunner(runner)
        assert restored.get_task(old.id) is None
        saved = restored.get_task(active.id)
        assert saved is not None and (saved.status, saved.stdout) == (active.status, active.stdout)
    finally:
        release.set()


def _fail_history_commit(monkeypatch, history_path, stage):
    temporary = history_path.with_suffix(".tmp")
    write_text = Path.write_text
    replace = Path.replace

    def fail_write(path, text, *args, **kwargs):
        if path == temporary:
            write_text(path, "partial write", encoding="utf-8")
            raise OSError("history write failed")
        return write_text(path, text, *args, **kwargs)

    def fail_replace(path, target):
        if path == temporary:
            assert path.is_file()
            raise OSError("history replace failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "write_text" if stage == "write" else "replace",
                        fail_write if stage == "write" else fail_replace)


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_clear_history_storage_failure_preserves_disk_memory_and_signals(runner, monkeypatch, stage):
    adb = runner.start_adb("saved adb", ["devices"], source="command")
    runner.cancel(adb.id)
    other = runner.start_process("saved process", "scrcpy", [], kind="scrcpy", source="command")
    runner.cancel(other.id)
    runner.flush_history()
    active = runner.start_adb("queued adb", ["devices"])
    original = runner._history_path.read_bytes()
    history = runner.history
    tasks = runner.tasks.copy()
    changes = []
    errors = []
    runner.history_changed.connect(lambda: changes.append(True))
    runner.error.connect(errors.append)

    with monkeypatch.context() as patch:
        _fail_history_commit(patch, runner._history_path, stage)
        with pytest.raises(OSError, match=f"history {stage} failed"):
            runner.clear_history("adb")

    assert runner._history_path.read_bytes() == original
    assert runner.history is history and runner.history == [adb, other]
    assert runner.tasks == tasks and runner.active() == [active]
    assert changes == [] and errors == []
    assert not runner._history_path.with_suffix(".tmp").exists()
    assert [task.id for task in TaskRunner(runner).history] == [adb.id, other.id]

    runner.clear_history("adb")
    assert runner.history == [other] and changes == [True]
    runner.cancel(active.id)


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_completion_storage_failure_emits_error_without_raising(runner, monkeypatch, stage):
    old = runner.start_adb("saved adb", ["devices"], source="command")
    runner.cancel(old.id)
    runner.flush_history()
    original = runner._history_path.read_bytes()
    task = runner.start_adb("new adb", ["devices"], source="command")
    errors = []
    finished = []
    runner.error.connect(errors.append)
    runner.task_finished.connect(finished.append)

    with monkeypatch.context() as patch:
        _fail_history_commit(patch, runner._history_path, stage)
        runner.cancel(task.id)
        runner.flush_history()

    assert task.status == "cancelled" and runner.history == [old, task]
    assert finished == [task]
    assert len(errors) == 1 and f"history {stage} failed" in errors[0]
    assert runner._history_path.read_bytes() == original
    assert not runner._history_path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("original", [
    b"{broken json",
    json.dumps([{"id": "unfinished", "title": "bad record", "program": "adb",
                 "args": [], "status": "running"}]).encode("utf-8"),
])
def test_clear_history_rejects_corrupt_readonly_storage(runner, original):
    runner._history_path.write_bytes(original)
    readonly = TaskRunner(runner)
    history = readonly.history
    changes = []
    readonly.history_changed.connect(lambda: changes.append(True))

    with pytest.raises(ValueError, match="原文件保留"):
        readonly.clear_history("adb")

    assert readonly._history_path.read_bytes() == original
    assert readonly.history is history and readonly.tasks == {}
    assert changes == []
    assert not readonly._history_path.with_suffix(".tmp").exists()


def test_transient_sampling_preserves_history_and_pin_until_released(runner, qtbot):
    ordinary = runner.start_adb("user output", ["devices"], source="command")
    runner.cancel(ordinary.id)
    runner.flush_history()
    original = runner._history_path.read_bytes()
    original_mtime = runner._history_path.stat().st_mtime_ns
    changes = []
    removed = []
    runner.history_changed.connect(lambda: changes.append(True))
    runner.task_removed.connect(removed.append)
    first = runner.start_adb("sample", ["devices"], transient=True)
    runner._receive_output(first.id, "stdout", "pinned original🙂")
    runner.pin_task(first.id)
    runner.cancel(first.id)
    for _ in range(50):
        task = runner.start_adb("sample", ["devices"], transient=True)
        runner.cancel(task.id)
        qtbot.wait(1)
    runner.flush_history()
    assert len([task for task in runner.tasks.values() if task.transient]) <= 21
    assert runner.get_task(first.id).stdout == "pinned original🙂"
    assert runner.history == [ordinary] and changes == []
    assert runner._history_path.read_bytes() == original
    assert runner._history_path.stat().st_mtime_ns == original_mtime
    runner.clear_finished()
    assert runner.get_task(first.id).stdout == "pinned original🙂"
    runner.unpin_task(first.id)
    for _ in range(21):
        task = runner.start_adb("sample", ["devices"], transient=True)
        runner.cancel(task.id)
    qtbot.wait(1)
    assert runner.get_task(first.id) is None and first.id in removed


def test_active_count_includes_queue_and_cancelled_requests_waiting_for_return(runner, qtbot, monkeypatch):
    from sysdroid.core import backend as android_backend
    entered, release = threading.Event(), threading.Event()
    counts = []
    runner.active_count_changed.connect(counts.append)
    def execute(*args):
        entered.set()
        release.wait(5)
        return android_backend._AdbResult(stdout="last")
    monkeypatch.setattr(runner, "_execute_adb", execute)
    task = runner.start_adb("slow", ["devices"], transient=True)
    try:
        assert runner.active_count == 1 and task.status == "starting"
        qtbot.waitUntil(entered.is_set)
        runner.cancel(task.id)
        assert runner.active_count == 1 and task.status == "stopping"
        release.set()
        qtbot.waitUntil(lambda: task.status in TERMINAL_STATUSES)
        assert runner.active_count == 0 and counts == [1, 0]
    finally:
        release.set()


def test_buffer_truncation_retains_both_unicode_tails_before_finished(runner, monkeypatch):
    from sysdroid.core import backend as android_backend
    monkeypatch.setattr(android_backend, "OUTPUT_LIMIT", 128)
    task = runner.start_adb("buffered", ["devices"], transient=True)
    finished = []
    runner.task_finished.connect(lambda task: finished.append((task.stdout, task.stderr)))
    for stream in ("stdout", "stderr"):
        runner._queue_output(task.id, stream, "x" * 256)
        runner._queue_output(task.id, stream, "尾部🙂")
    runner.cancel(task.id)
    assert finished == [(task.stdout, task.stderr)]
    assert all(text.endswith("尾部🙂") and len(text) <= 128 and "截断" in text for text in finished[0])

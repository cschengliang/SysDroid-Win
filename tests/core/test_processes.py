import re
import shlex

import pytest
from PySide6.QtCore import QObject, Qt, Signal

from sysdroid.core.backend import TRUNCATED, Task
from sysdroid.core.processes import (
    ProcessController, aggregate_cpu_percent, apply_cpu_delta, parse_activity_record,
    parse_meminfo, parse_process_details, parse_process_sample, parse_process_stat,
)
from sysdroid.ui.pages.process_page import ProcessPage


NONCE = "fixture42"


def stat(pid=42, *, name="worker (with) spaces)", start=1000, ticks=80,
         rss_pages=3, size=65536, ppid=1, threads=2, priority=20, nice=0, processor=0):
    fields = ["0"] * (37 if processor is not None else 22)
    values = {0: "S", 1: ppid, 11: ticks - 10, 12: 10, 15: priority, 16: nice,
              17: threads, 19: start, 20: size, 21: rss_pages}
    if processor is not None:
        values[36] = processor
    for index, value in values.items():
        fields[index] = str(value)
    return f"{pid} ({name}) " + " ".join(fields) + "\n"


def frame(sections, nonce=NONCE):
    return f"FRAME {nonce}\n" + "".join(
        f"BEGIN {nonce} {name}\n{text}\nEND {nonce} {name} {rc}\n"
        for name, text, rc in sections) + f"DONE {nonce}\n"


def sample_sections(*, records=None, total=1000, idle=300, ps=None, denied=(),
                    global_rc=0, status_rss="7 kB", status_size="99 kB",
                    mode="columns", hz="100", pages="4096", after_start=None,
                    status_swap="0 kB", priority=20, nice=0, processor=0, extra_stat=""):
    """Device response in the bulk format; ``denied`` PIDs have no /proc records."""
    records = records if records is not None else [(42, 1000, 80)]
    if ps is None:
        ps = "PID UID PPID S NAME RSS VSZ ETIME ARGS\n" + "".join(
            f"{pid} 10042 1 S worker 9 88 01:30 /system/bin/worker --value 空 格\n"
            for pid, start, ticks in records)
    before = after = statuses = ""
    for pid, start, ticks in records:
        if pid in denied:
            continue
        before += stat(pid, start=start, ticks=ticks, priority=priority, nice=nice, processor=processor)
        after += stat(pid, start=after_start if after_start is not None else start, ticks=ticks,
                      priority=priority, nice=nice, processor=processor)
        lines = [f"Pid:\t{pid}", "Uid:\t10042 10042 10042 10042", "Threads:\t3"]
        for key, value in (("VmRSS", status_rss), ("VmSize", status_size), ("VmSwap", status_swap)):
            if value is not None:
                lines.append(f"{key}:\t{value}")
        statuses += "".join(f"/proc/{pid}/status:{line}\n" for line in lines)
    # Bulk reads return 1 when any PID vanished mid-read; that is not a failure.
    return [
        ("ps-help", "RSS Resident Set Size (KiB)\nVSZ Virtual memory size (KiB)\n", 0),
        ("ps-mode", mode + "\n", 0),
        ("clock-ticks", hz + "\n", 0 if hz else 1),
        ("page-size", pages + "\n", 0 if pages else 1),
        ("ps", ps, 0),
        # Deliberately include guest: it must not be counted a second time.
        ("cpu", f"cpu {total - idle} 0 0 {idle} 0 0 0 0 9999 8888\nbtime 1700000000\n", global_rc),
        ("uptime", "100.00 70.00\n", global_rc),
        ("memory", "MemTotal: 1024 kB\nMemAvailable: 768 kB\n", global_rc),
        ("stats-before", before + extra_stat, 1 if denied else 0),
        ("statuses", statuses, 1 if denied else 0),
        ("stats-after", after + extra_stat, 1 if denied else 0),
    ]


def sample(**kwargs):
    return parse_process_sample(frame(sample_sections(**kwargs)), nonce=NONCE, clock_ticks=100, page_size=4096)


def detail_sections(*, pid=42, start=1000, after_start=None, cmdline="worker\0 空格\0中文\n\0", cmd_rc=0, uid=10042):
    return [
        ("stat-before", stat(pid, start=start), 0),
        ("cmdline", cmdline, cmd_rc),
        ("status", f"Pid:\t{pid}\nUid:\t{uid} {uid} {uid} {uid}\n", 0),
        ("meminfo", "Applications Memory Usage (in Kilobytes):\nTOTAL PSS: 12 TOTAL RSS: 35 TOTAL SWAP PSS: 4\n", 0),
        ("activity", f"  *APP* ProcessRecord{{hash {pid}:worker/u0a42}}\n    uid={uid} startSeq=17\n    startReason=activity launch\n    hostingType=activity\n", 0),
        ("stat-after", stat(pid, start=after_start if after_start is not None else start), 0),
    ]


class ManualProcessRunner(QObject):
    task_added = Signal(object)
    task_changed = Signal(object)
    task_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.pending = {}
        self.maximum_pending = 0
        self.start_error = None

    def start_adb(self, title, args, serial="", command_id="", timeout=10, *, transient=False):
        if self.start_error:
            error, self.start_error = self.start_error, None
            raise error
        task = Task(f"process-{len(self.requests) + 1}", title, "adb", list(args), serial,
                    command_id, transient=transient)
        task.status = "running"
        task.test_timeout = timeout
        self.requests.append(task)
        self.pending[task.id] = task
        self.maximum_pending = max(self.maximum_pending, len(self.pending))
        self.task_added.emit(task)
        return task

    def finish(self, task, stdout="", stderr="", exit_code=0, status=None):
        self.pending.pop(task.id)
        task.stdout, task.stderr, task.exit_code = stdout, stderr, exit_code
        task.status = status or ("succeeded" if exit_code == 0 else "failed")
        self.task_changed.emit(task)
        self.task_finished.emit(task)


def request_nonce(task):
    # This extracts the request correlation token to generate a device response,
    # rather than asserting implementation/source or echoing its command argv.
    script = shlex.split(task.args[1])[2]
    return re.search(r"(?m)^nonce=([a-z0-9]+)$", script).group(1)


def finish_sample(runner, task=None, **kwargs):
    task = task or runner.requests[-1]
    runner.finish(task, frame(sample_sections(**kwargs), request_nonce(task)))


def finish_details(runner, task=None, **kwargs):
    task = task or runner.requests[-1]
    runner.finish(task, frame(detail_sections(**kwargs), request_nonce(task)))


@pytest.fixture(autouse=True)
def synchronous_parsing(monkeypatch):
    # Parse inline so slot / publication ordering stays deterministic; the
    # real worker thread is covered by a dedicated test.
    monkeypatch.setattr(ProcessController, "_thread_executor", lambda self, job, done: done(job()))


@pytest.fixture
def process_runner(qapp):
    return ManualProcessRunner()


@pytest.fixture
def controller(process_runner):
    result = ProcessController(process_runner)
    result.set_device("device-a")
    yield result
    result.set_active(False)


def test_stat_uses_last_parenthesis_and_real_kernel_field_offsets():
    parsed = parse_process_stat(stat(name="a ) nested (name))", start=12345, ticks=90,
                                     rss_pages=13, size=123456, ppid=7, threads=9))
    assert (parsed.pid, parsed.name, parsed.ppid, parsed.cpu_ticks, parsed.start_ticks,
            parsed.vss_bytes, parsed.rss_pages, parsed.threads) == (
        42, "a ) nested (name))", 7, 90, 12345, 123456, 13, 9)


def test_sample_exposes_swap_and_signed_scheduling_fields_in_real_units():
    sections = sample_sections(status_swap="3 kB", hz="250", priority=-2, nice=-7, processor=0)
    info = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=250, page_size=4096).processes[42]
    assert info.swap_bytes == 3072
    assert (info.priority, info.nice, info.processor) == (-2, -7, 0)
    assert info.cpu_seconds == pytest.approx(80 / 250)


@pytest.mark.parametrize("swap,expected", [("0 kB", 0), (None, None), ("unknown", None), ("17 MB", None)])
def test_swap_zero_is_distinct_from_missing_or_unrecognized_units(swap, expected):
    info = sample(status_swap=swap).processes[42]
    assert info.swap_bytes == expected
    if expected is None:
        assert info.unavailable["swap_bytes"]


def test_missing_optional_stat_fields_and_clock_scale_keep_reliable_identity():
    sections = sample_sections(priority="unknown", nice=-1, processor=None, hz="")
    info = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=None, page_size=4096).processes[42]
    assert (info.pid, info.start_ticks, info.cpu_ticks, info.nice) == (42, 1000, 80, -1)
    assert info.priority is None and info.processor is None and info.cpu_seconds is None
    assert {"priority", "processor", "cpu_seconds"} <= info.unavailable.keys()


def test_pid_reuse_discards_extended_fields_instead_of_mixing_processes():
    info = sample(after_start=2000, status_swap="3 kB", nice=-7, processor=2).processes[42]
    assert (info.swap_bytes, info.priority, info.nice, info.processor, info.cpu_seconds) == (None,) * 5
    assert "PID" in info.unavailable["swap_bytes"]


@pytest.mark.parametrize("output", ["", "42 broken", "0 (worker) S 1", "42 (worker) S 1", "42 (worker) " + "x " * 22])
def test_stat_rejects_partial_and_invalid_identity_records(output):
    with pytest.raises(ValueError):
        parse_process_stat(output)


def test_reordered_ps_headers_command_tail_and_numeric_uid_are_preserved():
    ps = "STAT PPID UID PID RSS VSZ NAME ARGS\nS 7 10042 42 9 88 worker /system/bin/worker --arg 中文 value with spaces\n"
    result = sample(ps=ps).processes[42]
    assert result.command_line == "/system/bin/worker --arg 中文 value with spaces"
    assert result.uid == 10042
    assert result.name == "worker (with) spaces)"
    assert result.rss_bytes == 7 * 1024
    assert result.vss_bytes == 99 * 1024
    assert result.started_at == 1700000010
    assert result.elapsed_seconds == 90
    assert result.cpu_percent is None


def test_named_user_is_not_an_invented_numeric_uid_and_ps_remains_available():
    result = sample(ps="USER PID PPID S NAME\nshell 42 1 S worker\n", denied=(42,)).processes[42]
    assert result.uid is None and "uid" in result.unavailable
    assert result.name == "worker" and result.pid == 42
    assert result.start_ticks is None and result.cpu_ticks is None
    assert result.rss_bytes is None


def test_rss_prefers_status_then_ps_then_stat_with_actual_page_size():
    assert sample().processes[42].rss_bytes == 7 * 1024
    assert sample(status_rss=None).processes[42].rss_bytes == 9 * 1024
    sections = sample_sections(status_rss=None, ps="PID UID PPID S NAME\n42 10042 1 S worker\n")
    result = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=100, page_size=16384)
    assert result.processes[42].rss_bytes == 3 * 16384
    result = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=100, page_size=None)
    assert result.processes[42].rss_bytes is None


def test_unknown_ps_units_are_not_assumed_to_be_host_units():
    sections = [(name, "RSS VSZ memory size\n" if name == "ps-help" else text, rc)
                for name, text, rc in sample_sections(denied=(42,))]
    info = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=None, page_size=None).processes[42]
    assert info.rss_bytes is None and info.vss_bytes is None


def test_actual_hz_controls_epoch_and_no_hz_uses_only_provided_etime():
    sections = sample_sections()
    info = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=250, page_size=4096).processes[42]
    assert info.started_at == 1700000004 and info.elapsed_seconds == 96
    info = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=None, page_size=4096).processes[42]
    assert info.started_at is None and info.elapsed_seconds == 90
    no_etime = sample_sections(ps="PID UID PPID S NAME\n42 10042 1 S worker\n")
    info = parse_process_sample(frame(no_etime), nonce=NONCE, clock_ticks=None, page_size=4096).processes[42]
    assert info.elapsed_seconds is None


def test_aggregate_cpu_ignores_guest_and_counts_iowait_as_idle():
    sections = [(name, "cpu 100 20 30 40 50 60 70 80 9999 8888\nbtime 1700000000\n" if name == "cpu" else text, rc)
                for name, text, rc in sample_sections()]
    result = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=100, page_size=4096)
    assert result.total_ticks == 450 and result.idle_ticks == 90
    assert result.mem_total_bytes == 1024 * 1024
    assert result.mem_available_bytes == 768 * 1024


def test_cpu_is_whole_machine_delta_not_core_scaled_and_zero_is_real_zero():
    previous = sample(records=[(42, 1000, 80)], total=1000, idle=300)
    current = sample(records=[(42, 1000, 120)], total=1400, idle=400)
    assert apply_cpu_delta(previous, current).processes[42].cpu_percent == 10.0
    assert aggregate_cpu_percent(previous, current) == 75.0
    assert apply_cpu_delta(None, current).processes[42].cpu_percent is None
    zero = sample(records=[(42, 1000, 80)], total=1400)
    assert apply_cpu_delta(previous, zero).processes[42].cpu_percent == 0.0
    # CPU ratios use kernel ticks and do not require CLK_TCK.
    no_hz = parse_process_sample(frame(sample_sections(records=[(42, 1000, 120)], total=1400)),
                                 nonce=NONCE, clock_ticks=None, page_size=None)
    assert apply_cpu_delta(previous, no_hz).processes[42].cpu_percent == 10.0


@pytest.mark.parametrize("records,total", [([(42, 1001, 120)], 1400), ([(42, 1000, 70)], 1400),
                                          ([(42, 1000, 120)], 1000), ([(42, 1000, 120)], 900)])
def test_cpu_unknown_for_pid_reuse_counter_rollback_and_bad_denominator(records, total):
    previous = sample()
    result = apply_cpu_delta(previous, sample(records=records, total=total))
    assert result.processes[42].cpu_percent is None
    assert result.processes[42].unavailable["cpu_percent"]


def test_single_pid_denial_and_global_denial_publish_only_available_fields():
    global_denied = sample(global_rc=1)
    assert global_denied.total_ticks is None and global_denied.mem_available_bytes is None
    assert global_denied.processes[42].rss_bytes == 7 * 1024
    assert global_denied.processes[42].started_at is None
    sections = sample_sections(records=[(42, 1000, 80), (43, 2000, 40)], denied=(43,))
    result = parse_process_sample(frame(sections), nonce=NONCE, clock_ticks=100, page_size=4096)
    assert result.processes[42].start_ticks == 1000
    assert result.processes[43].start_ticks is None and result.processes[43].rss_bytes == 9 * 1024
    assert result.total_ticks == 1000


def test_pid_reuse_between_stat_reads_discards_mixed_proc_fields():
    info = sample(after_start=2000).processes[42]
    assert info.name == "worker"
    assert info.start_ticks is None and info.cpu_ticks is None and info.threads is None
    assert info.rss_bytes == 9 * 1024  # ps-only, not the discarded status RSS
    assert "PID" in info.unavailable["start_ticks"]


@pytest.mark.parametrize("transform", [
    lambda value: value.removesuffix(f"DONE {NONCE}\n"),
    lambda value: value[:-30],
    lambda value: "diagnostic\n" + value,
    lambda value: value + "trailing diagnostic\n",
    lambda value: TRUNCATED + value,
    lambda value: value.replace(f"END {NONCE} ps 0", f"END {NONCE} ps 1"),
    lambda value: value.replace("PID UID PPID S NAME RSS VSZ ETIME ARGS", "no process header"),
    lambda value: value.replace("42 10042 1 S worker 9 88 01:30", "42 10042"),
])
def test_incomplete_critical_or_truncated_frames_never_become_successful_snapshots(transform):
    with pytest.raises(ValueError):
        parse_process_sample(transform(frame(sample_sections())), nonce=NONCE, clock_ticks=100, page_size=4096)


def test_missing_segments_duplicate_segments_and_extra_pid_are_rejected():
    sections = sample_sections()
    for bad in ([entry for entry in sections if entry[0] != "memory"],
                [entry for entry in sections if entry[0] != "stats-after"],
                sections + [sections[4]],
                sample_sections(extra_stat=stat(42, start=1000)),
                sections + [("pid:42:status", "Pid: 42\n", 0)]):
        with pytest.raises(ValueError):
            parse_process_sample(frame(bad), nonce=NONCE, clock_ticks=100, page_size=4096)


def test_empty_ps_membership_is_success_only_with_valid_header():
    result = sample(records=[], ps="PID UID PPID S NAME\n")
    assert result.processes == {} and result.total_ticks == 1000
    with pytest.raises(ValueError):
        sample(records=[], ps="")


def test_cmdline_nul_arguments_and_empty_success_are_distinct_from_permission_denial():
    details = parse_process_details(frame(detail_sections()), nonce=NONCE, pid=42, start_ticks=1000, uid=10042)
    assert details.argv == ("worker", " 空格", "中文\n")
    assert details.pss_bytes == 12 * 1024 and details.swap_pss_bytes == 4 * 1024
    assert details.start_sequence == 17 and details.start_reason == "activity launch"
    assert details.hosting_type == "activity"
    empty = parse_process_details(frame(detail_sections(cmdline="")), nonce=NONCE, pid=42, start_ticks=1000, uid=10042)
    denied = parse_process_details(frame(detail_sections(cmdline="", cmd_rc=1)), nonce=NONCE, pid=42, start_ticks=1000, uid=10042)
    assert empty.argv == () and denied.argv is None
    assert "argv" in denied.unavailable


@pytest.mark.parametrize("kwargs", [{"after_start": 2000}, {"start": 2000}, {"uid": 10043}])
def test_details_reject_reused_pid_or_changed_uid(kwargs):
    with pytest.raises(ValueError):
        parse_process_details(frame(detail_sections(**kwargs)), nonce=NONCE, pid=42, start_ticks=1000, uid=10042)


def test_meminfo_requires_units_or_explicit_table_header():
    assert parse_meminfo("TOTAL PSS: 12 TOTAL SWAP PSS: 4") == (None, None)
    assert parse_meminfo("TOTAL PSS: 12 kB TOTAL SWAP PSS: 4 kB") == (12288, 4096)
    assert parse_meminfo("Applications Memory Usage (in Kilobytes):\nPss Private Private SwapPss Rss\nTotal Dirty Clean Dirty Total\nTOTAL 10 2 3 4 20\n") == (10240, 4096)
    assert parse_meminfo("RSS: 200 kB\nSwap: 20 kB") == (None, None)


def test_am_requires_exact_pid_and_decoded_uid_and_never_guesses_native_hosting():
    output = ("  *APP* ProcessRecord{one 42:worker/u0a42}\n    startSeq=1 hostingType=service\n"
              "  *APP* ProcessRecord{two 42:worker/u10a42}\n    startSeq=2\n    startReason=broadcast\n    hostingType=broadcast\n"
              "  *APP* ProcessRecord{three 43:worker/u10a42}\n    startSeq=3\n")
    assert parse_activity_record(output, 42, 1010042) == {
        "start_sequence": 2, "start_reason": "broadcast", "hosting_type": "broadcast"}
    assert parse_activity_record(output, 42, None) == {}
    assert parse_activity_record(output, 42, 1000) == {}
    system = "  ProcessRecord{one 42:system/u0s1000}\n    startSeq=9\n"
    assert parse_activity_record(system, 42, 1000) == {"start_sequence": 9}
    assert parse_activity_record(system + system, 42, 1000) == {}
    assert parse_activity_record("  ProcessRecord{one 42:worker/u0a42\n    hostingType=service\n", 42, 10042) == {}


def test_am_detail_header_is_not_ambiguous_with_home_uid_and_pid_references():
    output = (
        "  *APP* UID 10084 ProcessRecord{same 1223:com.android.launcher3/u0a84}\n"
        "    user #0 uid=10084 gids={50084, 20084}\n"
        "    pid=1223\n"
        "    startSeq=10\n"
        "    currentHostingComponentTypes=0x200\n"
        "  mHomeProcess: ProcessRecord{same 1223:com.android.launcher3/u0a84}\n"
        "    UID u0a84: UidRecord{uidhash u0a84 TOP}\n"
        "      proc=ProcessRecord{same 1223:com.android.launcher3/u0a84}\n"
        "        startSeq=99\n"
        "    PID #1223: ProcessRecord{same 1223:com.android.launcher3/u0a84}\n"
    )
    assert parse_activity_record(output, 1223, 10084) == {"start_sequence": 10}
    assert parse_activity_record(output, 1223, 10085) == {}


def test_controller_visible_first_frame_and_manual_pause_semantics(process_runner, controller):
    assert not process_runner.requests
    controller.set_active(True)
    first = process_runner.requests[-1]
    assert controller.busy and first.transient
    finish_sample(process_runner, first)
    assert controller.sample.processes[42].cpu_percent is None
    assert controller.sampled_at is not None and not controller.error
    controller.set_auto_refresh(False)
    old = controller.sample
    refresh = controller.refresh()
    assert not refresh.transient and controller.sample is old
    finish_sample(process_runner, refresh, total=1400, records=[(42, 1000, 120)])
    assert controller.sample.processes[42].cpu_percent == 10
    assert not controller.busy


def test_slow_request_skips_ticks_without_queue_or_overlap(process_runner, controller, qtbot):
    controller.set_active(True)
    first = process_runner.requests[-1]
    qtbot.wait(2100)
    assert process_runner.requests == [first] and process_runner.maximum_pending == 1
    controller.set_active(False)
    finish_sample(process_runner, first)
    assert controller.sample is None  # hidden late result is not published
    qtbot.wait(2100)
    assert len(process_runner.requests) == 1


def test_switch_keeps_physical_slot_until_old_device_returns(process_runner, controller, qtbot):
    controller.set_active(True)
    old = process_runner.requests[-1]
    controller.set_device("device-b")
    assert controller.busy and len(process_runner.requests) == 1 and controller.sample is None
    finish_sample(process_runner, old)
    assert controller.sample is None
    qtbot.waitUntil(lambda: len(process_runner.requests) == 2)
    current = process_runner.requests[-1]
    assert current.serial == "device-b" and process_runner.maximum_pending == 1
    finish_sample(process_runner, current, records=[(43, 2000, 40)])
    assert set(controller.sample.processes) == {43}
    assert controller.sample.processes[43].cpu_percent is None


def test_hide_return_during_slow_request_restarts_first_frame_without_old_publication(process_runner, controller, qtbot):
    controller.set_active(True)
    first = process_runner.requests[-1]
    controller.set_active(False)
    controller.set_active(True)
    assert len(process_runner.requests) == 1
    finish_sample(process_runner, first)
    assert controller.sample is None
    qtbot.waitUntil(lambda: len(process_runner.requests) == 2)
    finish_sample(process_runner, total=1400, records=[(42, 1000, 120)])
    assert controller.sample.processes[42].cpu_percent is None
    assert process_runner.maximum_pending == 1


def test_offline_stops_future_requests_and_discards_old_snapshot(process_runner, controller, qtbot):
    controller.set_active(True)
    first = process_runner.requests[-1]
    controller.set_device("device-a", "offline")
    finish_sample(process_runner, first)
    assert controller.sample is None and not controller.busy
    qtbot.wait(2100)
    assert len(process_runner.requests) == 1
    with pytest.raises(ValueError):
        controller.refresh()
    controller.set_device("device-a", "device")
    assert len(process_runner.requests) == 2
    finish_sample(process_runner)
    assert controller.sample is not None


def test_reentrant_task_added_switch_cannot_create_a_second_physical_request(process_runner, controller, qtbot):
    def switch(task):
        if task.serial == "device-a":
            controller.set_device("device-b")
    process_runner.task_added.connect(switch)
    controller.set_active(True)
    old = process_runner.requests[-1]
    assert len(process_runner.requests) == 1 and controller.busy
    finish_sample(process_runner, old)
    qtbot.waitUntil(lambda: len(process_runner.requests) == 2)
    finish_sample(process_runner)
    assert controller.serial == "device-b" and process_runner.maximum_pending == 1


def test_failed_frame_keeps_snapshot_and_next_cpu_uses_last_valid_frame(process_runner, controller):
    controller.set_active(True)
    finish_sample(process_runner)
    controller.set_auto_refresh(False)
    previous = controller.sample
    bad = controller.refresh()
    process_runner.finish(bad, frame(sample_sections(), request_nonce(bad)).removesuffix(f"DONE {request_nonce(bad)}\n"))
    assert controller.sample is previous and controller.error
    good = controller.refresh()
    finish_sample(process_runner, good, total=1400, records=[(42, 1000, 120)])
    assert not controller.error and controller.sample.processes[42].cpu_percent == 10


def test_real_hz_failure_does_not_disable_tick_cpu_ratio(process_runner, controller):
    controller.set_active(True)
    finish_sample(process_runner, hz="", pages="")
    assert controller.sample.processes[42].started_at is None
    assert controller.sample.processes[42].elapsed_seconds == 90
    controller.set_auto_refresh(False)
    task = controller.refresh()
    finish_sample(process_runner, task, total=1400, records=[(42, 1000, 120)])
    assert controller.sample.processes[42].cpu_percent == 10
    assert controller.sample.processes[42].started_at is None


def test_capability_snapshot_remains_device_scoped_until_device_change(process_runner, controller):
    controller.set_active(True)
    finish_sample(process_runner, mode="all", hz="250", pages="16384")
    assert controller.sample.processes[42].started_at == 1700000004
    controller.set_auto_refresh(False)
    request = controller.refresh()
    # Later response extras must not replace the first device capability snapshot.
    finish_sample(process_runner, request, mode="columns", hz="100", pages="4096")
    assert controller.sample.processes[42].started_at == 1700000004
    controller.set_device("device-b")
    finish_sample(process_runner, hz="100")
    assert controller.sample.processes[42].started_at == 1700000010


def test_submission_failure_releases_slot_and_manual_retry_can_publish(process_runner, controller):
    process_runner.start_error = OSError("ADB transport unavailable")
    with pytest.raises(ValueError):
        controller.refresh()
    assert not controller.busy and "ADB transport unavailable" in controller.error
    request = controller.refresh()
    finish_sample(process_runner, request)
    assert controller.sample is not None and not controller.error


def test_details_share_slot_do_not_change_baseline_and_reject_pid_reuse(process_runner, controller, qtbot):
    controller.set_active(True)
    finish_sample(process_runner)
    detail = controller.load_details(42, 1000)
    assert not detail.transient and controller.busy
    qtbot.wait(2100)
    assert process_runner.requests[-1] is detail
    finish_details(process_runner, detail)
    assert controller.details.argv == ("worker", " 空格", "中文\n")
    controller.set_auto_refresh(False)
    task = controller.refresh()
    finish_sample(process_runner, task, total=1400, records=[(42, 1000, 120)])
    assert controller.sample.processes[42].cpu_percent == 10
    reused = controller.load_details(42, 1000)
    finish_details(process_runner, reused, after_start=2000)
    assert controller.details is None and controller.error
    assert controller.sample.processes[42].start_ticks == 1000
    assert process_runner.maximum_pending == 1


def test_no_reliable_start_disables_unsafe_details_and_raw_failure_survives(process_runner, controller):
    controller.set_active(True)
    finish_sample(process_runner, denied=(42,))
    with pytest.raises(ValueError):
        controller.load_details(42, None)
    controller.set_auto_refresh(False)
    task = controller.refresh()
    process_runner.finish(task, "partial raw stdout", "Permission denied 原始 stderr", 1)
    assert "partial raw stdout" in controller.error
    assert "Permission denied 原始 stderr" in controller.error
    assert controller.sample.processes[42].start_ticks is None


def test_page_incremental_rows_numeric_sort_filter_and_identity_selection(process_runner, qtbot):
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner, records=[(42, 1000, 80), (100, 2000, 40)])
    page.controller.set_auto_refresh(False)
    page.table.sortItems(0, Qt.SortOrder.AscendingOrder)
    assert [page.table.item(row, 0).text() for row in range(2)] == ["42", "100"]
    page.table.setCurrentCell(0, 0)
    selected = page._selected_identity()
    retained = page.table.item(0, 0)
    task = page.controller.refresh()
    finish_sample(process_runner, task, records=[(42, 1000, 120), (100, 2000, 40)], total=1400)
    assert page._selected_identity() == selected
    assert page.table.item(page.table.currentRow(), 0) is retained
    page.search.setText("2000")
    qtbot.waitUntil(lambda: page.table.isRowHidden(page.table.currentRow()))
    assert page._selected_identity() == selected
    page.search.clear()
    qtbot.waitUntil(lambda: not page.table.isRowHidden(page.table.currentRow()))
    page.set_active(False)


def test_column_visibility_and_double_click_keep_selected_process_identity(process_runner, qtbot):
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    page.resize(1200, 700)
    page.show()
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner, records=[(42, 1000, 80), (100, 2000, 40)])
    page.auto_box.setChecked(False)
    page.table.sortItems(0, Qt.SortOrder.AscendingOrder)
    page.table.setCurrentCell(1, 0)
    retained = page.table.item(1, 0)
    page.set_visible_columns(["pid", "name", "swap_bytes", "nice", "cpu_seconds"])
    assert page._selected_identity() == (100, 2000)
    assert page.table.item(1, 0) is retained
    assert page.table.isColumnHidden(5)
    task = page.controller.refresh()
    finish_sample(process_runner, task, records=[(42, 1000, 120), (100, 2000, 40)], total=1400)
    assert page._selected_identity() == (100, 2000)
    cell = page.table.visualItemRect(retained).center()
    qtbot.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=cell)
    qtbot.mouseDClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=cell)
    qtbot.waitUntil(lambda: page.controller.busy)
    finish_details(process_runner, pid=100, start=2000, uid=10042, cmdline="worker\0--selected\0")
    assert (page.controller.details.pid, page.controller.details.start_ticks) == (100, 2000)
    assert " ".join(page.controller.details.argv) == "worker --selected"
    assert "--selected" in page.detail_text.toPlainText()
    page.set_active(False)


def test_page_unknown_cpu_sorts_last_both_directions_and_repeated_errors_do_not_log_each_tick(process_runner, qtbot):
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    messages = []
    page.log_message.connect(messages.append)
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner, records=[(42, 1000, 80), (43, 2000, 40)])
    page.controller.set_auto_refresh(False)
    task = page.controller.refresh()
    sections = sample_sections(records=[(42, 1000, 120), (43, 2000, 40)], total=1400, denied=(43,))
    process_runner.finish(task, frame(sections, request_nonce(task)))
    for order in (Qt.SortOrder.DescendingOrder, Qt.SortOrder.AscendingOrder):
        page.table.sortItems(5, order)
        assert page.table.item(1, 0).text() == "43"
    for raw in ("first device response", "different device response"):
        failed = page.controller.refresh()
        process_runner.finish(failed, raw, "denied", 1)
    assert len([message for message in messages if message.startswith("ERROR")]) == 1
    changed = page.controller.refresh()
    process_runner.finish(changed, "device response", "transport disconnected", 1)
    assert len([message for message in messages if message.startswith("ERROR")]) == 2
    recovered = page.controller.refresh()
    finish_sample(process_runner, recovered)
    assert len([message for message in messages if "已恢复" in message]) == 1
    page.set_active(False)


def test_page_hidden_keeps_items_until_reactivation_and_does_not_attach_old_pid_details(process_runner, qtbot):
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner)
    page.table.setCurrentCell(0, 0)
    task = page.controller.load_details(42, 1000)
    page.set_active(False)
    retained = page.table.item(0, 0)
    page.set_device("device-b")
    finish_details(process_runner, task)
    assert page.table.item(0, 0) is retained
    assert page.controller.details is None
    page.set_active(True)
    assert page.table.rowCount() == 0
    finish_sample(process_runner, records=[(42, 2000, 80)])
    page.table.setCurrentCell(0, 0)
    assert "启动序号：17" not in page.detail_text.toPlainText()
    page.set_active(False)


# --- PR 2: lighter sampling, worker parsing, queueing, actions -------------

from functools import partial
import threading

from sysdroid.core import processes as processes_module
from sysdroid.core.processes import SAMPLE_INTERVALS, ProcessInfo, app_package

_REAL_EXECUTOR = ProcessController.__dict__["_thread_executor"]


def script_of(task):
    return shlex.split(task.args[1])[2]


def test_sample_script_reads_proc_in_three_bulk_passes_without_per_pid_loop():
    script = processes_module._sample_script("n1", "columns", {})
    assert script.count("cat /proc/[0-9]*/stat") == 2
    assert "grep -H -E" in script and "/proc/[0-9]*/status" in script
    assert "while IFS= read -r ps_line" not in script
    assert "set +f" in script  # the helpers disable globbing; the bulk reads need it


def test_bulk_records_ignore_new_pids_and_comm_newline_fragments_but_reject_duplicates():
    fragment = "77 (bad\n" + "name) S 1 0\n"
    result = sample(extra_stat=stat(999, start=5) + fragment).processes
    assert set(result) == {42} and result[42].start_ticks == 1000
    with pytest.raises(ValueError):
        sample(extra_stat=stat(42, start=1000))


def test_failed_discovery_never_locks_a_guessed_ps_mode_and_retries_with_backoff(process_runner, controller):
    now = [100.0]
    controller._monotonic = lambda: now[0]
    controller.set_active(True)
    first = process_runner.requests[-1]
    assert "ps-mode" in script_of(first)
    process_runner.finish(first, "", "timeout", None, status="timed_out")
    assert controller._ps_mode is None and "ps 能力探测失败" in controller.status
    controller._tick()
    assert len(process_runner.requests) == 1  # backoff: no retry storm every tick
    now[0] += 2.5
    controller._tick()
    retry = process_runner.requests[-1]
    assert len(process_runner.requests) == 2 and "ps-mode" in script_of(retry)
    process_runner.finish(retry, frame(sample_sections(), request_nonce(retry))[:-10])  # damaged frame
    now[0] += 2.5
    controller._tick()
    assert len(process_runner.requests) == 2  # second failure waits longer
    controller.set_auto_refresh(False)
    manual = controller.refresh()  # manual refresh bypasses the backoff
    assert "ps-mode" in script_of(manual)
    finish_sample(process_runner, manual, mode="all")
    assert controller._ps_mode == "all" and controller._discovery_failures == 0
    following = controller.refresh()
    assert "ps-mode" not in script_of(following) and "$(ps -A)" in script_of(following)


def test_fixed_ps_mode_that_keeps_failing_is_rediscovered(process_runner, controller):
    controller.set_active(True)
    finish_sample(process_runner)
    controller.set_auto_refresh(False)
    for attempt in range(3):
        task = controller.refresh()
        assert "ps-mode" not in script_of(task)
        nonce = request_nonce(task)
        process_runner.finish(task, frame(sample_sections(), nonce).replace(f"END {nonce} ps 0", f"END {nonce} ps 1"))
    assert controller._ps_mode is None
    assert "ps-mode" in script_of(controller.refresh())


def test_details_requested_while_sampling_are_queued_not_dropped(process_runner, controller, qtbot):
    controller.set_active(True)
    finish_sample(process_runner, records=[(42, 1000, 80), (43, 2000, 40)])
    controller.set_auto_refresh(False)
    sampling = controller.refresh()
    assert controller.load_details(42, 1000) is None
    assert controller.details_pending and "已排队" in controller.status
    assert controller.load_details(43, 2000) is None  # latest request wins
    finish_sample(process_runner, sampling, records=[(42, 1000, 80), (43, 2000, 40)])
    qtbot.waitUntil(lambda: len(process_runner.requests) == 3)
    detail = process_runner.requests[-1]
    assert "43" in detail.title and not controller.details_pending
    finish_details(process_runner, detail, pid=43, start=2000)
    assert controller.details.pid == 43
    assert process_runner.maximum_pending == 1


def test_queued_details_for_a_process_that_exited_are_cancelled_with_reason(process_runner, controller, qtbot):
    controller.set_active(True)
    finish_sample(process_runner, records=[(42, 1000, 80), (43, 2000, 40)])
    controller.set_auto_refresh(False)
    sampling = controller.refresh()
    controller.load_details(43, 2000)
    finish_sample(process_runner, sampling, records=[(42, 1000, 80)])
    qtbot.wait(20)
    assert len(process_runner.requests) == 2 and not controller.details_pending
    assert "43" in controller.error and "取消" in controller.status


def test_sampling_interval_is_configurable_and_validated(controller):
    controller.set_active(True)
    for value in SAMPLE_INTERVALS:
        controller.set_interval(value)
        assert controller.interval == value == controller._timer.interval()
    with pytest.raises(ValueError):
        controller.set_interval(1500)


def test_parsing_runs_in_a_worker_thread_and_holds_the_slot_until_applied(process_runner, controller, qtbot, monkeypatch):
    controller._executor = partial(_REAL_EXECUTOR, controller)
    threads = []
    original = processes_module.parse_process_sample

    def recording(*args, **kwargs):
        threads.append(threading.current_thread())
        return original(*args, **kwargs)
    monkeypatch.setattr(processes_module, "parse_process_sample", recording)
    controller.set_active(True)
    finish_sample(process_runner)
    assert controller.busy and controller.sample is None  # published only via the queued result
    qtbot.waitUntil(lambda: controller.sample is not None)
    assert not controller.busy and threads and threads[0] is not threading.main_thread()


def test_worker_result_for_a_switched_device_is_discarded(process_runner, controller, qtbot):
    controller._executor = partial(_REAL_EXECUTOR, controller)
    controller.set_active(True)
    old = process_runner.requests[-1]
    finish_sample(process_runner, old)
    controller.set_device("device-b")
    qtbot.waitUntil(lambda: len(process_runner.requests) == 2)
    assert controller.sample is None and process_runner.requests[-1].serial == "device-b"


def test_kill_reverifies_identity_on_device_and_refreshes_after(process_runner, controller, qtbot):
    results = []
    controller.action_finished.connect(lambda message, ok: results.append((message, ok)))
    controller.set_active(True)
    finish_sample(process_runner)
    with pytest.raises(ValueError):
        controller.kill_process(42, 999)  # identity no longer matches the sample
    task = controller.kill_process(42, 1000)
    script = script_of(task)
    assert "expected=1000" in script and "kill -s TERM $pid" in script and '"${20}"' in script
    process_runner.finish(task)
    assert results[-1][1] and "已完成" in results[-1][0]
    qtbot.waitUntil(lambda: len(process_runner.requests) == 3)  # follow-up sample
    finish_sample(process_runner)
    forced = controller.kill_process(42, 1000, force=True)
    assert "kill -s KILL" in script_of(forced)
    process_runner.finish(forced, "", "kill: 42: Operation not permitted", 1)
    assert not results[-1][1] and "强行停止应用" in results[-1][0]


def test_force_stop_and_app_package_detection(process_runner, controller):
    controller.set_active(True)
    task = controller.force_stop("com.example.app", 10)
    assert shlex.split(task.args[1]) == ["am", "force-stop", "--user", "10", "com.example.app"]
    with pytest.raises(ValueError):
        controller.force_stop("bad package;rm", 0)

    def info(uid, name, command):
        return ProcessInfo(1, uid, 1, name, "S", 1, 1, None, None, None, None, None, None, command,
                           None, None, None, None, None, {})
    assert app_package(info(10123, "ple.app:remote", "com.example.app:remote")) == "com.example.app"
    assert app_package(info(1010123, "x", "com.work.app")) == "com.work.app"
    assert app_package(info(1000, "system_server", "system_server")) is None
    assert app_package(info(None, "com.a.b", "com.a.b")) is None
    assert app_package(info(10001, "app_process", "/system/bin/app_process")) is None


def test_page_kill_requires_confirmation_and_interval_combo_drives_controller(process_runner, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner)
    page.table.setCurrentCell(0, 0)
    answers = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: answers[0])
    page.kill_selected()
    assert len(process_runner.requests) == 1
    answers[0] = QMessageBox.StandardButton.Yes
    page.kill_selected(force=True)
    assert "kill -s KILL" in script_of(process_runner.requests[-1])
    page.set_interval(5000)
    assert page.controller.interval == 5000 and page.interval() == 5000
    page.set_active(False)


def test_page_details_double_click_while_busy_is_queued(process_runner, qtbot):
    page = ProcessPage(process_runner)
    qtbot.addWidget(page)
    page.set_device("device-a")
    page.set_active(True)
    finish_sample(process_runner)
    page.controller.set_auto_refresh(False)
    sampling = page.controller.refresh()
    page.table.setCurrentCell(0, 0)
    page._load_details()
    assert page.controller.details_pending and "已排队" in page.status_label.text()
    finish_sample(process_runner, sampling)
    qtbot.waitUntil(lambda: len(process_runner.requests) == 3)
    assert "详情" in process_runner.requests[-1].title
    page.set_active(False)

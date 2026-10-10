from __future__ import annotations

import math
import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from sysdroid.core.backend import OUTPUT_LIMIT, TRUNCATED, Task, TaskRunner, significant_stderr


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    uid: int | None
    ppid: int | None
    name: str | None
    state: str | None
    start_ticks: int | None
    cpu_ticks: int | None
    cpu_percent: float | None
    rss_bytes: int | None
    vss_bytes: int | None
    threads: int | None
    started_at: float | None
    elapsed_seconds: float | None
    command_line: str | None
    swap_bytes: int | None
    priority: int | None
    nice: int | None
    processor: int | None
    cpu_seconds: float | None
    unavailable: dict[str, str]


@dataclass(frozen=True)
class ProcessSample:
    processes: dict[int, ProcessInfo]
    total_ticks: int | None
    idle_ticks: int | None
    boot_time: int | None
    uptime: float | None
    mem_total_bytes: int | None
    mem_available_bytes: int | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ProcessDetails:
    pid: int
    start_ticks: int
    argv: tuple[str, ...] | None
    pss_bytes: int | None
    swap_pss_bytes: int | None
    start_sequence: int | None
    start_reason: str | None
    hosting_type: str | None
    sections: dict[str, str]
    unavailable: dict[str, str]


@dataclass(frozen=True)
class _Section:
    text: str
    rc: int


@dataclass(frozen=True)
class _Stat:
    pid: int
    name: str
    state: str
    ppid: int
    cpu_ticks: int
    start_ticks: int
    vss_bytes: int
    rss_pages: int
    threads: int
    priority: int | None
    nice: int | None
    processor: int | None


def _frame(output: str, nonce: str) -> dict[str, _Section]:
    if not re.fullmatch(r"[A-Za-z0-9]+", nonce):
        raise ValueError("无效采样标识")
    if TRUNCATED in output or len(output) > OUTPUT_LIMIT:
        raise ValueError("输出已截断，不能发布不完整快照")
    opening, closing = f"FRAME {nonce}\n", f"DONE {nonce}\n"
    if not output.startswith(opening) or not output.endswith(closing):
        raise ValueError("采样框架不完整（FRAME / DONE 缺失或包含额外输出）")
    sections: dict[str, _Section] = {}
    position, end = len(opening), len(output) - len(closing)
    begin_pattern = re.compile(rf"BEGIN {re.escape(nonce)} ([a-z0-9:-]+)\n")
    finish_pattern = re.compile(rf"\nEND {re.escape(nonce)} ([a-z0-9:-]+) ([0-9]+)\n")
    nested_pattern = re.compile(rf"(?m)^(?:BEGIN|END|DONE|FRAME) {re.escape(nonce)}(?: |$)")
    while position < end:
        begin = begin_pattern.match(output, position, end)
        if begin is None:
            raise ValueError("采样段开头损坏")
        name = begin.group(1)
        if name in sections:
            raise ValueError(f"重复采样段：{name}")
        start = begin.end()
        finish = finish_pattern.search(output, start, end)
        if finish is None or finish.group(1) != name:
            raise ValueError(f"采样段缺少结束或返回码：{name}")
        text = output[start:finish.start()]
        # A marker inside a payload signals broken/nested framing, not data.
        if nested_pattern.search(text):
            raise ValueError(f"采样段包含嵌套框架：{name}")
        sections[name] = _Section(text, int(finish.group(2)))
        position = finish.end()
    if position != end:
        raise ValueError("采样框架尾部损坏")
    return sections


def parse_process_stat(output: str) -> _Stat:
    line = output.removesuffix("\n")
    opening, closing = line.find("("), line.rfind(")")
    if opening < 1 or closing < opening or "\n" in line:
        raise ValueError("stat 记录不完整")
    pid_text = line[:opening].strip()
    fields = line[closing + 1:].split()
    if not pid_text.isdecimal() or int(pid_text) <= 0 or len(fields) < 22:
        raise ValueError("stat PID / 字段不完整")
    try:
        ppid, user, system, threads, start, size, rss = (
            int(fields[index]) for index in (1, 11, 12, 17, 19, 20, 21))
    except ValueError as exc:
        raise ValueError("stat 数值字段无效") from exc
    if min(ppid, user, system, threads, start, size, rss) < 0 or len(fields[0]) != 1:
        raise ValueError("stat 数值或状态无效")
    return _Stat(int(pid_text), line[opening + 1:closing], fields[0], ppid,
                 user + system, start, size, rss, threads,
                 _signed_number(fields[15]), _signed_number(fields[16]),
                 _number(fields[36]) if len(fields) > 36 else None)


def _elapsed(text: str) -> float | None:
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text)
    if match is None:
        return None
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    if seconds >= 60 or minutes >= 60:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _ps_rows(output: str) -> dict[int, dict[str, str]]:
    lines = output.splitlines()
    if not lines:
        raise ValueError("ps 缺少表头")
    header = lines[0].split()
    if "PID" not in header or len(set(header)) != len(header):
        raise ValueError("ps 缺少有效 PID 表头")
    known = {"PID", "UID", "USER", "PPID", "S", "STAT", "STATE", "NAME", "COMM", "CMD", "ARGS", "CMDLINE", "COMMAND"}
    if len(header) < 2 or not (set(header) - {"PID"}) & known:
        raise ValueError("ps 表头未识别")
    tail_columns = {"ARGS", "CMD", "CMDLINE", "COMMAND", "NAME", "COMM"}
    for column in header[:-1]:
        if column in {"ARGS", "CMDLINE", "COMMAND"}:
            raise ValueError("ps 命令尾串不是末列，无法可靠解析")
    rows: dict[int, dict[str, str]] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(None, len(header) - 1) if header[-1] in tail_columns else line.split()
        if len(values) != len(header):
            raise ValueError(f"ps 半记录或诊断输出：{line}")
        row = dict(zip(header, values))
        pid_text = row["PID"]
        if not pid_text.isascii() or not pid_text.isdecimal() or int(pid_text) <= 0:
            raise ValueError(f"ps 无效 PID：{line}")
        pid = int(pid_text)
        if pid in rows:
            raise ValueError(f"ps 重复 PID：{pid}")
        rows[pid] = row
    return rows


def _number(text: str | None) -> int | None:
    return int(text) if text is not None and text.isascii() and text.isdecimal() else None


def _signed_number(text: str) -> int | None:
    return int(text) if re.fullmatch(r"[+-]?[0-9]+", text) else None


def _status(output: str, pid: int) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key in values:
            raise ValueError("status 记录不完整或重复")
        values[key] = value.strip()
    if _number(values.get("Pid")) != pid:
        raise ValueError("status PID 与目标不符")
    return values


_SAMPLE_SECTIONS = frozenset({
    "ps", "cpu", "uptime", "memory", "ps-help", "ps-mode", "clock-ticks", "page-size", "ps-units",
    "stats-before", "statuses", "stats-after",
})
_STAT_PREFIX = re.compile(r"([1-9][0-9]*) \(")
_STATUS_LINE = re.compile(r"/proc/([1-9][0-9]*)/status:(.*)")


def _bulk_stats(text: str) -> dict[int, str]:
    """Split one ``cat /proc/[0-9]*/stat`` pass into per-PID records."""
    records: dict[int, str] = {}
    for line in text.split("\n"):
        match = _STAT_PREFIX.match(line)
        if match is None:
            # Blank separator or the tail of a comm containing a newline; the
            # head of such a record then fails its own stat validation.
            continue
        pid = int(match.group(1))
        if pid in records:
            raise ValueError("批量 stat 记录包含重复 PID")
        records[pid] = line
    return records


def _bulk_status(text: str) -> dict[int, str]:
    """Group ``grep -H`` status lines (``/proc/PID/status:Key: value``) by PID."""
    records: dict[int, list[str]] = {}
    for line in text.split("\n"):
        match = _STATUS_LINE.fullmatch(line)
        if match is not None:
            records.setdefault(int(match.group(1)), []).append(match.group(2))
    return {pid: "\n".join(lines) for pid, lines in records.items()}


def _kb(text: str | None) -> int | None:
    match = re.fullmatch(r"(\d+)\s+kB", text or "")
    return int(match.group(1)) * 1024 if match else None


def _ps_scales(sections: dict[str, _Section]) -> dict[str, int]:
    scales: dict[str, int] = {}
    help_section = sections.get("ps-help")
    if help_section:
        # Help may describe several columns on the same line (Toybox RSS/VSZ).
        for line in help_section.text.splitlines():
            unit = re.search(r"\b(KiB|kB|KB|kilobytes|bytes)\b", line, re.IGNORECASE)
            if unit:
                scale = 1 if unit.group(1).lower() == "bytes" else 1024
                for column in ("RSS", "VSZ", "VSIZE"):
                    if re.search(rf"\b{column}\b", line):
                        scales[column] = scale
    unit_section = sections.get("ps-units")
    if unit_section and unit_section.rc == 0:
        for line in unit_section.text.splitlines():
            match = re.fullmatch(r"(RSS|VSZ|VSIZE)=(1|1024)", line)
            if match:
                scales[match.group(1)] = int(match.group(2))
    return scales


def parse_process_sample(output: str, *, nonce: str, clock_ticks: int | None,
                         page_size: int | None) -> ProcessSample:
    sections = _frame(output, nonce)
    if "ps" not in sections or sections["ps"].rc != 0:
        raise ValueError("关键 ps 清单缺失或执行失败")
    if not {"cpu", "uptime", "memory"} <= sections.keys():
        raise ValueError("采样框架缺少全局段")
    for name in sections:
        if name not in _SAMPLE_SECTIONS:
            raise ValueError(f"未识别采样段：{name}")
    rows = _ps_rows(sections["ps"].text)
    if "stats-before" not in sections or "stats-after" not in sections:
        raise ValueError("采样框架缺少 PID 复核段")
    # The bulk reads exit non-zero whenever one listed PID exits mid-read, so
    # their return codes are informational: availability is per record.
    stats_before = _bulk_stats(sections["stats-before"].text)
    stats_after = _bulk_stats(sections["stats-after"].text)
    statuses = _bulk_status(sections["statuses"].text) if "statuses" in sections else {}
    warnings: list[str] = []

    def optional(name: str) -> str | None:
        section = sections.get(name)
        if section is None or section.rc != 0:
            warnings.append(f"{name} 不可读取" + (f"（返回码 {section.rc}）" if section else "（缺段）"))
            return None
        return section.text

    total = idle = boot = None
    cpu = optional("cpu")
    if cpu is not None:
        cpu_lines = [line for line in cpu.splitlines() if line.startswith("cpu ")]
        boot_lines = [line for line in cpu.splitlines() if line.startswith("btime ")]
        if len(cpu_lines) == 1:
            counters = cpu_lines[0].split()[1:]
            if len(counters) >= 8 and all(_number(value) is not None for value in counters):
                ticks = [int(value) for value in counters[:8]]
                total, idle = sum(ticks), ticks[3] + ticks[4]
        if len(boot_lines) == 1 and len(boot_lines[0].split()) == 2:
            boot = _number(boot_lines[0].split()[1])
        if total is None:
            warnings.append("整机 CPU 计数缺失或未识别")
        if boot is None:
            warnings.append("设备启动 epoch（btime）不可用")
    uptime = None
    uptime_text = optional("uptime")
    if uptime_text is not None:
        parts = uptime_text.split()
        try:
            values = [float(part) for part in parts] if len(parts) == 2 else []
            if values and all(math.isfinite(value) and value >= 0 for value in values):
                uptime = values[0]
        except ValueError:
            pass
        if uptime is None:
            warnings.append("设备 uptime 未识别")
    memory = optional("memory")
    mem_total = mem_available = None
    if memory is not None:
        fields = {key: value.strip() for line in memory.splitlines()
                  for key, separator, value in [line.partition(":")] if separator}
        mem_total, mem_available = _kb(fields.get("MemTotal")), _kb(fields.get("MemAvailable"))
        if mem_total is None or mem_available is None:
            warnings.append("MemTotal / MemAvailable 未完整提供")
    hz = clock_ticks if clock_ticks is not None and clock_ticks > 0 else None
    pages = page_size if page_size is not None and page_size > 0 else None
    scales = _ps_scales(sections)
    processes: dict[int, ProcessInfo] = {}
    for pid, row in rows.items():
        unavailable: dict[str, str] = {}
        uid = _number(row.get("UID", row.get("USER")))
        ppid = _number(row.get("PPID"))
        name = row.get("NAME", row.get("COMM", row.get("CMD")))
        state = row.get("S", row.get("STAT", row.get("STATE")))
        command = row.get("ARGS", row.get("CMDLINE", row.get("COMMAND", row.get("CMD"))))
        rss = _number(row.get("RSS"))
        rss = rss * scales["RSS"] if rss is not None and "RSS" in scales else None
        vss_column = "VSZ" if "VSZ" in row else "VSIZE"
        vss = _number(row.get(vss_column))
        vss = vss * scales[vss_column] if vss is not None and vss_column in scales else None
        start = cpu_ticks = threads = started = elapsed = None
        swap = priority = nice = processor = cpu_seconds = None
        try:
            before_text, after_text = stats_before.get(pid), stats_after.get(pid)
            if before_text is None or after_text is None:
                raise ValueError("stat 访问被拒绝、进程退出或复核记录缺失")
            before, after = parse_process_stat(before_text), parse_process_stat(after_text)
            if before.pid != pid or after.pid != pid or before.start_ticks != after.start_ticks:
                raise ValueError("PID 已复用，丢弃混合 /proc 字段")
            start, cpu_ticks = after.start_ticks, after.cpu_ticks
            ppid, name, state, threads = after.ppid, after.name, after.state, after.threads
            vss = after.vss_bytes
            priority, nice, processor = after.priority, after.nice, after.processor
            status_text = statuses.get(pid)
            status_values: dict[str, str] = {}
            if status_text is not None:
                try:
                    status_values = _status(status_text, pid)
                except ValueError as exc:
                    unavailable["status"] = str(exc)
            else:
                unavailable["status"] = "status 不可读取"
            uid_parts = status_values.get("Uid", "").split()
            real_uid = _number(uid_parts[0]) if uid_parts else None
            if real_uid is not None:
                uid = real_uid
            status_rss = _kb(status_values.get("VmRSS"))
            status_vss = _kb(status_values.get("VmSize"))
            swap = _kb(status_values.get("VmSwap"))
            rss = status_rss if status_rss is not None else rss
            if rss is None and pages is not None:
                rss = after.rss_pages * pages
            if status_vss is not None:
                vss = status_vss
            status_threads = _number(status_values.get("Threads"))
            if status_threads is not None:
                threads = status_threads
            if hz is not None:
                cpu_seconds = after.cpu_ticks / hz
                started = boot + start / hz if boot is not None else None
                elapsed = uptime - start / hz if uptime is not None else None
                if elapsed is not None and elapsed < 0:
                    elapsed = None
                    unavailable["elapsed_seconds"] = "启动 ticks 大于设备 uptime"
        except ValueError as exc:
            for field in ("start_ticks", "cpu_ticks", "threads", "started_at", "swap_bytes",
                          "priority", "nice", "processor", "cpu_seconds"):
                unavailable[field] = str(exc)
        if hz is None:
            elapsed = _elapsed(row.get("ETIME", ""))
            unavailable["started_at"] = "设备 CLK_TCK 不可用；未假定 100Hz"
            unavailable["cpu_seconds"] = "设备 CLK_TCK 不可用；不能将 CPU ticks 换算为秒"
        fields = {"uid": uid, "ppid": ppid, "name": name, "state": state,
                  "rss_bytes": rss, "vss_bytes": vss, "threads": threads,
                  "started_at": started, "elapsed_seconds": elapsed, "command_line": command,
                  "swap_bytes": swap, "priority": priority, "nice": nice,
                  "processor": processor, "cpu_seconds": cpu_seconds}
        for field, value in fields.items():
            if value is None:
                unavailable.setdefault(field, "设备未提供、权限不足或单位未识别")
        unavailable["cpu_percent"] = "首帧 / 无可靠前一帧 CPU 基线"
        processes[pid] = ProcessInfo(pid, uid, ppid, name, state, start, cpu_ticks, None,
                                     rss, vss, threads, started, elapsed, command,
                                     swap, priority, nice, processor, cpu_seconds, unavailable)
    return ProcessSample(processes, total, idle, boot, uptime, mem_total, mem_available, tuple(warnings))


def aggregate_cpu_percent(previous: ProcessSample | None, current: ProcessSample) -> float | None:
    if previous is None or any(value is None for value in (
            previous.total_ticks, current.total_ticks, previous.idle_ticks, current.idle_ticks)):
        return None
    delta = current.total_ticks - previous.total_ticks
    idle = current.idle_ticks - previous.idle_ticks
    if delta <= 0 or idle < 0 or idle > delta:
        return None
    return 100.0 * (1.0 - idle / delta)


def apply_cpu_delta(previous: ProcessSample | None, current: ProcessSample) -> ProcessSample:
    processes: dict[int, ProcessInfo] = {}
    for pid, info in current.processes.items():
        old = previous.processes.get(pid) if previous else None
        reason = "首帧 / 新 PID / 无可靠前一帧 CPU 基线"
        percent = None
        if info.start_ticks is None or info.cpu_ticks is None:
            reason = info.unavailable.get("cpu_ticks", "无可靠进程启动 ticks")
        elif old is not None and old.start_ticks == info.start_ticks and old.cpu_ticks is not None:
            if previous.total_ticks is None or current.total_ticks is None:
                reason = "整机 CPU 计数不可读取"
            else:
                denominator = current.total_ticks - previous.total_ticks
                delta = info.cpu_ticks - old.cpu_ticks
                if denominator <= 0 or delta < 0:
                    reason = "CPU 计数回退或总计数差值非正"
                else:
                    percent = 100.0 * delta / denominator
        elif old is not None:
            reason = "PID 身份变化 / 无可靠启动 ticks"
        unavailable = dict(info.unavailable)
        if percent is None:
            unavailable["cpu_percent"] = reason
        else:
            unavailable.pop("cpu_percent", None)
        processes[pid] = replace(info, cpu_percent=percent, unavailable=unavailable)
    return replace(current, processes=processes)


def _android_uid(value: str) -> int | None:
    if value.isascii() and value.isdecimal():
        return int(value)
    match = re.fullmatch(r"u(\d+)([as])(\d+)", value)
    if match:
        user, kind, app = match.groups()
        return int(user) * 100000 + int(app) + (10000 if kind == "a" else 0)
    return None


def parse_activity_record(output: str, pid: int, uid: int | None) -> dict[str, str | int]:
    if uid is None:
        return {}
    lines = output.splitlines()
    matches: list[dict[str, str | int]] = []
    for index, line in enumerate(lines):
        # AM repeats ProcessRecord references in PID/UID/home indexes. Only a
        # record header owns the following indented fields; references are not
        # competing detail records and must not erase a reliable startSeq.
        record = re.match(r"\s*(?:\*[A-Z]+\*(?:\s+UID\s+\d+)?\s+)?ProcessRecord\{[^{}\s]+\s+(\d+):([^{}\s]+)/([^{}\s]+)\}", line)
        if record is None or int(record.group(1)) != pid:
            continue
        indent = len(line) - len(line.lstrip())
        body = [line]
        for next_line in lines[index + 1:]:
            if next_line.strip() and ("ProcessRecord{" in next_line or len(next_line) - len(next_line.lstrip()) <= indent):
                break
            body.append(next_line)
        text = "\n".join(body)
        record_uid = _android_uid(record.group(3))
        numeric_uids = [int(value) for value in re.findall(r"\buid\s*[=:]\s*(\d+)\b", text)]
        if record_uid is None and len(set(numeric_uids)) == 1:
            record_uid = numeric_uids[0]
        if record_uid != uid or any(value != uid for value in numeric_uids):
            continue
        result: dict[str, str | int] = {}
        sequence = re.search(r"\b(?:startSeq|mStartSeq)\s*[=:]\s*(\d+)\b", text)
        if sequence:
            result["start_sequence"] = int(sequence.group(1))
        for source, field in (("(?:startReason|mStartReason)", "start_reason"),
                              ("(?:hostingType|mHostingType)", "hosting_type")):
            value = re.search(rf"\b{source}\s*[=:]\s*([^\n,}}]+)", text)
            if value:
                result[field] = re.split(r"\s+\w+\s*[=:]", value.group(1), maxsplit=1)[0].strip()
        matches.append(result)
    return matches[0] if len(matches) == 1 else {}


def parse_meminfo(output: str) -> tuple[int | None, int | None]:
    unit = bool(re.search(r"\bin\s+Kilobytes\b|(?m:^[^\n]*(?:Memory Usage|Memory Info|Pss)[^\n]*\((?:kB|KiB)\))",
                          output, re.IGNORECASE))
    pss = swap = None
    for field, target in (("TOTAL PSS", "pss"), ("TOTAL SWAP PSS", "swap")):
        match = re.search(rf"(?m)\b{field}:\s*(\d+)(?:\s*(kB|KiB|bytes))?", output)
        if match and (match.group(2) or unit):
            value = int(match.group(1)) * (1 if match.group(2) == "bytes" else 1024)
            if target == "pss":
                pss = value
            else:
                swap = value
    if unit and (pss is None or swap is None):
        lines = output.splitlines()
        for index, line in enumerate(lines):
            header = line.split()
            if "Pss" not in header:
                continue
            for candidate in lines[index + 1:]:
                parts = candidate.split()
                if parts and parts[0] == "TOTAL" and len(parts) > len(header):
                    numbers = parts[1:]
                    if pss is None and _number(numbers[header.index("Pss")]) is not None:
                        pss = int(numbers[header.index("Pss")]) * 1024
                    if "SwapPss" in header and swap is None and _number(numbers[header.index("SwapPss")]) is not None:
                        swap = int(numbers[header.index("SwapPss")]) * 1024
                    break
            break
    return pss, swap


def parse_process_details(output: str, *, nonce: str, pid: int, start_ticks: int,
                          uid: int | None) -> ProcessDetails:
    sections = _frame(output, nonce)
    required = {"stat-before", "cmdline", "status", "meminfo", "activity", "stat-after"}
    if set(sections) != required:
        raise ValueError("进程详情框架缺段或包含未知段")
    if sections["stat-before"].rc or sections["stat-after"].rc:
        raise ValueError("进程身份不可确认：stat 访问被拒绝或进程已退出")
    before, after = parse_process_stat(sections["stat-before"].text), parse_process_stat(sections["stat-after"].text)
    if before.pid != pid or after.pid != pid or before.start_ticks != start_ticks or after.start_ticks != start_ticks:
        raise ValueError("PID 已退出或复用，详情不属于选中的进程身份")
    unavailable: dict[str, str] = {}
    if sections["status"].rc == 0:
        status_values = _status(sections["status"].text, pid)
        uid_parts = status_values.get("Uid", "").split()
        actual_uid = _number(uid_parts[0]) if uid_parts else None
        if uid is not None and actual_uid is not None and uid != actual_uid:
            raise ValueError("进程 UID 已变化，详情身份不可确认")
    else:
        unavailable["status"] = f"status 返回码 {sections['status'].rc}"
    argv = None
    if sections["cmdline"].rc == 0:
        raw = sections["cmdline"].text
        argv = tuple(raw.removesuffix("\0").split("\0")) if raw else ()
    else:
        unavailable["argv"] = f"cmdline 返回码 {sections['cmdline'].rc}（与空 cmdline 不同）"
    pss, swap = parse_meminfo(sections["meminfo"].text) if sections["meminfo"].rc == 0 else (None, None)
    activity = parse_activity_record(sections["activity"].text, pid, uid) if sections["activity"].rc == 0 else {}
    for field, value in {"pss_bytes": pss, "swap_pss_bytes": swap,
                         "start_sequence": activity.get("start_sequence"),
                         "start_reason": activity.get("start_reason"),
                         "hosting_type": activity.get("hosting_type")}.items():
        if value is None:
            unavailable[field] = "设备未提供、权限不足或未识别；不从名称推断"
    if sections["meminfo"].rc:
        unavailable["pss_bytes"] = unavailable["swap_pss_bytes"] = f"meminfo 返回码 {sections['meminfo'].rc}；原始输出已保留"
    if sections["activity"].rc:
        for field in ("start_sequence", "start_reason", "hosting_type"):
            unavailable[field] = f"activity 返回码 {sections['activity'].rc}；不推断启动来源"
    return ProcessDetails(pid, start_ticks, argv, pss, swap, activity.get("start_sequence"),
                          activity.get("start_reason"), activity.get("hosting_type"),
                          {name: section.text for name, section in sections.items()}, unavailable)


# Every END reports the read/command's saved return code, never printf's code.
_SHELL_HELPERS = r'''
set -f
# Android mksh has builtin print but external printf. Prefer raw builtin output
# so each PID section does not spawn a dozen formatter processes.
if command -v print >/dev/null 2>&1; then
    emit() { print -r -- "$1"; }
else
    emit() { printf '%s\n' "$1"; }
fi
begin() { emit "BEGIN $nonce $1"; }
end() { emit ''; emit "END $nonce $1 $2"; }
read_stat() {
    IFS= read -r stat_line < "$1"
    stat_rc=$?
    if [ "$stat_rc" -eq 0 ]; then emit "$stat_line"; fi
    return "$stat_rc"
}
read_status_lines() {
    while :; do
        status_line=
        IFS= read -r status_line
        read_rc=$?
        if [ "$read_rc" -ne 0 ]; then
            # read=1 with no partial record is normal EOF; other failures
            # retain the read return code, not the last printf/case status.
            if [ "$read_rc" -eq 1 ] && [ -z "$status_line" ]; then return 0; fi
            return "$read_rc"
        fi
        case "$status_line" in
            Pid:*|PPid:*|Uid:*|Name:*|State:*|VmRSS:*|VmSize:*|VmSwap:*|Threads:*) emit "$status_line" ;;
        esac
    done
}
read_status() {
    read_status_lines < "$1"
    status_rc=$?
    return "$status_rc"
}
stat_section() { begin "$1"; read_stat "$2"; file_rc=$?; end "$1" "$file_rc"; }
status_section() { begin "$1"; read_status "$2"; file_rc=$?; end "$1" "$file_rc"; }
command_section() { section=$1; shift; begin "$section"; "$@"; command_rc=$?; end "$section" "$command_rc"; }
'''


def _sample_script(nonce: str, mode: str | None, scales: dict[str, int]) -> str:
    script = f"nonce={shlex.quote(nonce)}\n" + _SHELL_HELPERS + 'emit "FRAME $nonce"\n'
    if mode is None:
        script += r'''
command_section ps-help ps --help
command_section clock-ticks getconf CLK_TCK
command_section page-size getconf PAGESIZE
ps_mode=columns
ps_output=$(ps -A -o PID,UID,PPID,S,NAME,RSS,VSZ,ETIME,ARGS)
ps_rc=$?
if [ "$ps_rc" -ne 0 ]; then
    ps_mode=all
    ps_output=$(ps -A)
    ps_rc=$?
    if [ "$ps_rc" -ne 0 ]; then
        ps_mode=plain
        ps_output=$(ps)
        ps_rc=$?
    fi
fi
begin ps-mode; emit "$ps_mode"; end ps-mode 0
'''
    else:
        command = {"columns": "ps -A -o PID,UID,PPID,S,NAME,RSS,VSZ,ETIME,ARGS", "all": "ps -A", "plain": "ps"}[mode]
        script += f"ps_output=$({command})\nps_rc=$?\n"
        units = "".join(f"{column}={scale}\n" for column, scale in scales.items())
        script += f"begin ps-units; emit {shlex.quote(units)}; end ps-units 0\n"
    script += r'''
begin ps; emit "$ps_output"; end ps "$ps_rc"
command_section cpu cat /proc/stat
command_section uptime cat /proc/uptime
command_section memory cat /proc/meminfo
if [ "$ps_rc" -eq 0 ]; then
    # Three bulk reads instead of three shell reads per PID: stat before and
    # after the status pass detects PID reuse without a per-process loop.
    set +f
    command_section stats-before cat /proc/[0-9]*/stat 2>/dev/null
    command_section statuses grep -H -E '^(Pid|Uid|VmRSS|VmSize|VmSwap|Threads):' /proc/[0-9]*/status 2>/dev/null
    command_section stats-after cat /proc/[0-9]*/stat 2>/dev/null
    set -f
fi
emit "DONE $nonce"
'''
    return script


def _details_script(nonce: str, pid: int) -> str:
    return f"nonce={shlex.quote(nonce)}\n" + _SHELL_HELPERS + f'''
emit "FRAME $nonce"
stat_section stat-before /proc/{pid}/stat
command_section cmdline cat /proc/{pid}/cmdline
status_section status /proc/{pid}/status
command_section meminfo dumpsys meminfo {pid}
command_section activity dumpsys activity processes
stat_section stat-after /proc/{pid}/stat
emit "DONE $nonce"
'''


@dataclass
class _Request:
    generation: int
    visibility: int
    operation: str
    nonce: str
    automatic: bool
    capabilities: bool = False
    pid: int | None = None
    start_ticks: int | None = None
    uid: int | None = None
    task_id: str = ""


@dataclass(frozen=True)
class _Outcome:
    """Result of parsing one finished request; computed off the UI thread."""
    error: str = ""
    capabilities: tuple[str, dict[str, int], int | None, int | None] | None = None
    capability_error: str = ""
    sample: ProcessSample | None = None
    parsed: ProcessSample | None = None
    total_cpu_percent: float | None = None
    details: ProcessDetails | None = None
    ps_failed: bool = False


def _discover(stdout: str, nonce: str) -> tuple[str, dict[str, int], int | None, int | None]:
    sections = _frame(stdout, nonce)
    mode = sections.get("ps-mode")
    if mode is None or mode.rc or mode.text.strip() not in {"columns", "all", "plain"}:
        raise ValueError("ps 能力选择结果缺失或无效")
    values = []
    for name in ("clock-ticks", "page-size"):
        section = sections.get(name)
        value = _number(section.text.strip()) if section and section.rc == 0 else None
        values.append(value if value is not None and value > 0 else None)
    return mode.text.strip(), _ps_scales(sections), values[0], values[1]


def _analyze(request: _Request, status: str, exit_code: int | None, stdout: str, stderr: str,
             clock_ticks: int | None, page_size: int | None,
             baseline: ProcessSample | None) -> _Outcome:
    """Pure parsing step. Runs in a worker thread and touches no controller state."""
    if status != "succeeded" or exit_code != 0:
        return _Outcome(error=f"任务状态：{status}；退出码：{exit_code}")
    if TRUNCATED in stderr:
        return _Outcome(error="stderr 已截断，采样结果不完整")
    capabilities = None
    capability_error = ""
    if request.capabilities:
        try:
            capabilities = _discover(stdout, request.nonce)
            clock_ticks, page_size = capabilities[2], capabilities[3]
        except ValueError as exc:
            capability_error = str(exc)
            return _Outcome(error=capability_error, capability_error=capability_error)
    try:
        if request.operation == "sample":
            parsed = parse_process_sample(stdout, nonce=request.nonce, clock_ticks=clock_ticks, page_size=page_size)
            return _Outcome(capabilities=capabilities, sample=apply_cpu_delta(baseline, parsed), parsed=parsed,
                            total_cpu_percent=aggregate_cpu_percent(baseline, parsed))
        details = parse_process_details(stdout, nonce=request.nonce, pid=request.pid,
                                        start_ticks=request.start_ticks, uid=request.uid)
        return _Outcome(capabilities=capabilities, details=details)
    except ValueError as exc:
        return _Outcome(error=str(exc), capabilities=capabilities,
                        ps_failed=str(exc).startswith("关键 ps 清单"))
    except Exception as exc:  # parser bugs must never wedge the request slot
        return _Outcome(error=f"解析异常：{type(exc).__name__}: {exc}", capabilities=capabilities)


Executor = Callable[[Callable[[], object], Callable[[object], None]], None]

# Allowed automatic sampling intervals in milliseconds.
SAMPLE_INTERVALS = (1000, 2000, 5000, 10000)
# Discovery retry backoff after failures (seconds), capped at the last value.
_DISCOVERY_BACKOFF = (2, 5, 15, 30, 60)
# A fixed ps mode that fails this many consecutive times is re-discovered.
_PS_FAILURE_LIMIT = 3
_KILL_SIGNALS = {"TERM": "TERM", "KILL": "KILL"}


def _kill_script(pid: int, start_ticks: int, signal: str) -> str:
    # Re-verify PID + start ticks on the device right before signalling, so a
    # PID reused since the last sample is never killed.
    return f'''set -f
pid={pid}
expected={start_ticks}
if ! IFS= read -r line 2>/dev/null < /proc/$pid/stat; then echo "进程 $pid 已退出" >&2; exit 3; fi
rest=${{line##*) }}
set -- $rest
if [ "${{20}}" != "$expected" ]; then echo "PID $pid 已被其他进程复用，未发送信号" >&2; exit 4; fi
kill -s {signal} $pid
'''


class ProcessController(QObject):
    changed = Signal()
    # (message, ok) for kill / force-stop actions.
    action_finished = Signal(str, bool)
    _parsed = Signal(object)

    def __init__(self, runner: TaskRunner, parent: QObject | None = None, *,
                 executor: Executor | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self.serial = ""
        self.device_state = ""
        self.status = ""
        self.error = ""
        self.last_task_id = ""
        self.sample: ProcessSample | None = None
        self.sampled_at: float | None = None
        self.total_cpu_percent: float | None = None
        self.details: ProcessDetails | None = None
        self.details_task_id = ""
        self.auto_refresh = True
        self.active = False
        self._generation = 0
        self._visibility = 0
        self._request: _Request | None = None
        self._submitting = False
        self._baseline: ProcessSample | None = None
        self._ps_mode: str | None = None
        self._scales: dict[str, int] = {}
        self._clock_ticks: int | None = None
        self._page_size: int | None = None
        self._discovery_failures = 0
        self._discovery_retry_at = 0.0
        self._ps_failures = 0
        self._first_pending = False
        self._pending_details: tuple[int, int, int, int] | None = None
        self._actions: dict[str, tuple[int, str]] = {}
        self._monotonic = time.monotonic
        self._executor = executor or self._thread_executor
        self._parsed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._tick)
        runner.task_finished.connect(self._task_finished)

    # -- worker plumbing -------------------------------------------------
    def _thread_executor(self, job: Callable[[], object], done: Callable[[object], None]) -> None:
        def run() -> None:
            result = job()
            try:
                self._parsed.emit((done, result))
            except RuntimeError:
                pass  # controller already destroyed; nothing to publish to
        threading.Thread(target=run, name="process-parse", daemon=True).start()

    def _deliver(self, payload: tuple[Callable[[object], None], object]) -> None:
        done, result = payload
        done(result)

    # -- state -----------------------------------------------------------
    @property
    def busy(self) -> bool:
        # Context changes never release a physical, non-interruptible ADB slot;
        # the slot is also held while the response is parsed in the worker.
        return self._submitting or self._request is not None

    @property
    def interval(self) -> int:
        return self._timer.interval()

    @property
    def details_pending(self) -> bool:
        return self._pending_details is not None

    def set_interval(self, milliseconds: int) -> None:
        if milliseconds not in SAMPLE_INTERVALS:
            raise ValueError("采样间隔无效")
        if milliseconds == self._timer.interval():
            return
        self._timer.setInterval(milliseconds)
        self._update_timer()
        self.changed.emit()

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) == (self.serial, self.device_state):
            return
        self._generation += 1
        self.serial, self.device_state = serial, state
        self.sample = self._baseline = None
        self.sampled_at = self.total_cpu_percent = None
        self.details = None
        self.details_task_id = self.last_task_id = ""
        self._ps_mode = None
        self._scales = {}
        self._clock_ticks = self._page_size = None
        self._discovery_failures = self._ps_failures = 0
        self._discovery_retry_at = 0.0
        self._pending_details = None
        self.error = ""
        self.status = "等待在线设备" if not self._online() else "等待首次采样"
        self._first_pending = self.active and self._online()
        self._update_timer()
        self.changed.emit()
        self._start_first()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self.active:
            return
        self.active = active
        self._visibility += 1
        self._baseline = None
        self.total_cpu_percent = None
        self.details = None
        self.details_task_id = ""
        self._pending_details = None
        self._first_pending = active and self._online()
        self.status = "等待首次采样" if active else "页面隐藏，采样已暂停"
        self._update_timer()
        self.changed.emit()
        self._start_first()

    def set_auto_refresh(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.auto_refresh:
            return
        self.auto_refresh = enabled
        self.status = "自动采样已开启" if enabled else "自动采样已暂停，保留最后快照"
        self._update_timer()
        self.changed.emit()
        if enabled:
            self._tick()

    def _online(self) -> bool:
        return bool(self.serial) and self.device_state == "device"

    def _update_timer(self) -> None:
        if self.active and self.auto_refresh and self._online():
            self._timer.start()
        else:
            self._timer.stop()

    def _discovery_waiting(self) -> bool:
        return self._ps_mode is None and self._monotonic() < self._discovery_retry_at

    def _start_first(self) -> None:
        if not self.active or not self._online() or self.busy:
            return
        if self._start_pending_details():
            return
        if self._first_pending and not self._discovery_waiting():
            self._first_pending = False
            self._start_sample(True)

    def _tick(self) -> None:
        if not (self.active and self._online()) or self.busy:
            return
        if self._start_pending_details():
            return
        if self.auto_refresh and not self._discovery_waiting():
            self._first_pending = False
            self._start_sample(True)

    def _require_online(self) -> None:
        if not self._online():
            raise ValueError("请先选择已连接的在线设备")

    def _require_ready(self) -> None:
        self._require_online()
        if self.busy:
            raise ValueError("进程采样 / 详情请求尚未返回，请等待")

    def refresh(self) -> Task | None:
        self._require_ready()
        self._first_pending = False
        return self._start_sample(False)

    def _start_sample(self, automatic: bool) -> Task | None:
        nonce = uuid.uuid4().hex
        request = _Request(self._generation, self._visibility, "sample", nonce, automatic,
                           capabilities=self._ps_mode is None)
        return self._submit(request, _sample_script(nonce, self._ps_mode, self._scales), 5)

    def _identity(self, pid: int, start_ticks: int | None) -> ProcessInfo:
        if type(pid) is not int or pid <= 0 or type(start_ticks) is not int or start_ticks < 0:
            raise ValueError("身份不可确认：没有可靠 PID / 启动 ticks，不能加载可能误配的详情")
        info = self.sample.processes.get(pid) if self.sample else None
        if info is None or info.start_ticks != start_ticks:
            raise ValueError("选中的进程身份已变化，请重新采样 / 选择")
        return info

    def load_details(self, pid: int, start_ticks: int | None) -> Task | None:
        """Read details now, or queue them behind the request in flight.

        Returns the task when submitted immediately and ``None`` when queued;
        only the most recent queued request is kept.
        """
        self._require_online()
        info = self._identity(pid, start_ticks)
        if self.busy:
            self._pending_details = (pid, start_ticks, self._generation, self._visibility)
            self.status = f"已排队：当前请求返回后读取进程 {pid} 详情"
            self.changed.emit()
            return None
        self._pending_details = None
        return self._submit_details(pid, start_ticks, info.uid)

    def _submit_details(self, pid: int, start_ticks: int, uid: int | None) -> Task | None:
        nonce = uuid.uuid4().hex
        request = _Request(self._generation, self._visibility, "details", nonce, False,
                           pid=pid, start_ticks=start_ticks, uid=uid)
        return self._submit(request, _details_script(nonce, pid), 15)

    def _start_pending_details(self) -> bool:
        pending, self._pending_details = self._pending_details, None
        if pending is None:
            return False
        pid, start_ticks, generation, visibility = pending
        if (generation, visibility) != (self._generation, self._visibility):
            return False
        try:
            info = self._identity(pid, start_ticks)
        except ValueError as exc:
            self.status = "排队的详情请求已取消"
            self.error = f"进程 {pid}：{exc}"
            self.changed.emit()
            return False
        try:
            self._submit_details(pid, start_ticks, info.uid)
        except ValueError:
            pass  # _submit already published the failure
        return True

    def _submit(self, request: _Request, script: str, timeout: int) -> Task | None:
        self._request = request
        self._submitting = True
        serial = self.serial
        try:
            task = self._runner.start_adb(
                "采样 Android 进程" if request.operation == "sample" else f"读取 Android 进程详情：{request.pid}",
                ["shell", shlex.join(["sh", "-c", script])], serial=serial,
                timeout=timeout, transient=request.automatic)
        except Exception as exc:
            self._request = None
            self._submitting = False
            if request.generation == self._generation and request.visibility == self._visibility:
                self.status = "无法启动进程请求"
                self.error = f"{type(exc).__name__}: {exc}"
                self.changed.emit()
            if self._first_pending:
                QTimer.singleShot(0, self._start_first)
            if not request.automatic:
                raise ValueError(f"无法启动进程请求：{exc}") from exc
            return None
        self._submitting = False
        request.task_id = task.id
        if request.generation == self._generation and request.visibility == self._visibility:
            self.last_task_id = task.id
            self.status = "正在采样…" if request.operation == "sample" else "正在读取详情…"
            # Preserve the previous error until a successful result: no per-tick
            # error/recovery oscillation or log floods.
        self.changed.emit()
        if task.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            self._task_finished(task)
        return task if request.generation == self._generation and request.visibility == self._visibility else None

    def _task_finished(self, task: Task) -> None:
        if task.id in self._actions:
            self._action_finished(task)
            return
        request = self._request
        if request is None or task.id != request.task_id or request.task_id == "":
            return
        # Keep the slot occupied until the worker result is applied, and mark
        # the request so a duplicate finished signal is ignored.
        request.task_id = ""
        baseline = self._baseline if request.generation == self._generation and request.visibility == self._visibility else None
        status, exit_code, stdout, stderr = task.status, task.exit_code, task.stdout, task.stderr
        clock_ticks, page_size = self._clock_ticks, self._page_size
        self._executor(lambda: _analyze(request, status, exit_code, stdout, stderr, clock_ticks, page_size, baseline),
                       lambda outcome: self._apply(request, task, outcome))

    def _apply(self, request: _Request, task: Task, outcome: _Outcome) -> None:
        if self._request is not request:
            return
        self._request = None
        same_device = request.generation == self._generation
        current = same_device and request.visibility == self._visibility
        discovery_failed = same_device and request.capabilities and outcome.capabilities is None
        if same_device and request.capabilities:
            if outcome.capabilities is not None:
                self._ps_mode, self._scales, self._clock_ticks, self._page_size = outcome.capabilities
                self._discovery_failures = 0
                self._discovery_retry_at = 0.0
            else:
                # Never lock a guessed ps mode after a timed-out or damaged
                # discovery: retry discovery later with a capped backoff.
                delay = _DISCOVERY_BACKOFF[min(self._discovery_failures, len(_DISCOVERY_BACKOFF) - 1)]
                self._discovery_failures += 1
                self._discovery_retry_at = self._monotonic() + delay
        if same_device and request.operation == "sample" and not request.capabilities:
            if outcome.ps_failed:
                self._ps_failures += 1
                if self._ps_failures >= _PS_FAILURE_LIMIT:
                    # The fixed ps mode keeps failing: re-run capability discovery.
                    self._ps_mode = None
                    self._ps_failures = 0
            elif not outcome.error:
                self._ps_failures = 0
        if current:
            if not outcome.error:
                if request.operation == "sample":
                    sample = outcome.sample
                    self.total_cpu_percent = outcome.total_cpu_percent
                    self.sample = sample
                    self._baseline = outcome.parsed
                    self.sampled_at = time.time()
                    if self.details and (self.details.pid not in sample.processes or
                                         sample.processes[self.details.pid].start_ticks != self.details.start_ticks):
                        self.details = None
                        self.details_task_id = ""
                    self.status = f"已采样 {len(sample.processes)} 个进程" + (" · 自动采样已暂停" if not self.auto_refresh else "")
                else:
                    self.details = outcome.details
                    self.details_task_id = task.id
                    self.status = f"已读取进程 {request.pid} 详情（未改变 CPU 基线）"
                self.error = ""
            else:
                self.status = "进程采样失败，保留最后快照" if request.operation == "sample" else "进程详情不可用"
                if discovery_failed:
                    wait = max(0, round(self._discovery_retry_at - self._monotonic()))
                    self.status = f"ps 能力探测失败，约 {wait} 秒后自动重试（手动刷新可立即重试）"
                self.error = f"{outcome.error}\nstderr：\n{task.stderr}\nstdout：\n{task.stdout}"
                if request.operation == "details":
                    self.details = None
                    self.details_task_id = ""
        self.changed.emit()
        if self._pending_details is not None or self._first_pending:
            QTimer.singleShot(0, self._start_first)

    # -- actions ---------------------------------------------------------
    def kill_process(self, pid: int, start_ticks: int | None, *, force: bool = False) -> Task:
        """Send SIGTERM (or SIGKILL) after re-verifying the process identity on device."""
        self._require_online()
        self._identity(pid, start_ticks)
        signal = _KILL_SIGNALS["KILL" if force else "TERM"]
        return self._start_action(f"结束进程 {pid}（SIG{signal}）",
                                  ["shell", shlex.join(["sh", "-c", _kill_script(pid, start_ticks, signal)])], 10)

    def force_stop(self, package: str, user_id: int) -> Task:
        from sysdroid.core.apks import validate_package
        self._require_online()
        validate_package(package)
        if type(user_id) is not int or user_id < 0:
            raise ValueError("用户 ID 无效")
        return self._start_action(f"强行停止应用 {package}（用户 {user_id}）",
                                  ["shell", shlex.join(["am", "force-stop", "--user", str(user_id), package])], 15)

    def _start_action(self, title: str, args: list[str], timeout: int) -> Task:
        try:
            task = self._runner.start_adb(title, args, serial=self.serial, timeout=timeout)
        except Exception as exc:
            raise ValueError(f"无法启动操作：{exc}") from exc
        self._actions[task.id] = (self._generation, title)
        if task.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            self._action_finished(task)
        return task

    def _action_finished(self, task: Task) -> None:
        generation, title = self._actions.pop(task.id)
        if generation != self._generation:
            return
        diagnostic = significant_stderr(task.stderr).strip()
        ok = task.status == "succeeded" and task.exit_code == 0 and not re.search(
            r"(?im)^\s*(?:Error\b|.*Exception\b|.*Permission denied|.*Operation not permitted)", task.stdout + "\n" + diagnostic)
        if ok:
            message = f"{title}：已完成"
        else:
            detail = (diagnostic or task.stdout.strip() or f"状态 {task.status} / 退出码 {task.exit_code}")
            if re.search(r"Operation not permitted|Permission denied", detail):
                detail += "\n（shell 用户只能结束自身进程；应用进程请使用「强行停止应用」）"
            message = f"{title}：失败\n{detail}"
        self.action_finished.emit(message, ok)
        # Show the effect promptly without overlapping the sampling slot.
        self._first_pending = True
        self._start_first()


def app_package(info: ProcessInfo) -> str | None:
    """Package name of an app process (UID >= 10000), or None when unknown."""
    from sysdroid.core.apks import _PACKAGE
    if info.uid is None or info.uid % 100000 < 10000:
        return None
    for candidate in (info.command_line, info.name):
        token = (candidate or "").split(" ", 1)[0].split(":", 1)[0]
        if "." in token and _PACKAGE.fullmatch(token):
            return token
    return None

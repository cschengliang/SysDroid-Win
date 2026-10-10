from __future__ import annotations

import os
import re
import shlex
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterator

from PySide6.QtCore import QObject, Signal
from sysdroid.core.backend import Task, TaskRunner
from sysdroid.core.processes import _frame as parse_frame

_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_DIAGNOSTIC = re.compile(
    r"^[ \t]*(?:(?:cmd|pm|am):[ \t]*)?(?:Error:|(?:[\w$]+\.)*[\w$]*Exception\b|Failure\s*\[|"
    r"Unknown command\b|Unknown option\b|Permission denied\b|Permission Denial\b|"
    r"inaccessible or not found\b|Can't find service\b|not supported\b)", re.I | re.M)


def validate_package(package: str) -> None:
    if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
        raise ValueError("无效 Android 包名")


@dataclass(frozen=True)
class PackageSummary:
    package: str
    uid: int | None = None
    version_code: int | None = None
    system: bool | None = None
    enabled: bool | None = None
    installer: str | None = None
    apk_path: str | None = None


@dataclass(frozen=True)
class PackageDetails:
    package: str
    user_id: int
    version_name: str | None = None
    version_code: int | None = None
    app_id: int | None = None
    uid: int | None = None
    flags: tuple[str, ...] | None = None
    min_sdk: int | None = None
    target_sdk: int | None = None
    primary_abi: str | None = None
    secondary_abi: str | None = None
    code_path: str | None = None
    data_dir: str | None = None
    installer: str | None = None
    first_install_time: str | None = None
    last_update_time: str | None = None
    installed: bool | None = None
    enabled: int | None = None
    stopped: bool | None = None
    hidden: bool | None = None
    suspended: bool | None = None
    paths: tuple[str, ...] = ()
    permissions: str = ""
    components: str = ""
    raw: str = ""

    def _flag(self, name: str) -> bool | None:
        return name in self.flags if self.flags is not None else None

    @property
    def system(self) -> bool | None:
        return self._flag("SYSTEM")

    @property
    def updated_system(self) -> bool | None:
        return self._flag("UPDATED_SYSTEM_APP")

    @property
    def debuggable(self) -> bool | None:
        return self._flag("DEBUGGABLE")

    @property
    def persistent(self) -> bool | None:
        return self._flag("PERSISTENT")


def parse_package_list(output: str) -> dict[str, PackageSummary]:
    result = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        if not line.startswith("package:"):
            raise ValueError(f"不完整或诊断包记录：{line}")
        body = line[8:]
        # The path may contain spaces or '='; metadata begins at labelled fields.
        parts = re.split(r"\s+(?=(?:installer|uid|versionCode)=|(?:uid|versionCode):)", body)
        identity = parts.pop(0)
        path = None
        if "=" in identity:
            path, identity = identity.rsplit("=", 1)
            if not path.startswith("/") or not path:
                raise ValueError("APK 路径无效")
        validate_package(identity)
        if identity in result:
            raise ValueError("重复包记录")
        fields = {}
        for part in parts:
            match = re.fullmatch(r"(installer|uid|versionCode)[=:](.*)", part)
            if not match or match[1] in fields:
                raise ValueError(f"包字段无效：{part}")
            fields[match[1]] = match[2]
        for key in ("uid", "versionCode"):
            if key in fields and not fields[key].isdecimal():
                raise ValueError(f"{key} 不是数字")
        result[identity] = PackageSummary(identity, int(fields["uid"]) if "uid" in fields else None,
            int(fields["versionCode"]) if "versionCode" in fields else None,
            installer=fields.get("installer") if fields.get("installer") != "null" else None, apk_path=path)
    return result


def parse_package_paths(output: str) -> tuple[str, ...]:
    paths = []
    for line in output.splitlines():
        if not line:
            continue
        if not line.startswith("package:/") or "\0" in line or _DIAGNOSTIC.search(line):
            raise ValueError(f"无效 APK 路径记录：{line}")
        path = line[8:]
        if path in paths:
            raise ValueError("重复 APK 路径")
        paths.append(path)
    if not paths:
        raise ValueError("设备没有提供 APK 路径")
    return tuple(paths)


def parse_package_dump(output: str, package: str, user_id: int) -> PackageDetails:
    validate_package(package)
    if _DIAGNOSTIC.search(output):
        raise ValueError("包详情包含设备诊断")
    lines = output.splitlines()
    sections = [i for i, line in enumerate(lines) if line.strip() == "Packages:"]
    if len(sections) != 1:
        raise ValueError("未找到唯一活动 Packages 区段")
    section_start = sections[0]
    section_indent = len(lines[section_start]) - len(lines[section_start].lstrip())
    section_end = section_start + 1
    while section_end < len(lines) and (not lines[section_end].strip() or
            len(lines[section_end]) - len(lines[section_end].lstrip()) > section_indent):
        section_end += 1
    matches = [(i, len(lines[i]) - len(lines[i].lstrip())) for i in range(section_start + 1, section_end)
               if re.fullmatch(r"\s*Package \[" + re.escape(package) + r"\] \([^)]*\):\s*", lines[i])]
    if len(matches) != 1:
        raise ValueError("未找到唯一目标 Package 区段")
    index, indent = matches[0]
    end = index + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    block = "\n".join(lines[index + 1:end])
    # Remove every per-user subsection before collecting package-wide values.
    common = re.split(r"(?m)^\s*User \d+:", block)[0]
    def field(key: str) -> str | None:
        if key in {"versionName", "codePath", "dataDir", "installerPackageName", "primaryCpuAbi", "secondaryCpuAbi"}:
            match = re.search(r"(?m)^\s*" + re.escape(key) + r"=([^\n]*)$", common)
        else:
            match = re.search(r"(?:^|\s)" + re.escape(key) + r"=([^\s]+)", common)
        value = match[1] if match else None
        return None if value == "null" else value
    def number(key: str) -> int | None:
        value = field(key)
        return int(value) if value and value.isdecimal() else None
    users = list(re.finditer(r"(?m)^\s*User (\d+):([^\n]*)", block))
    selected = [m for m in users if int(m[1]) == user_id]
    if len(selected) > 1:
        raise ValueError("重复用户区段")
    user = selected[0][2] if selected else ""
    def boolean(key: str) -> bool | None:
        m = re.search(r"\b" + key + r"=(true|false)\b", user)
        return m[1] == "true" if m else None
    enabled = re.search(r"\benabled=(\d+)\b", user)
    flags = re.search(r"\b(?:pkgFlags|flags)=\[([^]]*)\]", common)
    def timestamp(key: str) -> str | None:
        m = re.search(r"(?m)^\s*" + key + r"=(.*)$", common)
        return m[1] if m else None
    # Retain labelled permission/component sections, including target-user grants.
    sections = []
    section_user = None
    for i, line in enumerate(lines[index + 1:end]):
        header = re.match(r"\s*User (\d+):", line)
        if header:
            section_user = int(header[1])
        if re.search(r"(?:permissions|Permissions|Activities|Services|Receivers|Providers|Activity|Service|Receiver|Provider).*:", line):
            depth = len(line) - len(line.lstrip())
            source = lines[index + 1:end]
            j = i + 1
            while j < len(source) and (not source[j].strip() or len(source[j]) - len(source[j].lstrip()) > depth):
                j += 1
            # Exclude grants for other users.
            if section_user is None or section_user == user_id:
                sections.append("\n".join(source[i:j]))
    # Resolver component tables precede Packages; preserve only exact target tokens.
    components = "\n".join(line for line in lines if re.search(r"(?<![\w.])" + re.escape(package) + r"/", line))
    return PackageDetails(package, user_id, field("versionName"), number("versionCode"), number("appId") if number("appId") is not None else number("userId"), number("uid"),
        tuple(flags[1].split()) if flags else None, number("minSdk"), number("targetSdk"),
        field("primaryCpuAbi"), field("secondaryCpuAbi"), field("codePath"), field("dataDir"),
        field("installerPackageName"), timestamp("firstInstallTime"), timestamp("lastUpdateTime"),
        boolean("installed"), int(enabled[1]) if enabled else None, boolean("stopped"), boolean("hidden"),
        boolean("suspended"), permissions="\n\n".join(sections), components=components, raw=output)


_LIST_SECTIONS = ("members", "system", "disabled", "enriched")
_USER_UNSUPPORTED = re.compile(
    r"(?:unknown|unsupported|not supported|unrecognized|invalid).*--user|--user.*(?:unknown|unsupported|not supported|unrecognized|invalid)", re.I)
_PERCENT = re.compile(r"\[\s*(\d{1,3})%\]")


def list_script(nonce: str, user_id: int, options: list[str]) -> str:
    """One shell round trip for every ``pm list`` query of a refresh."""
    user = str(user_id)
    commands = [
        ("members", ["pm", "list", "packages", "--user", user]),
        ("system", ["pm", "list", "packages", "--user", user, "-s"]),
        ("disabled", ["pm", "list", "packages", "--user", user, "-d"]),
    ]
    if options:
        commands.append(("enriched", ["pm", "list", "packages", "--user", user, *options]))
    lines = [f"nonce={shlex.quote(nonce)}", r"""printf 'FRAME %s\n' "$nonce" """.strip()]
    for name, tokens in commands:
        lines.append(rf"""printf 'BEGIN %s %s\n' "$nonce" {name}; {shlex.join(tokens)}; rc=$?; """
                     rf"""printf '\nEND %s %s %s\n' "$nonce" {name} "$rc" """.strip())
    lines.append(r"""printf 'DONE %s\n' "$nonce" """.strip())
    return "\n".join(lines) + "\n"


def parse_list_batch(output: str, nonce: str) -> dict[str, str]:
    sections = parse_frame(output, nonce)
    if not {"members", "system", "disabled"} <= sections.keys() or not set(sections) <= set(_LIST_SECTIONS):
        raise ValueError("包列表批量输出缺少或包含未识别的段")
    for name, section in sections.items():
        if section.rc != 0 or _DIAGNOSTIC.search(section.text):
            raise ValueError(f"pm list（{name}）失败：返回码 {section.rc}\n{section.text}")
    return {name: section.text for name, section in sections.items()}


def install_progress(text: str) -> int | None:
    """Last ``[ NN%]`` percentage printed by adb install, if any."""
    matches = _PERCENT.findall(text)
    return min(100, int(matches[-1])) if matches else None


class PackageController(QObject):
    changed = Signal()
    # Live adb install output: (latest line, percent or -1 when unknown).
    install_progress = Signal(str, int)

    def __init__(self, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self.serial = ""
        self.device_state = ""
        self.user_id: int | None = None
        self.packages: dict[str, PackageSummary] = {}
        self.details: dict[str, PackageDetails] = {}
        self.status = self.error = self.task_id = self.last_task_id = ""
        self.export_mapping: list[tuple[str, str]] = []
        self.unsupported_actions: set[str] = set()
        self._generation = 0
        self._flow: Iterator | None = None
        self._help: str | None = None
        self._submitting = False
        self._install_output = ""
        runner.task_finished.connect(self._finished)
        task_output = getattr(runner, "task_output", None)
        if task_output is not None:
            task_output.connect(self._output)

    @property
    def busy(self) -> bool:
        return self._flow is not None or self._submitting

    def _reset(self) -> None:
        self._generation += 1
        if self.busy and self.status == "导出 APK":
            self.error = "设备或用户已变化，导出未完成；已传输文件保留。"
        else:
            self.error = ""
        self._flow = None
        self.task_id = self.last_task_id = self.status = ""
        self.packages = {}
        self.details = {}
        self.changed.emit()

    def set_device(self, serial: str, state: str = "device") -> None:
        if (serial, state) != (self.serial, self.device_state):
            self.serial, self.device_state = serial, state
            self._help = None
            self.unsupported_actions.clear()
            self._reset()

    def set_user(self, user_id: int) -> None:
        if not isinstance(user_id, int) or user_id < 0:
            raise ValueError("用户 ID 无效")
        if user_id != self.user_id:
            self.user_id = user_id
            self._reset()

    def _ready(self) -> None:
        if not self.serial or self.device_state != "device" or self.user_id is None:
            raise ValueError("请选择在线设备和已发现用户")
        if self.busy:
            raise ValueError("包操作正在进行")

    def _shell(self, tokens: list[str], timeout: int = 15):
        task = yield (["shell", shlex.join(tokens)], timeout)
        diagnostic = task.stdout + "\n" + task.stderr
        if _USER_UNSUPPORTED.search(diagnostic):
            self.unsupported_actions.add(tokens[2] if tokens[0] == "cmd" else tokens[1])
        if task.status != "succeeded" or task.exit_code != 0 or _DIAGNOSTIC.search(task.stdout + "\n" + task.stderr):
            raise ValueError(f"设备请求失败：{task.status} / {task.exit_code}\n{task.stdout}\n{task.stderr}")
        return task.stdout

    def _begin(self, flow: Iterator, title: str) -> Task | None:
        self._ready()
        self._flow = flow
        self.status = title
        self.error = ""
        self.export_mapping = []
        return self._advance()

    def _advance(self, task: Task | None = None) -> Task | None:
        generation = self._generation
        try:
            args, timeout = next(self._flow) if task is None else self._flow.send(task)
            self._submitting = True
            try:
                submitted = self._runner.start_adb(self.status, args, serial=self.serial, timeout=timeout)
            finally:
                self._submitting = False
            if generation != self._generation:
                return None
            self.task_id = self.last_task_id = submitted.id
            self.changed.emit()
            return submitted
        except StopIteration:
            self._flow = None
            self.task_id = ""
            self.changed.emit()
        except Exception as exc:
            if generation == self._generation:
                self.error = str(exc)
                self.status = "操作失败" if "状态未确认" not in self.error else "命令已返回，状态未确认"
                self._flow = None
                self.task_id = ""
                self.changed.emit()
        return None

    def _finished(self, task: Task) -> None:
        if self._flow is not None and task.id == self.task_id:
            self._advance(task)

    def _output(self, task_id: str, stream: str, text: str) -> None:
        if task_id != self.task_id or self.status != "安装 APK":
            return
        self._install_output = (self._install_output + text)[-4096:]
        lines = [line.strip() for line in re.split(r"[\r\n]+", self._install_output) if line.strip()]
        percent = install_progress(self._install_output)
        self.install_progress.emit(lines[-1] if lines else "", -1 if percent is None else percent)

    def _lists(self):
        if self._help is None:
            # Cached per device: later refreshes are a single round trip.
            self._help = yield from self._shell(["pm", "help"])
        options = [flag for flag in ("-f", "-i", "-U", "--show-versioncode")
                   if re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", self._help)]
        nonce = uuid.uuid4().hex
        task = yield (["shell", shlex.join(["sh", "-c", list_script(nonce, self.user_id, options)])], 30)
        if _USER_UNSUPPORTED.search(task.stdout + "\n" + task.stderr):
            self.unsupported_actions.add("list")
        if task.status != "succeeded" or task.exit_code != 0 or _DIAGNOSTIC.search(task.stderr):
            raise ValueError(f"设备请求失败：{task.status} / {task.exit_code}\n{task.stdout}\n{task.stderr}")
        texts = parse_list_batch(task.stdout, nonce)
        members = parse_package_list(texts["members"])
        systems = parse_package_list(texts["system"])
        disabled = parse_package_list(texts["disabled"])
        if options:
            if "enriched" not in texts:
                raise ValueError("包列表批量输出缺少详细字段段")
            enriched = parse_package_list(texts["enriched"])
            if set(enriched) != set(members):
                raise ValueError("包成员在查询期间变化；保留旧快照")
            members = enriched
        if not set(systems) <= set(members) or not set(disabled) <= set(members):
            raise ValueError("包状态列表与成员不一致")
        return {name: replace(value, system=name in systems, enabled=name not in disabled) for name, value in members.items()}

    def refresh(self) -> Task | None:
        def flow():
            snapshot = yield from self._lists()
            self.packages = snapshot
            self.status = f"已加载 {len(snapshot)} 个包"
        return self._begin(flow(), "读取包列表")

    def _details(self, package: str):
        dump = yield from self._shell(["dumpsys", "package", package])
        detail = parse_package_dump(dump, package, self.user_id)
        paths = parse_package_paths((yield from self._shell(["pm", "path", "--user", str(self.user_id), package])))
        return replace(detail, paths=paths)

    def load_details(self, package: str) -> Task | None:
        validate_package(package)
        def flow():
            self.details[package] = yield from self._details(package)
            self.status = "包详情已读取"
        return self._begin(flow(), "读取包详情")

    def install(self, paths: list[Path], *, replace: bool = False, allow_test: bool = False) -> Task | None:
        if not paths or any(not Path(p).is_file() for p in paths):
            raise ValueError("请选择存在的普通 APK 文件")
        files = [str(Path(p).resolve()) for p in paths]
        def flow():
            args = ["install" if len(files) == 1 else "install-multiple", "--user", str(self.user_id)]
            # Recent package managers replace by default even without -r.
            # Ordinary installation must explicitly refuse replacement.
            args.append("-r" if replace else "-R")
            if allow_test:
                args.append("-t")
            self._install_output = ""
            task = yield (args + files, 300)
            if _USER_UNSUPPORTED.search(task.stdout + task.stderr):
                self.unsupported_actions.add("install")
            if task.status != "succeeded" or task.exit_code != 0 or _DIAGNOSTIC.search(task.stdout + task.stderr) or not re.search(r"(?m)^Success\s*$", task.stdout):
                raise ValueError(f"安装失败（没有卸载或重试）：{task.status} / {task.exit_code}\n{task.stdout}\n{task.stderr}")
            self.details.clear()
            try:
                self.packages = yield from self._lists()
            except ValueError as exc:
                raise ValueError(f"安装命令成功，状态未确认：{exc}") from exc
            self.status = "安装命令成功；已刷新所选用户"
        return self._begin(flow(), "安装 APK")

    def _mutation(self, package: str, operation: str, enabled: bool | None = None) -> Task | None:
        validate_package(package)
        def flow():
            u = str(self.user_id)
            if operation == "force-stop":
                yield from self._shell(["am", "force-stop", "--user", u, package])
                self.status = "强行停止请求已完成"
                return
            command = "uninstall" if operation == "uninstall" else ("enable" if enabled else "disable-user")
            output = yield from self._shell(["pm", command, "--user", u, package])
            if operation == "uninstall" and not re.search(r"(?m)^Success\s*$", output):
                raise ValueError(f"卸载没有成功记录：{output}")
            try:
                snapshot = yield from self._lists()
                matches = package not in snapshot if operation == "uninstall" else package in snapshot and snapshot[package].enabled == enabled
                if not matches:
                    raise ValueError("业务状态读回不匹配")
            except ValueError as exc:
                raise ValueError(f"命令已返回，状态未确认：{exc}") from exc
            self.packages = snapshot
            self.details.pop(package, None)
            self.status = "卸载成功，已确认不存在" if operation == "uninstall" else "启用/禁用成功，已确认状态"
        return self._begin(flow(), f"{operation}：{package}")

    def uninstall(self, package: str) -> Task | None:
        return self._mutation(package, "uninstall")

    def set_enabled(self, package: str, enabled: bool) -> Task | None:
        return self._mutation(package, "set-enabled", enabled)

    def force_stop(self, package: str) -> Task | None:
        return self._mutation(package, "force-stop")

    def launch(self, package: str) -> Task | None:
        validate_package(package)
        def flow():
            output = yield from self._shell(["cmd", "package", "resolve-activity", "--brief", "--user", str(self.user_id), "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p", package])
            components = [line.strip() for line in output.splitlines() if re.fullmatch(re.escape(package) + r"/[\w.$]+", line.strip())]
            if len(set(components)) != 1 or len(components) != 1:
                raise ValueError("未找到可启动入口")
            output = yield from self._shell(["am", "start", "--user", str(self.user_id), "-W", "-n", components[0]])
            if not re.search(r"(?m)^Status:\s*ok\s*$", output):
                raise ValueError(f"启动未确认：{output}")
            self.status = "启动请求完成\n" + output
        return self._begin(flow(), "启动应用")

    def export(self, package: str, directory: Path) -> Task | None:
        validate_package(package)
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError("导出目标不是目录")
        def flow():
            before = yield from self._details(package)
            if before.version_code is None:
                raise ValueError("设备没有提供版本码，不能确认导出身份")
            child = directory / f"{package}-{self.user_id}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(5)}"
            child.mkdir(exist_ok=False)
            used = set()
            failures = []
            for index, remote in enumerate(before.paths):
                name = Path(remote).name
                if index == 0:
                    name = "base.apk"
                stem = Path(name).stem
                number = 1
                while name.casefold() in used:
                    number += 1
                    name = f"{stem}-{number}.apk"
                used.add(name.casefold())
                final = child / name
                partial = child / (name + ".part")
                self.export_mapping.append((remote, str(final)))
                task = yield (["pull", remote, str(partial)], 120)
                if task.status in {"cancelled", "timed_out"}:
                    raise ValueError(f"导出已{'停止' if task.status == 'cancelled' else '超时'}；未继续拉取其他 APK。\n"
                                     f"{remote} → {partial}\n{task.stdout}\n{task.stderr}")
                if task.status != "succeeded" or task.exit_code != 0 or _DIAGNOSTIC.search(task.stdout + task.stderr) or not partial.is_file():
                    failures.append(f"{remote} → {partial}\n{task.stdout}\n{task.stderr}")
                    continue
                # Windows rename refuses overwrites and also works on FAT/exFAT.
                # On POSIX, linking reserves the name without an overwrite race.
                try:
                    if os.name == "nt":
                        partial.rename(final)
                    else:
                        final.hardlink_to(partial)
                        partial.unlink()
                except OSError as exc:
                    failures.append(f"{remote} → {partial}: {exc}")
            if failures:
                raise ValueError("导出部分失败；成功文件与明确 .part 保留：\n" + "\n".join(failures))
            after = yield from self._details(package)
            if before.paths != after.paths or before.version_code != after.version_code or before.version_name != after.version_name or before.last_update_time != after.last_update_time:
                raise ValueError("包在导出期间更新；文件保留，但整体导出未确认")
            self.status = "导出完成\n" + "\n".join(f"{a} → {b}" for a, b in self.export_mapping)
        return self._begin(flow(), "导出 APK")

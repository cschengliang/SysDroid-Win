from __future__ import annotations

import json
import re
import shlex
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from sysdroid.core.backend import DATA_DIR


CATEGORIES = ("设备", "Shell", "应用", "系统", "文件")
EXECUTION_TYPES = {"quick": "短时一次性", "long": "长时一次性", "continuous": "持续运行", "wait": "条件等待"}
VARIABLE = re.compile(r"\{([^{}\s]+)\}")


@dataclass(frozen=True)
class Command:
    id: str
    name: str
    category: str
    template: str
    description: str = ""
    tags: str = ""
    scope: str = "device"
    timeout: int = 10
    permission: str = "user"
    favorite: bool = False
    execution_type: str = "quick"
    show_in_library: bool = True


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    steps: list[str]


@dataclass(frozen=True)
class PreparedCommand:
    command_id: str
    title: str
    args: tuple[str, ...]
    serial: str
    timeout: int
    permission: str


class StorageError(ValueError):
    pass


def split_template(text: str) -> list[str]:
    """Group single/double quotes; backslashes are always literal Windows path data.

    A doubled quote inside a quoted group represents that quote. Parameter values
    are substituted *after* this parser and are never parsed as template syntax.
    """
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\0":
            raise ValueError("命令不能包含 NUL 字符")
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    current.append(char)
                    index += 1
                else:
                    quote = ""
            else:
                current.append(char)
        elif char in "\"'":
            quote = char
            started = True
        elif char.isspace():
            if started:
                tokens.append("".join(current))
                current.clear()
                started = False
        else:
            current.append(char)
            started = True
        index += 1
    if quote:
        raise ValueError("命令模板的引号未闭合")
    if started:
        tokens.append("".join(current))
    return tokens


def variable_names(template: str) -> list[str]:
    return list(dict.fromkeys(VARIABLE.findall(template)))


def infer_execution_type(template: str) -> str:
    """Conservatively classify old templates during version-1 migration only.

    Recognize simple static programs, not arbitrary remote shell scripts. A label
    describes expected execution behavior; it does not prove script semantics.
    """
    if not isinstance(template, str) or "\n" in template or "\r" in template:
        return "long"
    try:
        tokens = split_template(template)
    except ValueError:
        return "long"
    if len(tokens) < 2 or tokens[0].lower() not in {"adb", "adb.exe"}:
        return "long"
    subcommand = tokens[1]
    if variable_names(subcommand):
        return "long"
    if subcommand.startswith("wait-for-"):
        return "wait"
    if subcommand in {"devices", "version", "help", "get-state", "get-serialno", "get-devpath", "server-status", "start-server", "kill-server"}:
        return "quick"
    arguments = tokens[2:]
    if subcommand in {"shell", "exec-out"}:
        if not arguments or variable_names(arguments[0]):
            return "long"
        # Shell syntax, expansions and multi-word script tokens are deliberately
        # not parsed. Ordinary parameter placeholders in data arguments are OK.
        for token in arguments:
            literal = VARIABLE.sub("", token)
            if re.search(r"[\s$`;|&<>(){}]", literal):
                return "long"
        program = arguments[0].rsplit("/", 1)[-1]
        arguments = arguments[1:]
    elif subcommand == "logcat":
        program = subcommand
    else:
        return "long"
    if program == "logcat":
        if any(option in {"-d", "-t"} or re.fullmatch(r"-t\d+", option) for option in arguments):
            return "quick"
        return "long" if any(variable_names(option) for option in arguments) else "continuous"
    if program in {"ping", "ping6", "top"}:
        flags = {"-n"} if program == "top" else {"-c", "-w"}
        for index, option in enumerate(arguments):
            if option in flags and index + 1 < len(arguments) and re.fullmatch(r"[1-9]\d*", arguments[index + 1]):
                return "quick"
            if any(re.fullmatch(re.escape(flag) + r"[1-9]\d*", option) for flag in flags):
                return "quick"
        if any(variable_names(option) or option in flags or any(option.startswith(flag) for flag in flags) for option in arguments):
            return "long"
        return "continuous"
    if program in {"getprop", "id", "whoami", "uname", "uptime", "date", "pwd", "ls", "cat", "df", "printenv", "echo", "screencap"}:
        return "quick"
    if program in {"pm", "cmd"}:
        if program == "cmd":
            if not arguments or arguments[0] != "package":
                return "long"
            arguments = arguments[1:]
        if arguments and arguments[0] == "path":
            return "quick"
        if len(arguments) >= 2 and arguments[0] == "list" and arguments[1] in {"packages", "features", "libraries", "users", "permissions", "permission-groups", "instrumentation"}:
            return "quick"
    if program == "wm" and arguments in (["size"], ["density"]):
        return "quick"
    return "long"


def is_remote_shell(command: Command) -> bool:
    tokens = split_template(command.template)
    return len(tokens) == 3 and tokens[0].lower() in {"adb", "adb.exe"} and tokens[1:] == ["shell", "{command}"]


def validate_command(command: Command) -> None:
    for field in ("id", "name", "category", "template", "description", "tags", "scope", "permission", "execution_type"):
        if not isinstance(getattr(command, field), str):
            raise ValueError(f"命令字段 {field} 必须是文本")
    if not command.id or not command.name.strip() or not command.category.strip():
        raise ValueError("命令 ID、名称和分类不能为空")
    if len(command.name) > 100:
        raise ValueError("命令名称不能超过 100 字符")
    if command.scope not in {"device", "host"} or command.permission not in {"user", "root"}:
        raise ValueError("无效的模式或权限")
    if command.execution_type not in EXECUTION_TYPES:
        raise ValueError("无效的执行类型")
    if type(command.timeout) is not int or not 1 <= command.timeout <= 3600:
        raise ValueError("超时必须为 1–3600 秒")
    if type(command.favorite) is not bool:
        raise ValueError("收藏字段必须为布尔值")
    if type(command.show_in_library) is not bool:
        raise ValueError("命令库显示字段必须为布尔值")
    tokens = split_template(command.template)
    if len(tokens) < 2 or tokens[0].lower() not in {"adb", "adb.exe"}:
        raise ValueError("仅支持以 adb 或 adb.exe 开头的 ADB 模板；不运行本地 Shell")
    if tokens[1].startswith("-"):
        raise ValueError("模板不能含 ADB 全局选项（包括 -s / -d / -e / -t）；设备由全局选择器指定")
    if variable_names(tokens[1]):
        raise ValueError("ADB 子命令不能是参数")
    if command.scope == "host" and tokens[1] in {"shell", "exec-out"}:
        raise ValueError("Shell / exec-out 必须使用设备模式，避免连接未指定的设备")
    if tokens[1] in {"shell", "exec-out"}:
        if len(tokens) < 3:
            raise ValueError("不支持交互 Shell，请填写要执行的设备命令")
        if variable_names(tokens[2]) and not is_remote_shell(command):
            raise ValueError("设备端程序名不能是参数；完整远程 Shell 请使用 adb shell {command}")
        # These programs interpret a data argument as source code themselves.
        interpreters = {"sh", "bash", "mksh", "zsh", "ash", "dash", "su", "eval"}
        programs = [token.rsplit("/", 1)[-1] for token in tokens[2:]]
        if variable_names(command.template) and not is_remote_shell(command) and any(token in interpreters for token in programs):
            raise ValueError("Shell 解释器参数不能使用普通变量；需明确使用 adb shell {command} 远程脚本模式")


def prepare_command(command: Command, parameters: Mapping[str, str], serial: str = "") -> PreparedCommand:
    validate_command(command)
    if command.scope == "device" and not serial:
        raise ValueError("请先选择设备")
    for name in variable_names(command.template):
        if name not in parameters or not isinstance(parameters[name], str) or not parameters[name]:
            raise ValueError(f"请填写参数 {name}")
        if "\0" in parameters[name]:
            raise ValueError(f"参数 {name} 不能包含 NUL 字符")
    tokens = split_template(command.template)[1:]
    values = [VARIABLE.sub(lambda match: parameters[match[1]], token) for token in tokens]
    if is_remote_shell(command):
        # Deliberately one script string, not local Shell syntax.
        args = ["shell", parameters["command"]]
    elif tokens[0] in {"shell", "exec-out"}:
        # ADB joins its arguments for the Android shell. Quote *each complete
        # token*, including constant template tokens, to preserve data boundaries.
        args = [tokens[0], " ".join(shlex.quote(value) for value in values[1:])]
    else:
        args = values
    return PreparedCommand(command.id, command.name, tuple(args), serial if command.scope == "device" else "", command.timeout, command.permission)


def seed_commands() -> list[Command]:
    return [
        Command("devices", "查看设备列表", "设备", "adb devices -l", "查看 ADB Server 发现的设备及连接信息。", "设备,连接", "host", favorite=True, execution_type="quick"),
        Command("shell", "执行 Shell 命令", "Shell", "adb shell {command}", "在当前设备上执行明确授权的远程 Shell 脚本。", "调试", execution_type="long"),
        Command("packages", "列出第三方应用", "应用", "adb shell pm list packages -3", "列出用户安装的应用包名。", "包名,应用", execution_type="quick"),
        Command("props", "读取系统属性", "系统", "adb shell getprop {key}", "读取指定的 Android 系统属性。", "属性,调试", execution_type="quick"),
        Command("screenshot", "截取屏幕", "文件", "adb shell screencap -p {path}", "将截图保存到设备上的指定路径。", "截图", execution_type="quick"),
        Command("preset-version", "Android 版本", "系统", "adb shell getprop ro.build.version.release", "读取系统版本号。", "版本", execution_type="quick", show_in_library=False),
        Command("preset-size", "屏幕分辨率", "设备", "adb shell wm size", "查看物理和覆盖的屏幕尺寸。", "屏幕", execution_type="quick", show_in_library=False),
        Command("preset-storage", "存储空间", "文件", "adb shell df -h /data", "检查用户数据分区的可用空间。", "存储", execution_type="quick", show_in_library=False),
        Command("preset-logcat", "最近 100 条日志", "系统", "adb logcat -d -t 100", "输出日志快照后退出，不持续监听。", "日志", execution_type="quick", show_in_library=False),
    ]


EXPORT_FORMAT = "sysdroid-commands"


def parse_payload(payload: object) -> tuple[list[Command], list[Workflow], int]:
    """Parse a stored or exported command library, migrating older versions."""
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] not in {1, 2, 3}:
        raise ValueError("命令库格式或版本无效")
    if not isinstance(payload.get("commands"), list) or not isinstance(payload.get("workflows"), list):
        raise ValueError("commands / workflows 必须是列表")
    records = []
    for record in payload["commands"]:
        if not isinstance(record, dict):
            raise ValueError("命令记录必须是对象")
        if "execution_type" not in record:
            if payload["version"] != 1:
                raise ValueError("命令记录缺少 execution_type")
            record = {**record, "execution_type": infer_execution_type(record.get("template", ""))}
        if "show_in_library" not in record:
            if payload["version"] == 3:
                raise ValueError("命令记录缺少 show_in_library")
            record = {**record, "show_in_library": True}
        records.append(record)
    if any(not isinstance(record, dict) for record in payload["workflows"]):
        raise ValueError("工作流记录必须是对象")
    commands = [Command(**record) for record in records]
    workflows = [Workflow(**record) for record in payload["workflows"]]
    return commands, workflows, payload["version"]


class CommandStore:
    """Atomic command/workflow transactions; corrupt storage remains read-only.

    Recovery is explicit and moves the original file to a unique backup first.
    No failed write changes the in-memory model.
    """
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DATA_DIR / "commands.json"
        self.commands: dict[str, Command] = {}
        self.workflows: dict[str, Workflow] = {}
        self.error = ""
        try:
            if self.path.exists():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                commands, workflows, version = parse_payload(payload)
                self._validate(commands, workflows)
                command_map = {command.id: command for command in commands}
                workflow_map = {workflow.id: workflow for workflow in workflows}
                if version != 3:
                    templates = {command.template for command in commands}
                    for command in seed_commands():
                        if not command.show_in_library and command.id not in command_map and command.template not in templates:
                            command_map[command.id] = command
                            templates.add(command.template)
                    self._commit(command_map, workflow_map)
                else:
                    self.commands = command_map
                    self.workflows = workflow_map
            else:
                self.commands = {command.id: command for command in seed_commands()}
        except (OSError, ValueError, TypeError) as exc:
            self.commands.clear()
            self.workflows.clear()
            self.error = f"无法读取 {self.path}：{exc}。原文件未修改；命令库只读，请修复文件或使用明确恢复。"

    @staticmethod
    def _validate(commands: list[Command], workflows: list[Workflow]) -> None:
        ids = {command.id for command in commands}
        if len(ids) != len(commands):
            raise ValueError("命令 ID 重复")
        for command in commands:
            validate_command(command)
        if len({workflow.id for workflow in workflows}) != len(workflows):
            raise ValueError("工作流 ID 重复")
        for workflow in workflows:
            if not isinstance(workflow.id, str) or not workflow.id or not isinstance(workflow.name, str) or not workflow.name.strip():
                raise ValueError("工作流 ID / 名称无效")
            if not isinstance(workflow.steps, list) or any(not isinstance(step, str) or step not in ids for step in workflow.steps):
                raise ValueError("工作流步骤引用无效的命令")

    def _commit(self, commands: dict[str, Command], workflows: dict[str, Workflow]) -> None:
        if self.error:
            raise StorageError(self.error)
        self._validate(list(commands.values()), list(workflows.values()))
        payload = {"version": 3, "commands": [asdict(command) for command in commands.values()],
                   "workflows": [asdict(workflow) for workflow in workflows.values()]}
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            cleanup_error = ""
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup:
                cleanup_error = f"；临时文件 {temporary} 清理失败：{cleanup}"
            raise StorageError(f"无法保存命令库 {self.path}：{exc}{cleanup_error}") from exc
        self.commands = commands
        self.workflows = workflows

    def save_command(self, command: Command) -> None:
        self._commit({**self.commands, command.id: command}, dict(self.workflows))

    def delete_command(self, command_id: str) -> None:
        commands = {key: value for key, value in self.commands.items() if key != command_id}
        workflows = {key: replace(value, steps=[step for step in value.steps if step != command_id]) for key, value in self.workflows.items()}
        self._commit(commands, workflows)

    def save_workflow(self, workflow: Workflow) -> None:
        self._commit(dict(self.commands), {**self.workflows, workflow.id: workflow})

    def delete_workflow(self, workflow_id: str) -> None:
        self._commit(dict(self.commands), {key: value for key, value in self.workflows.items() if key != workflow_id})

    def export_payload(self, command_ids: list[str] | None = None) -> dict[str, object]:
        """Portable JSON for all (or selected) commands plus workflows fully covered by them."""
        if self.error:
            raise StorageError(self.error)
        selected = [self.commands[key] for key in (command_ids if command_ids is not None else self.commands) if key in self.commands]
        ids = {command.id for command in selected}
        workflows = [workflow for workflow in self.workflows.values() if set(workflow.steps) <= ids]
        return {"format": EXPORT_FORMAT, "version": 3, "exported_at": datetime.now().isoformat(timespec="seconds"),
                "commands": [asdict(command) for command in selected],
                "workflows": [asdict(workflow) for workflow in workflows]}

    def import_payload(self, payload: object, *, overwrite: bool = False) -> dict[str, int]:
        """Merge an exported library in one atomic save.

        Existing IDs are kept unless ``overwrite``; identical records count as
        unchanged. Nothing is written if any record is invalid.
        """
        if isinstance(payload, dict) and payload.get("format") not in (None, EXPORT_FORMAT):
            raise ValueError("不是 SysDroid 命令库导出文件")
        commands, workflows, _version = parse_payload(payload)
        self._validate(commands, [])
        stats = {"added": 0, "updated": 0, "skipped": 0, "workflows": 0}
        command_map = dict(self.commands)
        for command in commands:
            existing = command_map.get(command.id)
            if existing is None:
                stats["added"] += 1
            elif existing == command:
                continue
            elif not overwrite:
                stats["skipped"] += 1
                continue
            else:
                stats["updated"] += 1
            command_map[command.id] = command
        workflow_map = dict(self.workflows)
        for workflow in workflows:
            if workflow.id in workflow_map and not overwrite:
                continue
            if workflow_map.get(workflow.id) != workflow:
                workflow_map[workflow.id] = workflow
                stats["workflows"] += 1
        self._commit(command_map, workflow_map)
        return stats

    def recover_defaults(self) -> Path | None:
        backup = None
        if self.path.exists():
            backup = self.path.with_name(self.path.name + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            try:
                self.path.rename(backup)
            except OSError as exc:
                raise StorageError(f"无法备份原文件：{exc}") from exc
        old_error = self.error
        self.error = ""
        try:
            self._commit({command.id: command for command in seed_commands()}, {})
        except (StorageError, ValueError):
            self.error = old_error or "恢复失败；原数据已保留在备份中"
            if backup is not None and not self.path.exists():
                backup.rename(self.path)
            raise
        return backup

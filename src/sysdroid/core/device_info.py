"""Device status batching and wireless-debugging helpers (pure, no Qt)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# One adb shell round trip. The first three lines keep the historic layout
# (uid, ro.debuggable, Android release); every later block is introduced by a
# marker so a missing tool (no `ip`, no `getenforce`) only empties its block.
STATUS_SCRIPT = "; ".join((
    "id -u",
    "getprop ro.debuggable",
    "getprop ro.build.version.release",
    "echo @@abi", "getprop ro.product.cpu.abi", "getprop ro.product.cpu.abilist",
    "echo @@selinux", "getenforce 2>/dev/null",
    "echo @@fingerprint", "getprop ro.build.fingerprint",
    "echo @@size", "wm size 2>/dev/null",
    "echo @@battery", "dumpsys battery 2>/dev/null",
    "echo @@ip", "ip -o -4 addr show 2>/dev/null",
    "echo @@mounts", "cat /proc/mounts",
))

BATTERY_STATUS = {"1": "未知", "2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}


@dataclass
class DeviceStatus:
    uid: str = ""
    debuggable: str = ""
    release: str = ""
    abi: str = ""
    abilist: str = ""
    selinux: str = ""
    fingerprint: str = ""
    resolution: str = ""
    battery: str = ""
    ip: str = ""
    mounts: list[str] = field(default_factory=list)

    @property
    def root(self) -> bool:
        return self.uid == "0"

    @property
    def system_writable(self) -> bool:
        return any(len(parts := line.split()) >= 4 and parts[1] in {"/", "/system", "/vendor"}
                   and "rw" in parts[3].split(",") for line in self.mounts)


def _sections(stdout: str) -> tuple[list[str], dict[str, list[str]]]:
    head: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] = head
    for line in stdout.replace("\r\n", "\n").split("\n"):
        marker = re.fullmatch(r"@@([a-z]+)\s*", line)
        if marker:
            current = sections.setdefault(marker[1], [])
        else:
            current.append(line)
    return head, sections


def format_resolution(lines: list[str]) -> str:
    physical = override = ""
    for line in lines:
        match = re.search(r"(Physical|Override) size:\s*(\d+x\d+)", line)
        if match:
            if match[1] == "Physical":
                physical = match[2]
            else:
                override = match[2]
    if physical and override and override != physical:
        return f"{override}（物理 {physical}）"
    return override or physical


def format_battery(lines: list[str]) -> str:
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition(":")
        if sep:
            values.setdefault(key.strip(), value.strip())
    level = values.get("level", "")
    if not level.isdigit():
        return ""
    scale = values.get("scale", "100")
    percent = round(int(level) * 100 / int(scale)) if scale.isdigit() and int(scale) > 0 else int(level)
    parts = [f"{percent}%"]
    status = BATTERY_STATUS.get(values.get("status", ""))
    if status:
        parts.append(status)
    sources = [name for key, name in (("AC powered", "AC"), ("USB powered", "USB"), ("Wireless powered", "无线")) if values.get(key) == "true"]
    if sources:
        parts.append("供电 " + "/".join(sources))
    temperature = values.get("temperature", "")
    if re.fullmatch(r"-?\d+", temperature):
        parts.append(f"{int(temperature) / 10:.1f}°C")
    return " · ".join(parts)


def format_ip(lines: list[str]) -> str:
    addresses = []
    for line in lines:
        match = re.search(r"^\d+:\s*(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line.strip())
        if match and match[1] != "lo" and not match[2].startswith("127."):
            addresses.append(f"{match[2]}（{match[1]}）")
    return "、".join(addresses)


def parse_device_status(stdout: str) -> DeviceStatus:
    head, sections = _sections(stdout)
    if len(head) < 3:
        raise ValueError("输出不完整，请查看任务输出")

    def first(name: str, index: int = 0) -> str:
        values = [line.strip() for line in sections.get(name, []) if line.strip()]
        return values[index] if len(values) > index else ""

    mounts = sections["mounts"] if "mounts" in sections else head[3:]
    return DeviceStatus(
        uid=head[0].strip(), debuggable=head[1].strip(), release=head[2].strip(),
        abi=first("abi"), abilist=first("abi", 1), selinux=first("selinux"),
        fingerprint=first("fingerprint"), resolution=format_resolution(sections.get("size", [])),
        battery=format_battery(sections.get("battery", [])), ip=format_ip(sections.get("ip", [])),
        mounts=[line for line in mounts if line.strip()],
    )


@dataclass(frozen=True)
class MdnsService:
    name: str
    kind: str
    address: str

    @property
    def pairing(self) -> bool:
        return "pairing" in self.kind

    @property
    def label(self) -> str:
        role = "配对" if self.pairing else "连接"
        return f"{role} · {self.address} · {self.name}"


def parse_mdns_services(stdout: str) -> list[MdnsService]:
    """Parse ``adb mdns services``; ignores the header and unrelated lines."""
    services: list[MdnsService] = []
    for line in stdout.splitlines():
        match = re.match(r"^(\S+)\s+(_adb[\w-]*\._tcp)\.?\s+(\S+:\d+)\s*$", line.strip())
        if match:
            service = MdnsService(match[1], match[2], match[3])
            if service not in services:
                services.append(service)
    return services


ADDRESS = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):\d{1,5}")


def validate_pairing(address: str, code: str) -> tuple[str, str]:
    address, code = address.strip(), code.strip()
    if not ADDRESS.fullmatch(address) or not 0 < int(address.rsplit(":", 1)[1]) < 65536:
        raise ValueError("请输入“无线调试 → 使用配对码配对设备”中显示的 IP:端口")
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("配对码应为 6 位数字")
    return address, code

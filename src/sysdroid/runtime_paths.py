from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import sys

_DLL_HANDLES: list[object] = []
_DLL_CONFIGURED = False


# src/sysdroid/runtime_paths.py -> repository root (which holds lib/ and tool/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def application_dir() -> Path:
    """Folder that contains tool/: the exe's folder when frozen, else the repository root."""
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT


def _override(name: str, value: str) -> Path:
    selected = Path(value).expanduser()
    found = str(selected) if selected.is_file() else shutil.which(value)
    if not found or not Path(found).is_file():
        raise ValueError(f"{name} 指定的 ADB 不存在：{value}")
    return Path(found).resolve()


def adb_executable() -> Path:
    adb = os.environ.get("ADB")
    adbutils = os.environ.get("ADBUTILS_ADB_PATH")
    first = _override("ADB", adb) if adb else None
    second = _override("ADBUTILS_ADB_PATH", adbutils) if adbutils else None
    if first is not None and second is not None and first != second:
        raise ValueError(f"ADB 路径冲突：ADB={first}；ADBUTILS_ADB_PATH={second}")
    selected = first or second or application_dir() / "tool" / "scrcpy-win64-v5.0" / "adb.exe"
    if not selected.is_file():
        raise ValueError(f"找不到选定的 ADB：{selected}")
    return selected.resolve()


def configure_runtime() -> None:
    global _DLL_CONFIGURED
    selected = str(adb_executable())
    os.environ["ADB"] = selected
    os.environ["ADBUTILS_ADB_PATH"] = selected
    if os.name != "nt" or not getattr(sys, "frozen", False) or _DLL_CONFIGURED:
        return
    internal = Path(getattr(sys, "_MEIPASS", application_dir() / "_internal")).resolve()
    for directory in (internal, internal / "PySide6", internal / "shiboken6", internal / "PIL"):
        if directory.is_dir():
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    if not ctypes.windll.kernel32.SetDllDirectoryW(None):
        raise ValueError(f"无法恢复外部程序 DLL 搜索路径：Windows error {ctypes.windll.kernel32.GetLastError()}")
    def outside(entry: str) -> bool:
        if not entry:
            return True
        candidate = Path(entry).resolve()
        return candidate != internal and internal not in candidate.parents
    os.environ["PATH"] = os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep) if outside(entry))
    _DLL_CONFIGURED = True

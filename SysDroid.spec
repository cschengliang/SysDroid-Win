# -*- mode: python ; coding: utf-8 -*-
"""Build only through scripts/build_sysdroid.py (it prepares the embedded stdlib)."""
from pathlib import Path
import ast
import json
import os
import sys

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

# The build script registers itself as this module; the fallback keeps the import
# meaningful (and the error below reachable) when PyInstaller is run on the spec directly.
if "build_sysdroid" not in sys.modules:
    sys.path.insert(0, str(Path(SPECPATH).resolve() / "scripts"))
from build_sysdroid import (
    BuildError, ensure_analysis_inputs, prune_unused_qt_addons, validate_stdlib_analysis,
)

source_root = Path(SPECPATH).resolve()
stage_value = os.environ.get("SYSDROID_BUILD_STAGE")
stdlib_value = os.environ.get("SYSDROID_BUILD_STDLIB")
if not stage_value or not stdlib_value:
    raise BuildError(
        "Use lib/python-3.14.8-embed-amd64/python.exe -s scripts/build_sysdroid.py; "
        "the spec requires its dedicated build stage."
    )
stage = Path(stage_value).resolve()
stdlib = Path(stdlib_value).resolve()
ensure_analysis_inputs(source_root, stage, stdlib)

runtime_names = json.loads((stage / "runtime-packages.json").read_text(encoding="utf-8"))
datas = []
for distribution_name in runtime_names:
    # In particular, copy_metadata('adbutils') preserves its runtime version lookup.
    datas += copy_metadata(distribution_name)

# App icon: the multi-size .ico goes into the EXE, the PNGs into _internal/assets/icons
# for the in-app QIcon (see sysdroid.ui.theme.app_icon).
assets = source_root / "assets"
exe_icon = assets / "SysDroid.ico"
icon_pngs = sorted((assets / "icons").glob("sysdroid-*.png"))
if not exe_icon.is_file() or len(icon_pngs) != 7:
    raise BuildError(f"App icon assets are incomplete under {assets} (need SysDroid.ico and 7 icons/sysdroid-*.png).")
datas += [(str(path), "assets/icons") for path in icon_pngs]


def _app_info(name):
    """Read a string constant from src/sysdroid/app_info.py without importing the app."""
    tree = ast.parse((source_root / "src" / "sysdroid" / "app_info.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise BuildError(f"app_info.py does not define {name}")


app_name, app_version = _app_info("APP_NAME"), _app_info("APP_VERSION")
numbers = tuple(int(part) for part in app_version.split("."))
numbers = (numbers + (0, 0, 0, 0))[:4]
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "cschengliang"),
            StringStruct("FileDescription", f"{app_name} - Android system development toolbox"),
            StringStruct("FileVersion", app_version),
            StringStruct("InternalName", app_name),
            StringStruct("OriginalFilename", f"{app_name}.exe"),
            StringStruct("ProductName", app_name),
            StringStruct("ProductVersion", app_version),
            StringStruct("LegalCopyright", "cschengliang"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

site_packages = source_root / "lib" / "python-3.14.8-embed-amd64" / "Lib" / "site-packages"
binaries = []
for category, filename in (
    ("platforms", "qwindows.dll"),
    ("styles", "qmodernwindowsstyle.dll"),
):
    plugin = site_packages / "PySide6" / "plugins" / category / filename
    if not plugin.is_file():
        raise BuildError(f"Required Qt plugin is missing: {plugin}")
    binaries.append((str(plugin), f"PySide6/plugins/{category}"))

# Keep the contributed adbutils hook and the normal Qt/Pillow/certifi hooks.
# No collect_all(), QML tree, development scripts, tests, or Qt Addons are bundled.
a = Analysis(
    # Not the root sysdroid.py launcher: its name would shadow the sysdroid package.
    [str(source_root / "src" / "sysdroid" / "__main__.py")],
    pathex=[str(stdlib), str(source_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "PIL.Image", "PIL._imaging", "importlib.metadata",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "_pytest", "pytestqt", "PyInstaller",
        "_pyinstaller_hooks_contrib", "pip", "setuptools", "win32ctypes",
        "pefile", "peutils", "tkinter",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2", "PySide6.QtQuickTest", "PySide6.QtQuickWidgets",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtWebView",
    ],
    noarchive=False,
    optimize=0,
)
prune_unused_qt_addons(a, stage)
validate_stdlib_analysis(a, source_root, stage, stdlib)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SysDroid",
    icon=[str(exe_icon)],
    version=version_info,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SysDroid-win-x64",
)

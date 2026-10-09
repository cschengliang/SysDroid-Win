# -*- mode: python ; coding: utf-8 -*-
"""Build only through scripts/build_android_toolbox.py (it prepares the embedded stdlib)."""
from pathlib import Path
import json
import os
import sys

from PyInstaller.utils.hooks import copy_metadata

# The build script registers itself as this module; the fallback keeps the import
# meaningful (and the error below reachable) when PyInstaller is run on the spec directly.
if "build_android_toolbox" not in sys.modules:
    sys.path.insert(0, str(Path(SPECPATH).resolve() / "scripts"))
from build_android_toolbox import (
    BuildError, ensure_analysis_inputs, prune_unused_qt_addons, validate_stdlib_analysis,
)

source_root = Path(SPECPATH).resolve()
stage_value = os.environ.get("ANDROID_TOOLBOX_BUILD_STAGE")
stdlib_value = os.environ.get("ANDROID_TOOLBOX_BUILD_STDLIB")
if not stage_value or not stdlib_value:
    raise BuildError(
        "Use lib/python-3.14.8-embed-amd64/python.exe -s scripts/build_android_toolbox.py; "
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
    [str(source_root / "android_toolbox.py")],
    pathex=[str(stdlib), str(source_root / "src"), str(source_root)],
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
    name="AndroidToolbox",
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
    name="AndroidToolbox-win-x64",
)

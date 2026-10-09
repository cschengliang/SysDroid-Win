import os
from importlib import metadata
from pathlib import Path
import shutil
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PE dependency closure")
def test_python_abi_forwarder_requires_matching_implementation(tmp_path):
    pytest.importorskip("pefile")
    from portable_assets import _audit

    provider = metadata.distribution("shiboken6")
    bindings = Path(provider.locate_file("shiboken6"))
    for binary in bindings.glob("*.dll"):
        shutil.copy2(binary, tmp_path / binary.name)
    interpreter = Path(sys.executable).parent
    shutil.copy2(interpreter / "python3.dll", tmp_path / "python3.dll")

    incomplete = _audit(tmp_path)
    assert any(row["kind"] == "forwarder" and row["name"] == "python314.dll"
               for row in incomplete["missing"])

    shutil.copy2(interpreter / "python314.dll", tmp_path / "python314.dll")
    complete = _audit(tmp_path)
    assert complete["missing"] == []
    assert any(row["kind"] == "forwarder" and row["name"] == "python314.dll"
               and row["resolved"] == "python314.dll" for row in complete["imports"])


@pytest.mark.skipif(os.name != "nt", reason="Windows PE dependency closure")
def test_large_qt_binding_reports_dependencies_after_full_import_table(tmp_path):
    pytest.importorskip("pefile")
    from portable_assets import _audit

    provider = metadata.distribution("PySide6-Essentials")
    binary = Path(provider.locate_file("PySide6/QtCore.pyd"))
    shutil.copy2(binary, tmp_path / binary.name)

    report = _audit(tmp_path)
    assert {row["name"] for row in report["missing"]} == {
        "pyside6.abi3.dll", "shiboken6.abi3.dll", "python3.dll", "qt6core.dll",
        "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    }

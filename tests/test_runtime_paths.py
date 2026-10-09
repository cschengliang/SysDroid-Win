import os
from pathlib import Path

import pytest

from runtime_paths import adb_executable, configure_runtime


def test_conflicting_or_missing_overrides_never_fall_back(monkeypatch, tmp_path):
    a = tmp_path / "a.exe"
    b = tmp_path / "b.exe"
    a.touch()
    b.touch()
    monkeypatch.setenv("ADB", str(a))
    monkeypatch.setenv("ADBUTILS_ADB_PATH", str(b))
    with pytest.raises(ValueError, match="冲突"):
        configure_runtime()
    monkeypatch.setenv("ADB", str(tmp_path / "missing.exe"))
    monkeypatch.delenv("ADBUTILS_ADB_PATH")
    with pytest.raises(ValueError, match="missing.exe"):
        adb_executable()


def test_default_ignores_path_and_explicit_choice_configures_both(monkeypatch, tmp_path):
    import runtime_paths
    adb = tmp_path / "tool" / "scrcpy-win64-v5.0" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.delenv("ADBUTILS_ADB_PATH", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(runtime_paths, "application_dir", lambda: tmp_path)
    configure_runtime()
    assert Path(os.environ["ADB"]) == adb
    assert os.environ["ADB"] == os.environ["ADBUTILS_ADB_PATH"]
    adb.unlink()
    with pytest.raises(ValueError):
        adb_executable()

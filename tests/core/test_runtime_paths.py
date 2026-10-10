import os
from pathlib import Path

import pytest

from sysdroid.runtime_paths import adb_executable, configure_runtime


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
    from sysdroid import runtime_paths
    adb = tmp_path / "tool" / "scrcpy-win64-v5.0" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    # setenv first so monkeypatch records (and later restores) the original state:
    # configure_runtime() writes both variables directly into os.environ.
    for name in ("ADB", "ADBUTILS_ADB_PATH"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(runtime_paths, "application_dir", lambda: tmp_path)
    configure_runtime()
    assert Path(os.environ["ADB"]) == adb
    assert os.environ["ADB"] == os.environ["ADBUTILS_ADB_PATH"]
    adb.unlink()
    with pytest.raises(ValueError):
        adb_executable()


def test_source_mode_resolves_the_repository_root_that_holds_tool_and_lib(monkeypatch):
    from sysdroid import runtime_paths
    root = Path(__file__).resolve().parents[2]
    monkeypatch.delattr(runtime_paths.sys, "frozen", raising=False)
    assert runtime_paths.application_dir() == root
    assert (root / "src" / "sysdroid" / "runtime_paths.py").is_file()
    assert (root / "android_toolbox.py").is_file()


def test_frozen_mode_uses_the_executable_folder(monkeypatch, tmp_path):
    from sysdroid import runtime_paths
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "executable", str(tmp_path / "AndroidToolbox.exe"))
    assert runtime_paths.application_dir() == tmp_path.resolve()

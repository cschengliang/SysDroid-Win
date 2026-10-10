import json
from pathlib import Path

from sysdroid.core import data_dir
from sysdroid.core.data_dir import (MIGRATION_MARKER, legacy_data_dir, migrate_legacy_data,
                                    resolve_data_dir)


def test_default_is_localappdata_sysdroid(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert resolve_data_dir(env) == tmp_path / "SysDroid"
    assert legacy_data_dir(env) == tmp_path / "AndroidToolbox"


def test_new_override_wins_then_old_variable_is_a_fallback(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path), "SYSDROID_DATA_DIR": str(tmp_path / "new"),
           "ANDROID_TOOLBOX_DATA_DIR": str(tmp_path / "old")}
    assert resolve_data_dir(env) == tmp_path / "new"
    del env["SYSDROID_DATA_DIR"]
    assert resolve_data_dir(env) == tmp_path / "old"
    # An explicit folder is the user's choice: nothing is migrated into it.
    assert legacy_data_dir(env) is None
    env["SYSDROID_DATA_DIR"] = ""
    assert resolve_data_dir(env) == tmp_path / "old"


def test_missing_localappdata_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr(data_dir.Path, "home", classmethod(lambda cls: tmp_path))
    assert resolve_data_dir({}) == tmp_path / "SysDroid"


def _legacy(tmp_path: Path) -> Path:
    legacy = tmp_path / "AndroidToolbox"
    (legacy / "exec-out").mkdir(parents=True)
    (legacy / "commands.json").write_text('{"old": 1}', encoding="utf-8")
    (legacy / "workspace.ini").write_text("[ui]\n", encoding="utf-8")
    (legacy / "exec-out" / "run.log").write_text("transient", encoding="utf-8")
    return legacy


def test_one_time_copy_keeps_legacy_and_existing_files(tmp_path):
    legacy = _legacy(tmp_path)
    target = tmp_path / "SysDroid"
    target.mkdir()
    (target / "workspace.ini").write_text("[new]\n", encoding="utf-8")

    assert migrate_legacy_data(target, legacy) == "migrated"
    assert (target / "commands.json").read_text(encoding="utf-8") == '{"old": 1}'
    assert (target / "workspace.ini").read_text(encoding="utf-8") == "[new]\n"  # SysDroid's own file wins
    assert not (target / "exec-out").exists()
    record = json.loads((target / MIGRATION_MARKER).read_text(encoding="utf-8"))
    assert record["copied"] == ["commands.json"] and record["from"] == str(legacy)
    # The legacy folder is untouched.
    assert sorted(p.name for p in legacy.iterdir()) == ["commands.json", "exec-out", "workspace.ini"]

    # Second start: the marker stops a re-copy even if the user deleted a file on purpose.
    (target / "commands.json").unlink()
    assert migrate_legacy_data(target, legacy) == "already"
    assert not (target / "commands.json").exists()


def test_nothing_to_migrate_creates_no_folder(tmp_path):
    target = tmp_path / "SysDroid"
    assert migrate_legacy_data(target, tmp_path / "AndroidToolbox") == "none"
    assert migrate_legacy_data(target, None) == "none"
    assert not target.exists()


def test_failed_copy_writes_no_marker_so_the_next_start_retries(monkeypatch, tmp_path):
    legacy = _legacy(tmp_path)
    target = tmp_path / "SysDroid"

    def broken(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(data_dir.shutil, "copy2", broken)
    assert migrate_legacy_data(target, legacy) == "failed"
    assert not (target / MIGRATION_MARKER).exists()
    monkeypatch.undo()
    assert migrate_legacy_data(target, legacy) == "migrated"
    assert (target / "commands.json").is_file()

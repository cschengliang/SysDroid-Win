import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from sysdroid.core.commands import (
    EXECUTION_TYPES,
    Command,
    CommandStore,
    StorageError,
    Workflow,
    infer_execution_type,
    prepare_command,
    split_template,
    validate_command,
)


def test_template_quotes_preserve_windows_paths_and_literal_quotes():
    template = '''adb push "C:\\My Files\\folder\\" 'remote ''quoted'' path' '''
    assert split_template(template) == ["adb", "push", "C:\\My Files\\folder\\", "remote 'quoted' path"]
    assert split_template('adb shell printf ""') == ["adb", "shell", "printf", ""]
    with pytest.raises(ValueError, match="引号"):
        split_template('adb push "unfinished')


def test_corrupt_store_is_preserved_until_explicit_backed_up_recovery(tmp_path):
    path = tmp_path / "commands.json"
    damaged = b'{ "version": '
    path.write_bytes(damaged)
    store = CommandStore(path)
    with pytest.raises(StorageError):
        store.save_command(Command("new", "new", "Shell", "adb shell echo hello"))
    assert path.read_bytes() == damaged
    backup = store.recover_defaults()
    assert backup is not None and backup.read_bytes() == damaged
    assert not CommandStore(path).error
    recovered = json.loads(path.read_text(encoding="utf-8"))
    assert recovered["version"] == 3
    assert all(record["execution_type"] in EXECUTION_TYPES for record in recovered["commands"])


def test_deleting_command_removes_saved_workflow_references(tmp_path):
    path = tmp_path / "commands.json"
    store = CommandStore(path)
    store.save_workflow(Workflow("ordered", "ordered", ["devices", "props", "devices"]))
    store.delete_command("devices")
    restored = CommandStore(path)
    assert "devices" not in restored.commands
    assert restored.workflows["ordered"].steps == ["props"]


@pytest.mark.parametrize("template, expected", [
    ("adb wait-for-device", "wait"),
    ("adb logcat", "continuous"),
    ("adb logcat -d", "quick"),
    ("adb logcat -t100", "quick"),
    ("adb logcat -T 100", "continuous"),
    ("adb logcat -d -T 100", "quick"),
    ("adb shell logcat", "continuous"),
    ("adb exec-out logcat -t100", "quick"),
    ("adb shell ping example.org", "continuous"),
    ("adb shell ping -c 3 example.org", "quick"),
    ("adb exec-out ping -c3 example.org", "quick"),
    ("adb shell ping -W 5 example.org", "continuous"),
    ("adb shell ping -c {count} example.org", "long"),
    ("adb shell top", "continuous"),
    ("adb exec-out top -n2", "quick"),
    ("adb shell top -n {count}", "long"),
    ("adb devices -l", "quick"),
    ("adb shell getprop {key}", "quick"),
    ("adb exec-out /system/bin/getprop ro.product.model", "quick"),
    ("adb shell pm list packages -3", "quick"),
    ("adb shell cmd package list packages", "quick"),
    ("adb shell wm size", "quick"),
    ("adb shell {command}", "long"),
    ("adb shell sh -c 'getprop ro.product.model'", "long"),
    ('adb shell "getprop ro.product.model"', "long"),
    ("adb shell getprop; sleep 10", "long"),
    ("adb shell getprop\nid", "long"),
    ("adb shell echo $(getprop)", "long"),
    ('adb shell "unfinished', "long"),
])
def test_migration_inference_recognizes_only_simple_execution_patterns(template, expected):
    assert infer_execution_type(template) == expected


def test_v1_migration_is_atomic_and_preserves_records_workflow_order_and_explicit_labels(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    commands = [
        Command("logs", "logs", "系统", "adb logcat", timeout=90, favorite=True),
        Command("manual", "manual", "系统", "adb shell getprop", tags="kept", execution_type="long", show_in_library=False),
        Command("wait", "wait", "设备", "adb wait-for-device"),
    ]
    records = [asdict(command) for command in commands]
    del records[0]["execution_type"]
    del records[2]["execution_type"]
    del records[0]["show_in_library"]
    del records[2]["show_in_library"]
    workflows = [
        asdict(Workflow("second", "second", ["manual", "logs", "manual"])),
        asdict(Workflow("first", "first", ["wait", "logs"])),
    ]
    original = json.dumps({"version": 1, "commands": records, "workflows": workflows}).encode()
    path.write_bytes(original)
    replacements = []
    original_replace = Path.replace

    def observe_replace(temporary, destination):
        assert destination == path
        assert path.read_bytes() == original
        assert json.loads(temporary.read_text(encoding="utf-8"))["version"] == 3
        replacements.append(temporary)
        return original_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", observe_replace)
    store = CommandStore(path)
    assert not store.error
    assert list(store.commands)[:3] == ["logs", "manual", "wait"]
    assert len(store.commands) == 7
    assert list(store.workflows) == ["second", "first"]
    expected = [
        {**records[0], "execution_type": "continuous", "show_in_library": True},
        records[1],
        {**records[2], "execution_type": "wait", "show_in_library": True},
    ]
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["version"] == 3
    assert migrated["commands"][:3] == expected
    assert all(record["show_in_library"] is False for record in migrated["commands"][3:])
    assert migrated["workflows"] == workflows
    reloaded = CommandStore(path)
    assert reloaded.commands == store.commands
    assert reloaded.workflows == store.workflows
    assert len(replacements) == 1


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("failure_stage", ["write", "replace"])
def test_failed_legacy_migration_preserves_original_and_remains_read_only(tmp_path, monkeypatch, version, failure_stage):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    if version == 1:
        del record["execution_type"]
    del record["show_in_library"]
    original = json.dumps({"version": version, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    original_write = Path.write_text

    def fail_write(temporary, text, **kwargs):
        original_write(temporary, text[:10], **kwargs)
        raise OSError("simulated migration write failure")

    def fail_replace(temporary, destination):
        assert destination == path
        assert path.read_bytes() == original
        raise OSError("simulated migration replace failure")

    monkeypatch.setattr(Path, "write_text" if failure_stage == "write" else "replace",
                        fail_write if failure_stage == "write" else fail_replace)
    store = CommandStore(path)
    assert store.error
    assert not store.commands and not store.workflows
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(StorageError):
        store.save_command(Command("new", "new", "系统", "adb shell getprop"))
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("execution_type", ["interactive", []])
def test_invalid_stored_execution_type_is_rejected_without_overwriting_original(tmp_path, version, execution_type):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    record["execution_type"] = execution_type
    original = json.dumps({"version": version, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    assert store.error
    assert not store.commands and not store.workflows
    with pytest.raises(StorageError):
        store.save_workflow(Workflow("empty", "empty", []))
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [2, 3])
def test_current_and_v2_missing_execution_type_are_not_inferred(tmp_path, version):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    del record["execution_type"]
    original = json.dumps({"version": version, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    assert store.error
    assert path.read_bytes() == original
    with pytest.raises(StorageError):
        store.delete_command("query")


def test_invalid_v1_template_does_not_escape_read_only_recovery(tmp_path):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    record["template"] = None
    del record["execution_type"]
    original = json.dumps({"version": 1, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    assert CommandStore(path).error
    assert path.read_bytes() == original


@pytest.mark.parametrize("execution_type", ["quick", "long", "continuous", "wait"])
def test_v2_save_edit_and_reload_retain_user_execution_choice(tmp_path, execution_type):
    path = tmp_path / "commands.json"
    command = Command("custom", "custom", "系统", "adb logcat", timeout=73,
                      permission="root", execution_type=execution_type)
    record = asdict(command)
    del record["show_in_library"]
    path.write_text(json.dumps({"version": 2, "commands": [record], "workflows": []}), encoding="utf-8")
    store = CommandStore(path)
    assert not store.error
    assert store.commands["custom"] == command
    edited = replace(store.commands["custom"], name="renamed", template="adb shell getprop")
    store.save_command(edited)
    restored = CommandStore(path)
    assert not restored.error
    assert restored.commands["custom"] == edited
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3
    prepared = prepare_command(restored.commands["custom"], {}, "serial-1")
    assert prepared.args == ("shell", "getprop")
    assert prepared.serial == "serial-1"
    assert prepared.timeout == 73
    assert prepared.permission == "root"


@pytest.mark.parametrize("execution_type", ["interactive", []])
def test_invalid_execution_type_cannot_be_saved_over_valid_v3_library(tmp_path, execution_type):
    path = tmp_path / "commands.json"
    command = Command("query", "query", "系统", "adb shell getprop", execution_type="wait")
    original = json.dumps({"version": 3, "commands": [asdict(command)], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    invalid = replace(command, execution_type=execution_type)
    with pytest.raises(ValueError):
        validate_command(invalid)
    with pytest.raises(ValueError):
        store.save_command(invalid)
    assert path.read_bytes() == original
    assert store.commands["query"] == command


def test_v2_migration_defaults_missing_visibility_and_preserves_explicit_flags(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    commands = [
        Command("missing", "missing", "系统", "adb shell getprop", execution_type="wait"),
        Command("hidden", "hidden", "系统", "adb logcat", timeout=80, permission="root",
                execution_type="long", show_in_library=False),
        Command("visible", "visible", "Shell", "adb shell echo hello", tags="kept", favorite=True,
                execution_type="continuous", show_in_library=True),
    ]
    records = [asdict(command) for command in commands]
    del records[0]["show_in_library"]
    workflow = Workflow("ordered", "ordered", ["hidden", "missing", "visible", "hidden"])
    original = json.dumps({"version": 2, "commands": records, "workflows": [asdict(workflow)]}).encode()
    path.write_bytes(original)
    replacements = []
    original_replace = Path.replace

    def observe_replace(temporary, destination):
        assert destination == path
        assert path.read_bytes() == original
        assert json.loads(temporary.read_text(encoding="utf-8"))["version"] == 3
        replacements.append(temporary)
        return original_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", observe_replace)
    store = CommandStore(path)
    assert not store.error
    assert list(store.commands)[:3] == ["missing", "hidden", "visible"]
    for command in commands:
        assert store.commands[command.id] == command
    assert store.workflows[workflow.id] == workflow
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["version"] == 3
    assert migrated["commands"][:3] == [asdict(command) for command in commands]
    assert len(migrated["commands"]) == 7
    assert all(type(record["show_in_library"]) is bool for record in migrated["commands"])
    assert all(record["show_in_library"] is False for record in migrated["commands"][3:])
    assert migrated["workflows"] == [asdict(workflow)]
    restored = CommandStore(path)
    assert not restored.error
    assert restored.commands == store.commands
    assert restored.workflows == store.workflows
    assert len(replacements) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_migration_deduplicates_hidden_catalog_by_id_or_exact_template_only_once(tmp_path, monkeypatch, version):
    path = tmp_path / "commands.json"
    imported_id = "6399d9ec-8293-4a16-b286-f877a66e70f6"
    commands = [
        Command("preset-version", "user ID owner", "应用", "adb shell pm list packages -3",
                tags="custom", favorite=True, execution_type="continuous"),
        Command(imported_id, "imported size", "设备", "adb shell wm size", timeout=73,
                permission="root", execution_type="wait", show_in_library=False),
        Command("near-match", "near match", "文件", "adb shell df -h /data ", execution_type="long"),
    ]
    records = [asdict(command) for command in commands]
    del records[0]["show_in_library"]
    del records[2]["show_in_library"]
    workflow = Workflow("ordered", "ordered", [imported_id, "preset-version", imported_id, "near-match"])
    path.write_text(json.dumps({"version": version, "commands": records, "workflows": [asdict(workflow)]}),
                    encoding="utf-8")
    replacements = []
    original_replace = Path.replace

    def observe_replace(temporary, destination):
        replacements.append(temporary)
        return original_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", observe_replace)
    store = CommandStore(path)
    assert not store.error
    assert list(store.commands) == ["preset-version", imported_id, "near-match", "preset-storage", "preset-logcat"]
    for command in commands:
        assert store.commands[command.id] == command
    assert "preset-size" not in store.commands
    assert store.commands["preset-storage"].show_in_library is False
    assert store.commands["preset-logcat"].show_in_library is False
    assert store.commands["preset-storage"].execution_type == "quick"
    assert store.commands["preset-logcat"].execution_type == "quick"
    assert store.workflows[workflow.id] == workflow
    restored = CommandStore(path)
    assert not restored.error
    assert restored.commands == store.commands
    assert restored.workflows == store.workflows
    assert len(replacements) == 1

    restored.delete_command("preset-logcat")
    deleted_bytes = path.read_bytes()
    reloaded = CommandStore(path)
    assert not reloaded.error
    assert "preset-logcat" not in reloaded.commands
    assert "preset-size" not in reloaded.commands
    assert reloaded.commands[imported_id] == commands[1]
    assert reloaded.workflows[workflow.id] == workflow
    assert path.read_bytes() == deleted_bytes
    assert len(replacements) == 2


def test_visibility_toggle_persists_without_changing_workflow_references_or_execution(tmp_path):
    path = tmp_path / "commands.json"
    command = Command("query", "query", "系统", "adb shell getprop", timeout=73,
                      permission="root", execution_type="wait")
    other = Command("other", "other", "设备", "adb devices", scope="host")
    workflow = Workflow("ordered", "ordered", ["query", "other", "query"])
    path.write_text(json.dumps({"version": 3, "commands": [asdict(command), asdict(other)],
                               "workflows": [asdict(workflow)]}), encoding="utf-8")
    store = CommandStore(path)
    assert not store.error

    for visibility in (False, True):
        edited = replace(command, show_in_library=visibility)
        store.save_command(edited)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["version"] == 3
        assert persisted["commands"][0]["show_in_library"] is visibility
        assert persisted["workflows"] == [asdict(workflow)]
        store = CommandStore(path)
        assert not store.error
        assert list(store.commands) == ["query", "other"]
        assert store.commands["query"] == edited
        assert store.commands["other"] == other
        assert store.workflows[workflow.id] == workflow
        prepared = prepare_command(store.commands["query"], {}, "serial-1")
        assert prepared.args == ("shell", "getprop")
        assert prepared.timeout == 73
        assert prepared.permission == "root"


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("visibility", [0, 1, "false", None, [], {}])
def test_invalid_stored_visibility_is_rejected_without_overwriting_original(tmp_path, version, visibility):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    record["show_in_library"] = visibility
    original = json.dumps({"version": version, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    assert store.error
    assert not store.commands and not store.workflows
    with pytest.raises(StorageError):
        store.save_command(Command("new", "new", "系统", "adb shell getprop"))
    assert path.read_bytes() == original


def test_v3_missing_visibility_is_rejected_without_defaulting_or_overwriting_original(tmp_path):
    path = tmp_path / "commands.json"
    record = asdict(Command("query", "query", "系统", "adb shell getprop"))
    del record["show_in_library"]
    original = json.dumps({"version": 3, "commands": [record], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    assert store.error
    assert not store.commands and not store.workflows
    with pytest.raises(StorageError):
        store.delete_command("query")
    assert path.read_bytes() == original


@pytest.mark.parametrize("visibility", [0, 1, "false", None])
def test_invalid_visibility_cannot_be_saved_over_valid_library(tmp_path, visibility):
    path = tmp_path / "commands.json"
    command = Command("query", "query", "系统", "adb shell getprop", show_in_library=False)
    original = json.dumps({"version": 3, "commands": [asdict(command)], "workflows": []}).encode()
    path.write_bytes(original)
    store = CommandStore(path)
    assert not store.error
    invalid = replace(command, show_in_library=visibility)
    with pytest.raises(ValueError):
        validate_command(invalid)
    with pytest.raises(ValueError):
        store.save_command(invalid)
    assert path.read_bytes() == original
    assert store.commands[command.id] == command

import json
import shlex
from urllib.parse import unquote

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from sysdroid.core.backend import Task
from sysdroid.core.settings import SettingsController, SettingValue, parse_setting_names
from sysdroid.core.users import AndroidUserController
from sysdroid.ui.pages.settings_page import SettingsPage


class ManualSettingsRunner(QObject):
    task_added = Signal(object)
    task_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.start_error = None

    def start_adb(self, title, args, serial="", command_id="", timeout=10, *, transient=False):
        if self.start_error is not None:
            error, self.start_error = self.start_error, None
            raise error
        task = Task(f"settings-{len(self.requests) + 1}", title, "adb",
                    (["-s", serial] if serial else []) + list(args), serial, command_id)
        task.status = "running"
        self.requests.append(task)
        self.task_added.emit(task)
        return task

    def finish(self, task, stdout="", stderr="", exit_code=0, status=None):
        task.stdout, task.stderr, task.exit_code = stdout, stderr, exit_code
        task.status = status or ("succeeded" if exit_code == 0 else "failed")
        self.task_finished.emit(task)


def tokens(task):
    return shlex.split(task.args[-1])


def names_output(*names):
    return "".join(f"Row: {index} name={name}\n" for index, name in enumerate(names)) if names else "No result found.\n"


def script_lines(script):
    """Split a generated shell script into commands, keeping quoted newlines inside their command."""
    commands, pending = [], ""
    for line in script.split("\n"):
        pending = f"{pending}\n{line}" if pending else line
        try:
            shlex.split(pending)
        except ValueError:
            continue
        commands.append(pending)
        pending = ""
    assert not pending
    return commands


def framed_output(task, respond):
    """Answer a printf-framed settings script; ``respond(kind, index)`` returns each record body."""
    records = []
    lines = script_lines(task.args[-1])
    for offset in range(0, len(lines), 4):
        _, nonce, index, kind = shlex.split(lines[offset])[1].replace("\\n", "").split(" ")
        body, code = respond(kind, index)
        records.append(f"BEGIN {nonce} {index} {kind}\n{body}\nEND {nonce} {index} {kind} {code}\n")
    return "".join(records)


def finish_read(runner, name, value, *, after_exists=None, mutate="", mutate_code=0):
    """Finish the latest read (or change) script with a single observed value."""
    exists = value is not None
    exists_after = exists if after_exists is None else after_exists

    def respond(kind, index):
        if kind == "mutate":
            return mutate.removesuffix("\n"), mutate_code
        if kind == "before":
            return (names_output(name) if exists else names_output()).removesuffix("\n"), 0
        if kind == "get":
            return (f"Row: 0 value={value}" if exists else names_output().removesuffix("\n")), 0
        return (names_output(name) if exists_after else names_output()).removesuffix("\n"), 0
    runner.finish(runner.requests[-1], framed_output(runner.requests[-1], respond))


class DeviceSettings:
    """Manual provider fixture with query/insert/delete framing and null canonicalization."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def _response(self, command):
        uri = command[command.index("--uri") + 1]
        namespace, separator, encoded_name = uri.removeprefix("content://settings/").partition("/")
        user_id = int(command[command.index("--user") + 1])
        name = unquote(encoded_name)
        key = user_id, namespace, name
        if command[1] == "insert":
            bindings = {command[i + 1].split(":", 2)[0]: command[i + 1].split(":", 2)[2]
                        for i, argument in enumerate(command) if argument == "--bind"}
            value = bindings["value"]
            self.values[(user_id, namespace, bindings["name"])] = None if value == "null" else value
            output = ""
        elif command[1] == "delete":
            existed = key in self.values
            self.values.pop(key, None)
            output = f"Deleted {int(existed)} rows\n"
        elif separator:
            if key not in self.values:
                output = names_output()
            elif command[command.index("--projection") + 1] == "value":
                value = self.values[key]
                output = f"Row: 0 value={'NULL' if value is None else value}\n"
            else:
                output = names_output(name)
        else:
            members = [(key[2], value) for key, value in self.values.items() if key[:2] == (user_id, namespace)]
            if command[command.index("--projection") + 1] == "name:value":
                output = "".join(f"Row: {index} name={name}, value={'NULL' if value is None else value}\n"
                                 for index, (name, value) in enumerate(members)) if members else names_output()
            else:
                output = names_output(*(name for name, _ in members))
        return output

    def batch_output(self, task):
        records = []
        lines = script_lines(task.args[-1])
        for offset in range(0, len(lines), 4):
            begin = shlex.split(lines[offset])[1].replace("\\n", "\n")
            response = self._response(shlex.split(lines[offset + 1]))
            end = shlex.split(lines[offset + 3])[1].replace("\\n", "\n").replace("%s", "0")
            records.extend((begin, response, end))
        return "".join(records)

    def answer(self, runner):
        task = runner.requests[-1]
        output = self.batch_output(task) if task.args[-1].startswith("printf ") else self._response(tokens(task))
        runner.finish(task, output)

    def settle(self, runner, controller):
        for _ in range(100):
            if not controller.busy:
                return
            self.answer(runner)
        raise AssertionError("Settings operation did not terminate")


@pytest.fixture
def settings_runner(qapp):
    return ManualSettingsRunner()


@pytest.fixture
def controller(settings_runner):
    instance = SettingsController(settings_runner)
    instance.set_device("device-a")
    instance.set_context("secure", 10)
    return instance


@pytest.fixture
def settings_page(settings_runner):
    users = AndroidUserController(settings_runner)
    users.set_device("device-a")
    page = SettingsPage(settings_runner, users)
    page.set_device("device-a")
    page.set_active(True)
    settings_runner.finish(settings_runner.requests[-1], "Users:\n\tUserInfo{0:Owner:13} running\n\tUserInfo{10:工作用户:10}\n")
    settings_runner.finish(settings_runner.requests[-1], "0\n")
    DeviceSettings({(0, "system", "existing"): "initial existing", (0, "system", "other"): "initial other"}).settle(
        settings_runner, page.controller)
    yield page
    page.set_active(False)
    page.deleteLater()


@pytest.mark.parametrize("output,expected", [
    ("Row: 0 name=中文\nRow: 1 name=has space\n", ("中文", "has space")),
    ("Row: 8 name=quote'\"$;[]/key", ("quote'\"$;[]/key",)),
    ("Row: 0 name=a\r\nRow: 1 name=b\r\n", ("a", "b")),
    ("No result found.\n", ()),
])
def test_name_query_accepts_only_complete_records(output, expected):
    assert parse_setting_names(output) == expected


@pytest.mark.parametrize("output", [
    "", "\n", "No result found.\n\n", "No result found.\nRow: 0 name=a\n",
    "Row: 0 name=\n", "Row: 0 value=a\n", "Row: 0 name=a\nRow: 1 name=a\n",
    "Row: 0 name=a\nRow: 0 name=b\n", "Row: 0 name=a\nError: permission denied\n",
    "Exception occurred while executing 'query'\n", "Row: 0 name=bad\0name\n",
    "Row: 0 name=a\rbroken\n", "Row: 0 name=a\nRow: 1\n",
])
def test_diagnostics_and_partial_name_queries_are_not_successful_empty_snapshots(output):
    with pytest.raises(ValueError):
        parse_setting_names(output)


@pytest.mark.parametrize("value", [
    "", "--user", "  text  ", "\r", "\n", "\r\n", "first\r\nsecond\n  ",
    "  中文🙂 '\"$;[]\\\r\n尾\n  ", "\u2028value\u2029", "Error: this is a literal value",
])
def test_exact_values_round_trip_and_delete_without_confusing_null_empty_missing(
        settings_runner, controller, value):
    name = "测试/quote'\"$;[] key"
    device = DeviceSettings()
    controller.write(name, value)
    device.settle(settings_runner, controller)
    assert device.values[(10, "secure", name)] == value
    assert controller.values[name] == SettingValue(True, value)
    assert not controller.error and "写入成功" in controller.status
    controller.read(name)
    device.settle(settings_runner, controller)
    assert controller.values[name] == SettingValue(True, value)
    controller.delete(name)
    device.settle(settings_runner, controller)
    assert controller.values[name] == SettingValue(False, None)
    assert name not in controller.names
    assert not controller.error and "删除成功" in controller.status


def test_missing_empty_and_ambiguous_provider_null_are_distinct_observations(settings_runner, controller):
    for name, value in [("missing", None), ("empty", ""), ("text-null", "null"), ("provider-null", "NULL")]:
        controller.read(name)
        finish_read(settings_runner, name, value)
    assert controller.values == {
        "missing": SettingValue(False, None), "empty": SettingValue(True, ""),
        "text-null": SettingValue(True, "null"), "provider-null": SettingValue(True, None)}
    assert "unread" not in controller.values


@pytest.mark.parametrize("original,new_value,after_exists", [
    (SettingValue(True, "old"), "new", False),
    (SettingValue(False, None), None, True),
])
def test_external_existence_change_never_publishes_mixed_read(
        settings_runner, controller, original, new_value, after_exists):
    controller.values = {"key": original}
    controller.read("key")
    finish_read(settings_runner, "key", new_value, after_exists=after_exists)
    assert controller.values["key"] == original
    assert controller.last_read_name == "" and controller.read_revision == 0
    assert controller.error and not controller.busy


@pytest.mark.parametrize("operation,observed", [("write", "different"), ("write", None), ("delete", "still here")])
def test_business_readback_mismatch_exposes_actual_value_not_requested_success(
        settings_runner, controller, operation, observed):
    if operation == "write":
        controller.write("key", "wanted")
    else:
        controller.delete("key")
    assert len(settings_runner.requests) == 1
    finish_read(settings_runner, "key", observed, mutate="" if operation == "write" else "Deleted 1 rows")
    assert controller.values["key"] == SettingValue(observed is not None, observed)
    assert controller.error and "成功" not in controller.status


@pytest.mark.parametrize("failure", ["stderr", "nonzero", "diagnostic", "record-rc", "truncated"])
def test_failed_readback_preserves_known_snapshot_and_never_acknowledges_command(
        settings_runner, controller, failure):
    old = SettingValue(True, "previous")
    controller.values = {"key": old}
    task = controller.write("key", "wanted")
    assert "content insert" in task.args[-1] and "content query" in task.args[-1]

    def respond(kind, index):
        if kind == "get" and failure == "diagnostic":
            return "Error: provider permission denied", 0
        if kind == "after" and failure == "record-rc":
            return "Row: 0 name=key", 1
        return {"mutate": "", "get": "Row: 0 value=wanted"}.get(kind, "Row: 0 name=key"), 0
    output = framed_output(task, respond)
    if failure == "stderr":
        settings_runner.finish(task, output, "SecurityException: denied")
    elif failure == "nonzero":
        settings_runner.finish(task, output, "device offline", 1)
    elif failure == "truncated":
        settings_runner.finish(task, output[:len(output) // 2])
    else:
        settings_runner.finish(task, output)
    assert controller.values == {"key": old}
    assert controller.error and "成功" not in controller.status
    assert "未确认" in controller.status and not controller.busy


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_change_submission_failure_reports_not_executed(settings_runner, controller, operation):
    original = SettingValue(True, "known")
    controller.values = {"key": original}
    settings_runner.start_error = OSError("launch failure")
    with pytest.raises(ValueError):
        controller.write("key", "wanted") if operation == "write" else controller.delete("key")
    assert controller.values == {"key": original} and not controller.busy
    assert "未确认" not in controller.status and "launch failure" in controller.error


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_single_value_operations_take_one_round_trip(settings_runner, controller, operation):
    device = DeviceSettings({(10, "secure", "key"): "old"})
    getattr(controller, operation)(*(("key", "new") if operation == "write" else ("key",)))
    device.answer(settings_runner)
    assert len(settings_runner.requests) == 1 and not controller.busy and not controller.error


def test_write_many_writes_and_verifies_all_names_in_one_script(settings_runner, controller):
    device = DeviceSettings({(10, "global", "a"): "1", (10, "secure", "shown"): "x"})
    controller.names = ("shown",)
    controller.values = {"shown": SettingValue(True, "x")}
    controller.write_many("global", [("a", "0.5"), ("b", "0.5"), ("c", "0.5")])
    device.answer(settings_runner)
    assert len(settings_runner.requests) == 1 and not controller.busy
    assert all(device.values[(10, "global", name)] == "0.5" for name in "abc")
    assert "已写入 3 项" in controller.status and not controller.error
    # Another namespace's readback never leaks into the browsed snapshot.
    assert controller.names == ("shown",) and set(controller.values) == {"shown"}


def test_write_many_reports_each_mismatch(settings_runner, controller):
    controller.set_context("global", 10)
    task = controller.write_many("global", [("a", "1"), ("b", "2")])

    def respond(kind, index):
        name = "ab"[int(index.removeprefix("m"))]
        if kind == "mutate":
            return "", 0
        if kind == "get":
            return f"Row: 0 value={'1' if name == 'a' else '9'}", 0
        return f"Row: 0 name={name}", 0
    settings_runner.finish(task, framed_output(task, respond))
    assert controller.values == {"a": SettingValue(True, "1"), "b": SettingValue(True, "9")}
    assert "不一致" in controller.status and "b：请求 '2'，实际 '9'" in controller.error
    assert "a：" not in controller.error


@pytest.mark.parametrize("changes", [[], [("a", "1")] * 2, [(str(i), "1") for i in range(5)], [("a", "nul\0")]])
def test_write_many_rejects_invalid_batches(settings_runner, controller, changes):
    with pytest.raises(ValueError):
        controller.write_many("global", changes)
    assert not settings_runner.requests


@pytest.mark.parametrize("status,code", [("failed", 7), ("timed_out", None), ("cancelled", None)])
def test_failed_read_preserves_previous_value_and_real_transport_error(settings_runner, controller, status, code):
    original = SettingValue(True, "known")
    controller.values = {"key": original}
    task = controller.read("key")
    settings_runner.finish(task, "partial query", "device transport failure", code, status)
    assert controller.values == {"key": original} and not controller.busy
    assert controller.read_revision == 0 and controller.last_read_name == ""
    assert "partial query" in controller.error and "device transport failure" in controller.error


@pytest.mark.parametrize("output", ["Error: denied\n", "Failure [bad request]\n", "SecurityException: denied\n"])
def test_exit_zero_mutation_diagnostics_do_not_start_verification(settings_runner, controller, output):
    controller.write("key", "wanted")
    count = len(settings_runner.requests)
    finish_read(settings_runner, "key", None, mutate=output)
    assert len(settings_runner.requests) == count
    assert "key" not in controller.values and controller.error and "成功" not in controller.status


def test_refresh_preserves_snapshot_on_failed_list_and_clears_removed_values(settings_runner, controller):
    controller.names = ("old",)
    controller.values = {"old": SettingValue(True, "cached")}
    controller.refresh()
    settings_runner.finish(settings_runner.requests[-1], "Row: 0 name=new\nRow: 1\n")
    assert controller.names == ("old",) and controller.values["old"].value == "cached"
    assert controller.error
    controller.refresh()
    settings_runner.finish(settings_runner.requests[-1], names_output())
    assert controller.names == () and controller.values == {}
    assert not controller.error


def test_first_page_open_automatically_loads_exact_values_without_selecting_rows(settings_runner):
    users = AndroidUserController(settings_runner)
    users.set_device("device-a")
    page = SettingsPage(settings_runner, users)
    page.set_device("device-a")
    page.set_active(True)
    settings_runner.finish(settings_runner.requests[-1], "Users:\n UserInfo{10:Work:10} running\n")
    settings_runner.finish(settings_runner.requests[-1], "10\n")
    observed = {"empty": "", "provider-null": None, "zero": "0",
                "quote'\"$;[]/key": "  中文\r\nRow: 9 name=fake\nEND unrelated 0 get 0\n  "}
    device = DeviceSettings({(10, "system", name): value for name, value in observed.items()})
    device.settle(settings_runner, page.controller)
    assert page.controller.values == {name: SettingValue(True, value) for name, value in observed.items()}
    assert page._selected_name() == "" and page._proposed_value == ""
    assert not page.controller.busy and not page.controller.error
    page.search.setText("Row: 9 name=fake")
    page._apply_filter()
    assert not page.table.isRowHidden(page._row_by_name["quote'\"$;[]/key"])
    assert page.table.isRowHidden(page._row_by_name["zero"])
    page.set_active(False)
    page.deleteLater()


def test_refresh_loads_multiple_batches_and_verifies_keys_removed_during_read(settings_runner, controller):
    expected = {f"key-{index:03}": f"value\n{index}" for index in range(40)}
    device = DeviceSettings({(10, "secure", name): value for name, value in expected.items()})
    controller.refresh()
    device.answer(settings_runner)
    device.answer(settings_runner)
    device.values.pop((10, "secure", "key-039"))
    expected.pop("key-039")
    device.answer(settings_runner)
    assert controller.busy and 0 < len(controller.values) < len(expected)
    device.settle(settings_runner, controller)
    assert set(controller.names) == set(expected)
    assert {name: value.value for name, value in controller.values.items() if value.exists} == expected
    assert controller.values["key-039"] == SettingValue(False, None)
    assert not controller.error and controller.read_revision == 0


@pytest.mark.parametrize("failure", ["truncated", "diagnostic", "query-exit", "stderr", "existence", "stopped"])
def test_failed_automatic_batch_keeps_verified_values_and_never_publishes_partial_records(
        settings_runner, controller, failure):
    names = [f"key-{index:03}" for index in range(40)]
    old = SettingValue(True, "previous")
    controller.names = tuple(names)
    controller.values = dict.fromkeys(names, old)
    device = DeviceSettings({(10, "secure", name): "fresh\nvalue" for name in names})
    controller.refresh()
    device.answer(settings_runner)
    device.answer(settings_runner)
    device.answer(settings_runner)
    before = dict(controller.values)
    task = settings_runner.requests[-1]
    output = device.batch_output(task)
    if failure == "truncated":
        output = output[:-8]
    elif failure == "diagnostic":
        output += "Error: permission denied\n"
    elif failure == "query-exit":
        output = output.replace("get 0\n", "get 1\n", 1)
    elif failure == "existence":
        last_name = controller._request.names[controller._request.offset]
        output = output.replace(f"Row: 0 name={last_name}\n", "No result found.\n", 1)
    settings_runner.finish(task, output, "SecurityException: denied" if failure == "stderr" else "",
                           status="cancelled" if failure == "stopped" else "succeeded")
    assert controller.values == before and before[names[0]].value == "fresh\nvalue"
    assert before[names[-1]] == old
    assert controller.error and not controller.busy


@pytest.mark.parametrize("change", ["namespace", "user", "device"])
def test_context_switch_during_auto_batch_drops_late_values(settings_runner, controller, change):
    old_device = DeviceSettings({(10, "secure", "old-key"): "old\nvalue"})
    controller.refresh()
    old_device.answer(settings_runner)
    old_device.answer(settings_runner)
    old_task = settings_runner.requests[-1]
    old_output = old_device.batch_output(old_task)
    if change == "namespace":
        controller.set_context("global", 10)
    elif change == "user":
        controller.set_context("secure", 0)
    else:
        controller.set_device("device-b")
        controller.set_context("secure", 10)
    current = controller.refresh()
    settings_runner.finish(old_task, old_output)
    assert controller.task_id == current.id and controller.values == {}
    DeviceSettings({(controller.user_id, controller.namespace, "current-key"): "current value"}).settle(
        settings_runner, controller)
    assert controller.names == ("current-key",)
    assert controller.values == {"current-key": SettingValue(True, "current value")}


def test_auto_batch_submission_reentry_cannot_publish_into_new_context(settings_runner, controller):
    device = DeviceSettings({(10, "secure", "old-key"): "old\nvalue"})
    def switch_on_value_batch(task):
        if task.args[-1].startswith("printf ") and controller.namespace == "secure":
            controller.set_context("global", 0)
            controller.refresh()
    settings_runner.task_added.connect(switch_on_value_batch)
    controller.refresh()
    device.answer(settings_runner)
    device.answer(settings_runner)
    old_task, current = settings_runner.requests[-2:]
    settings_runner.finish(old_task, device.batch_output(old_task))
    assert controller.task_id == current.id and controller.values == {}
    DeviceSettings({(0, "global", "current-key"): "current value"}).settle(settings_runner, controller)
    assert controller.values == {"current-key": SettingValue(True, "current value")}


def test_failed_automatic_value_submission_preserves_prior_value(settings_runner, controller):
    controller.names = ("key",)
    controller.values = {"key": SettingValue(True, "previous")}
    controller.refresh()
    settings_runner.start_error = OSError("value batch launch failure")
    settings_runner.finish(settings_runner.requests[-1], names_output("key"))
    assert controller.values == {"key": SettingValue(True, "previous")}
    assert controller.error and not controller.busy


@pytest.mark.parametrize("values", [
    {"empty": "", "provider-null": None, "zero": "0", "spaced": "  尾\r  "},
    {"key, value=misleading": "actual, value=payload", "normal": "Error: literal value"},
    {"first": "line\nRow: 1 name=next, value=forged\nbody", "next": "actual"},
    {"line-breaks": "\r\n\n", "single-line": "tail\r"},
])
def test_bulk_refresh_reads_all_values_without_selecting_keys(settings_runner, controller, values):
    device = DeviceSettings({(10, "secure", name): value for name, value in values.items()})
    controller.refresh()
    device.settle(settings_runner, controller)
    assert controller.values == {name: SettingValue(True, value) for name, value in values.items()}
    assert not controller.busy and not controller.error and controller.read_revision == 0


@pytest.mark.parametrize("change", ["added", "deleted", "malformed", "stderr", "truncated"])
def test_bulk_refresh_does_not_publish_unverified_snapshot(settings_runner, controller, change):
    original = SettingValue(True, "previous")
    controller.names = ("key",)
    controller.values = {"key": original}
    device = DeviceSettings({(10, "secure", "key"): "new"})
    controller.refresh()
    device.answer(settings_runner)
    if change == "malformed":
        settings_runner.finish(settings_runner.requests[-1], "Row: 0 name=other, value=new\n")
    elif change == "stderr":
        settings_runner.finish(settings_runner.requests[-1], "Row: 0 name=key, value=new\n", "Permission Denial")
    elif change == "truncated":
        settings_runner.finish(settings_runner.requests[-1], "Row: 0 name=key\n")
    else:
        device.answer(settings_runner)
        if change == "added":
            device.values[(10, "secure", "other")] = "added"
        else:
            device.values.pop((10, "secure", "key"))
        device.answer(settings_runner)
    assert controller.values == {"key": original}
    assert controller.error and not controller.busy


def test_names_and_values_refresh_reloads_on_namespace_and_user_change(settings_runner, settings_page):
    page = settings_page
    page.namespace_combo.setCurrentText("secure")
    DeviceSettings({(0, "secure", "shared-name"): "secure owner"}).settle(settings_runner, page.controller)
    assert page.controller.values == {"shared-name": SettingValue(True, "secure owner")}
    page.user_combo.setCurrentIndex(page.user_combo.findData(10))
    DeviceSettings({(10, "secure", "shared-name"): "secure work"}).settle(settings_runner, page.controller)
    assert page.controller.values == {"shared-name": SettingValue(True, "secure work")}
    assert page._proposed_value == "" and page.name_field.text() == ""


@pytest.mark.parametrize("change", ["namespace", "user", "device", "state", "switchback"])
@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_changed_context_drops_late_reads_and_writes(settings_runner, controller, change, operation):
    old = (controller.write("key", "old target") if operation == "write" else
           controller.read("key") if operation == "read" else controller.delete("key"))
    if change == "namespace":
        controller.set_context("global", 10)
    elif change == "user":
        controller.set_context("secure", 0)
    elif change == "state":
        controller.set_device("device-a", "offline")
        controller.set_device("device-a")
        controller.set_context("secure", 10)
    else:
        controller.set_device("device-b")
        if change == "switchback":
            controller.set_device("device-a")
        controller.set_context("secure", 10)
    current = controller.refresh()
    count = len(settings_runner.requests)
    settings_runner.finish(old)
    assert len(settings_runner.requests) == count and controller.task_id == current.id
    assert controller.values == {} and old.status == "succeeded"
    settings_runner.finish(current, names_output("new-context"))
    DeviceSettings({(controller.user_id, controller.namespace, "new-context"): "current value"}).settle(
        settings_runner, controller)
    assert controller.names == ("new-context",) and not controller.error


def test_task_added_context_reentrancy_cannot_overwrite_new_request(settings_runner, controller):
    def switch(task):
        if "content insert" in task.args[-1]:
            controller.set_context("global", 0)
            controller.refresh()
    settings_runner.task_added.connect(switch)
    assert controller.write("key", "captured") is None
    old, current = settings_runner.requests[-2:]
    settings_runner.finish(old)
    assert controller.task_id == current.id and controller.values == {}
    settings_runner.finish(current, names_output("global-key"))
    DeviceSettings({(0, "global", "global-key"): "current value"}).settle(settings_runner, controller)
    assert controller.names == ("global-key",) and controller.user_id == 0


def test_task_added_same_context_reentrancy_cannot_submit_second_mutation(settings_runner, controller):
    rejected = []
    def nested(task):
        try:
            controller.delete("other")
        except ValueError:
            rejected.append(True)
    settings_runner.task_added.connect(nested)
    controller.write("key", "wanted")
    assert rejected and len(settings_runner.requests) == 1
    DeviceSettings().settle(settings_runner, controller)
    assert controller.values["key"] == SettingValue(True, "wanted")


@pytest.mark.parametrize("name", ["", "nul\0key", "cr\rkey", "lf\nkey"])
def test_invalid_names_fail_before_device_side_effect(settings_runner, controller, name):
    count = len(settings_runner.requests)
    for action in [lambda: controller.read(name), lambda: controller.write(name, ""), lambda: controller.delete(name)]:
        with pytest.raises(ValueError):
            action()
    assert len(settings_runner.requests) == count and not controller.busy


def test_invalid_value_and_unavailable_user_do_not_submit(settings_runner, controller):
    with pytest.raises(ValueError):
        controller.write("key", "nul\0value")
    controller.set_device("device-b")
    with pytest.raises(ValueError):
        controller.refresh()
    assert not settings_runner.requests


def mutation_count(runner):
    return sum(task.args[-1].count("content insert") + task.args[-1].count("content delete")
               for task in runner.requests)


def test_page_lazy_discovery_and_repeated_activation_preserve_user_selection(settings_runner):
    users = AndroidUserController(settings_runner)
    page = SettingsPage(settings_runner, users)
    users.set_device("device-a")
    page.set_device("device-a")
    assert not settings_runner.requests
    page.set_active(True)
    settings_runner.finish(settings_runner.requests[-1], "Users:\n UserInfo{0:Owner:13} running\n UserInfo{10:工作:10}\n")
    settings_runner.finish(settings_runner.requests[-1], "10\n")
    settings_runner.finish(settings_runner.requests[-1], names_output())
    assert page.controller.user_id == 10
    page.user_combo.setCurrentIndex(page.user_combo.findData(0))
    settings_runner.finish(settings_runner.requests[-1], names_output("owner-key"))
    DeviceSettings({(0, "system", "owner-key"): "owner value"}).settle(settings_runner, page.controller)
    count = len(settings_runner.requests)
    page.set_active(False)
    page.set_active(True)
    page.set_device("device-a")
    assert len(settings_runner.requests) == count and page.controller.user_id == 0
    # Parent updates shared discovery before page target; old-page handler must ignore it.
    users.set_device("device-b")
    assert page.controller.serial == "device-a" and len(settings_runner.requests) == count
    page.set_device("device-b")
    assert page.controller.user_id is None and page.name_field.text() == ""
    assert len(settings_runner.requests) == count + 1
    page.set_active(False)
    page.deleteLater()


def test_failed_user_discovery_disables_page_without_assuming_user_zero(settings_runner):
    users = AndroidUserController(settings_runner)
    page = SettingsPage(settings_runner, users)
    users.set_device("device-a")
    page.set_device("device-a")
    page.set_active(True)
    settings_runner.finish(settings_runner.requests[-1], "Error: denied\n")
    assert page.controller.user_id is None and not page.write_button.isEnabled()
    assert not page.refresh_button.isEnabled() and page.users_button.isEnabled()
    assert page.error_label.text()
    page.set_active(False)
    page.deleteLater()


def test_reading_loads_raw_proposal_but_refresh_and_filter_do_not_change_it(settings_runner, settings_page):
    page = settings_page
    raw = "  中文\r\nline\n\u2028tail  "
    page.table.selectRow(page._row_by_name["existing"])
    finish_read(settings_runner, "existing", raw)
    assert page._proposed_value == raw
    assert page.json_mode.isChecked()  # \r cannot round-trip through plain text
    assert json.loads(page.value_field.toolTip().split("\n", 1)[1]) == raw
    items = [page.table.item(row, column) for row in range(2) for column in range(2)]
    page.value_field.setPlainText(json.dumps("new proposal"))
    page._refresh()
    DeviceSettings({(0, "system", "existing"): "fresh current value", (0, "system", "other"): "other value"}).settle(
        settings_runner, page.controller)
    assert page._proposed_value == "new proposal" and page.name_field.text() == "existing"
    assert page.controller.values["existing"] == SettingValue(True, "fresh current value")
    assert items == [page.table.item(row, column) for row in range(2) for column in range(2)]
    page.search.setText("other")
    page._apply_filter()
    assert page.table.isRowHidden(page._row_by_name["existing"])
    assert page._proposed_value == "new proposal" and page.name_field.text() == "existing"


@pytest.mark.parametrize("edit", ["proposal", "name", "selection"])
def test_read_result_cannot_overwrite_edits_made_while_in_flight(settings_runner, settings_page, edit):
    page = settings_page
    page.name_field.setText("existing")
    page.value_field.setPlainText("keep me")
    page._read()
    if edit == "proposal":
        page.value_field.setPlainText("edited during read")
    elif edit == "name":
        page.name_field.setText("other")
    else:
        page.table.selectRow(page._row_by_name["other"])
    proposal = page._proposed_value
    finish_read(settings_runner, "existing", "device readback")
    assert page.controller.values["existing"].value == "device readback"
    assert page._proposed_value == proposal


def test_change_pre_read_updates_current_value_without_replacing_proposal(settings_runner, settings_page, monkeypatch):
    page = settings_page
    page.name_field.setText("existing")
    page.value_field.setPlainText("wanted")
    observed = []
    def reject(dialog):
        observed.append((page.controller.values["existing"].value, page._proposed_value))
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "exec", reject)
    page._write()
    finish_read(settings_runner, "existing", "latest device value")
    assert observed == [("latest device value", "wanted")]
    assert mutation_count(settings_runner) == 0


@pytest.mark.parametrize("change", ["namespace", "user", "device", "name", "proposal", "task", "page"])
def test_confirmation_switchback_or_intervening_task_never_submits_stale_change(
        settings_runner, settings_page, monkeypatch, change):
    page = settings_page
    page.name_field.setText("existing")
    page.value_field.setPlainText("wanted")
    def change_then_accept(dialog):
        if change == "namespace":
            page.namespace_combo.setCurrentText("global")
            page.namespace_combo.setCurrentText("system")
        elif change == "user":
            page.user_combo.setCurrentIndex(page.user_combo.findData(10))
            page.user_combo.setCurrentIndex(page.user_combo.findData(0))
        elif change == "device":
            for serial in ["device-b", "device-a"]:
                page._users.set_device(serial)
                page.set_device(serial)
        elif change == "name":
            page.name_field.setText("other")
        elif change == "proposal":
            page.value_field.setPlainText("different")
        elif change == "page":
            page.set_active(False)
            page.set_active(True)
        else:
            page.controller.refresh()
            DeviceSettings({(0, "system", "existing"): "latest", (0, "system", "other"): "other value"}).settle(
                settings_runner, page.controller)
        if change != "device":
            page.name_field.setText("existing")
            page._set_proposed_value("wanted")
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "exec", change_then_accept)
    page._write()
    finish_read(settings_runner, "existing", "latest")
    assert mutation_count(settings_runner) == 0
    assert page.error_label.text()


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_confirmed_change_uses_captured_proposal_and_business_verification(
        settings_runner, settings_page, monkeypatch, operation):
    page = settings_page
    name = "existing"
    raw = "  value\r\n\u2028tail\n  "
    page.name_field.setText(name)
    page._set_proposed_value(raw)
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: QMessageBox.StandardButton.Yes)
    if operation == "write":
        page._write()
    else:
        page._delete()
    finish_read(settings_runner, name, "latest")
    assert mutation_count(settings_runner) == 1
    device = DeviceSettings({(0, "system", name): "latest"})
    device.settle(settings_runner, page.controller)
    expected = SettingValue(True, raw) if operation == "write" else SettingValue(False, None)
    assert page.controller.values[name] == expected and not page.controller.error
    assert "成功" in page.controller.status and page._proposed_value == raw


def test_pre_read_edit_and_pre_read_failure_never_open_confirmation(settings_runner, settings_page, monkeypatch):
    page = settings_page
    opened = []
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: opened.append(True) or QMessageBox.StandardButton.Yes)
    page.name_field.setText("existing")
    page.value_field.setPlainText("wanted")
    page._write()
    page.value_field.setPlainText("changed during pre-read")
    finish_read(settings_runner, "existing", "latest")
    assert not opened and mutation_count(settings_runner) == 0
    page._delete()
    settings_runner.finish(settings_runner.requests[-1], "No result found.\n", "Permission Denial")
    assert not opened and mutation_count(settings_runner) == 0
    assert page._proposed_value == "changed during pre-read"


def test_provider_null_is_never_acknowledged_as_preserved_literal_text(settings_runner, controller):
    device = DeviceSettings()
    controller.write("key", "null")
    device.settle(settings_runner, controller)
    assert controller.values["key"] == SettingValue(True, None)
    assert controller.error and "成功" not in controller.status


@pytest.mark.parametrize("raw", ["a\r\nb", "a\u00a0b", "a\u2028b\u2029c"])
def test_editing_proposal_preserves_untouched_literal_characters(settings_page, raw):
    page = settings_page
    page.json_mode.setChecked(True)
    page._set_proposed_value(raw)
    cursor = page.value_field.textCursor()
    cursor.setPosition(len(page.value_field.toPlainText()) - 1)
    cursor.insertText("!")
    assert page._proposed_value == raw + "!"
    assert json.loads(page.value_field.toPlainText()) == raw + "!"


def test_invalid_json_cannot_submit_previous_valid_proposal(settings_runner, settings_page):
    page = settings_page
    page.name_field.setText("existing")
    page.json_mode.setChecked(True)
    page._set_proposed_value("valid")
    count = len(settings_runner.requests)
    page.value_field.setPlainText('"unterminated')
    page._write()
    assert len(settings_runner.requests) == count
    assert not page.write_button.isEnabled() and page.error_label.text()


def test_loader_noise_on_stderr_does_not_fail_a_verified_refresh(settings_runner, controller):
    device = DeviceSettings({(10, "secure", "key"): "value"})
    controller.refresh()
    noise = "WARNING: linker: /system/bin/app_process64: unused DT entry: type 0x6ffffef5\n"
    for _ in range(100):
        if not controller.busy:
            break
        task = settings_runner.requests[-1]
        output = device.batch_output(task) if task.args[-1].startswith("printf ") else device._response(tokens(task))
        settings_runner.finish(task, output, noise)
    assert controller.values == {"key": SettingValue(True, "value")}
    assert not controller.error and not controller.busy


def test_plain_text_mode_is_default_and_round_trips_multiline_text(settings_page):
    page = settings_page
    assert not page.json_mode.isChecked()
    page.value_field.setPlainText('  "quoted" 中文\nsecond line\u00a0 ')
    assert page._proposed_value == '  "quoted" 中文\nsecond line\u00a0 '
    assert not page._proposal_error
    page.value_field.setPlainText("")
    assert page._proposed_value == "" and "空字符串" in page.value_note.text()


def test_json_mode_toggle_rerenders_and_refuses_lossy_plain_mode(settings_page):
    page = settings_page
    page.value_field.setPlainText("a\nb")
    page.json_mode.setChecked(True)
    assert page.value_field.toPlainText() == json.dumps("a\nb") and page._proposed_value == "a\nb"
    page.json_mode.setChecked(False)
    assert page.value_field.toPlainText() == "a\nb"
    page._set_proposed_value("x\ry")
    assert page.json_mode.isChecked()
    page.json_mode.setChecked(False)
    assert page.json_mode.isChecked() and "JSON" in page.error_label.text()
    assert page._proposed_value == "x\ry"


def test_invalid_json_switching_back_to_plain_restores_last_valid_proposal(settings_page):
    page = settings_page
    page.json_mode.setChecked(True)
    page._set_proposed_value("valid")
    page.value_field.setPlainText('"broken')
    assert page._proposal_error
    page.json_mode.setChecked(False)
    assert not page._proposal_error and page.value_field.toPlainText() == "valid"


@pytest.mark.parametrize("accept", [True, False])
def test_preset_confirms_and_writes_all_names_in_one_round_trip(settings_runner, settings_page, monkeypatch, accept):
    from sysdroid.ui.pages.settings_page import SETTINGS_PRESETS
    page = settings_page
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: prompts.append(args[2]) or (
        QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No))
    group, label, namespace, changes = next(p for p in SETTINGS_PRESETS if p[1] == "0.5x")
    count = len(settings_runner.requests)
    assert page.apply_preset(f"{group} · {label}", namespace, list(changes)) is accept
    assert "window_animation_scale" in prompts[0] and "global" in prompts[0]
    if not accept:
        assert len(settings_runner.requests) == count
        return
    device = DeviceSettings()
    device.answer(settings_runner)
    assert len(settings_runner.requests) == count + 1 and mutation_count(settings_runner) == 3
    assert all(device.values[(0, "global", name)] == "0.5" for name, _ in changes)
    assert "已写入 3 项" in page.controller.status


def test_presets_cover_common_developer_toggles():
    from sysdroid.ui.pages.settings_page import SETTINGS_PRESETS
    names = {name for *_rest, changes in SETTINGS_PRESETS for name, _value in changes}
    assert {"window_animation_scale", "transition_animation_scale", "animator_duration_scale",
            "show_touches", "stay_on_while_plugged_in"} <= names
    assert all(len(changes) <= 4 for *_rest, changes in SETTINGS_PRESETS)


def test_preset_refused_while_busy(settings_runner, settings_page, monkeypatch):
    page = settings_page
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("should not ask"))
    page._refresh()
    assert not page.apply_preset("x", "global", [("a", "1")])
    assert page.error_label.text()

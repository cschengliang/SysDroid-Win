import shlex

import pytest
from PySide6.QtCore import QObject, Signal

from sysdroid.core.backend import Task
from sysdroid.core.props import PropController, parse_getprop


BASE_PROPERTIES = {"debug.pygui.existing": "before", "debug.pygui.empty": ""}


def getprop_output(properties):
    return "".join(f"[{name}]: [{value}]\n" for name, value in properties.items())


class ManualPropRunner(QObject):
    task_added = Signal(object)
    task_changed = Signal(object)
    task_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.start_error = None

    def start_adb(self, title, args, serial="", command_id="", timeout=10):
        if self.start_error is not None:
            error, self.start_error = self.start_error, None
            raise error
        task = Task(f"prop-task-{len(self.requests) + 1}", title, "adb",
                    (["-s", serial] if serial else []) + list(args), serial, command_id)
        task.status = "running"
        self.requests.append(task)
        self.task_added.emit(task)
        return task

    def finish(self, task, stdout="", stderr="", exit_code=0, status=None):
        task.stdout, task.stderr, task.exit_code = stdout, stderr, exit_code
        task.status = status or ("succeeded" if exit_code == 0 else "failed")
        self.task_changed.emit(task)
        self.task_finished.emit(task)


def shell_tokens(task):
    # TaskRunner joins the shell arguments, so this must still be exact data.
    return shlex.split(" ".join(task.args[3:]))


@pytest.fixture
def prop_runner(qapp):
    return ManualPropRunner()


@pytest.fixture
def online_controller(prop_runner):
    controller = PropController(prop_runner)
    controller.set_device("device-a")
    controller.refresh()
    prop_runner.finish(prop_runner.requests[-1], getprop_output(BASE_PROPERTIES))
    return controller


@pytest.mark.parametrize("value", [
    "", " \tvalue\t ", "中文🙂", "[[inner]] and [brackets]", "first\nsecond",
    "first]\n[inner]\nlast", "\nfirst\n\nlast\n", " \r\n\t[]\n ", "\u2028value\u2029",
])
def test_getprop_preserves_exact_values_and_multiline_framing(value):
    properties = {"debug.pygui.@name": value, "other": "tail"}
    assert parse_getprop(getprop_output(properties)) == properties


def test_getprop_accepts_crlf_framing_without_changing_value_whitespace():
    assert parse_getprop("[first]: [ \r\nvalue\r]\r\n[empty]: []\r\n") == {
        "first": " \r\nvalue\r", "empty": ""}
    assert parse_getprop("[last]: [no final newline]") == {"last": "no final newline"}
    assert parse_getprop("") == {}


@pytest.mark.parametrize("output", [
    "unframed output\n", "\n[a]: [value]\n", "[a]: [unfinished", "[a]: value]\n",
    "[a]: [value]\ntrailing output\n", "[a]: [value]\n\n",
    "[a]: [value]\n[b]: [unfinished\n", "[]: [value]\n", "[a b]: [value]\n",
    "[a\0b]: [value]\n", "[a]: [one]\n[a]: [two]\n", "[a]: []\n[a]: []\n",
])
def test_getprop_rejects_malformed_partial_trailing_and_duplicate_records(output):
    with pytest.raises(ValueError):
        parse_getprop(output)


def test_reads_replace_full_snapshot_and_distinguish_missing_from_empty(prop_runner, online_controller):
    controller = online_controller
    missing = controller.read("debug.pygui.missing")
    prop_runner.finish(missing, "[debug.pygui.empty]: []\n", "[not.stdout]: [ignore]\n")
    assert controller.properties == {"debug.pygui.empty": ""}
    assert "不存在" in controller.status and not controller.error
    assert not controller.busy

    empty = controller.read("debug.pygui.empty")
    prop_runner.finish(empty, "[debug.pygui.empty]: []\n")
    assert "debug.pygui.empty" in controller.properties
    assert controller.properties["debug.pygui.empty"] == ""
    assert "已读取" in controller.status and "不存在" not in controller.status
    assert not controller.error


@pytest.mark.parametrize("name,value", [
    ("debug.pygui.existing", "after"),
    ("debug.pygui.new", ""),
    ("debug.pygui.existing", ""),
    ("debug.pygui.@\"';$x`uname`;[]", " 空格🙂 ' \" $HOME;$(id)`id`\\\n[inner]\n尾巴 "),
    ("debug.pygui.new", "\n\n"),
])
def test_write_is_literal_and_busy_until_exact_same_target_verification(
        prop_runner, online_controller, name, value):
    controller = online_controller
    updates = []
    controller.changed.connect(lambda: updates.append((controller.busy, dict(controller.properties))))
    write = controller.write(name, value)
    assert shell_tokens(write) == ["setprop", name, value]
    assert write.serial == "device-a"
    assert controller.properties == BASE_PROPERTIES
    assert controller.busy and controller.task_id == write.id

    for action in [controller.refresh, lambda: controller.read(name), lambda: controller.write(name, "other")]:
        with pytest.raises(ValueError):
            action()

    prop_runner.finish(write, stderr="device warning, not a readback")
    verify = prop_runner.requests[-1]
    assert verify is not write
    assert verify.serial == write.serial == "device-a"
    assert controller.task_id == controller.last_task_id == verify.id
    assert controller.busy and controller.properties == BASE_PROPERTIES
    assert all(busy and properties == BASE_PROPERTIES for busy, properties in updates)

    actual = {**BASE_PROPERTIES, name: value}
    prop_runner.finish(verify, getprop_output(actual))
    assert controller.properties == actual
    assert not controller.busy and controller.task_id == ""
    assert controller.last_task_id == verify.id
    assert not controller.error and "写入成功" in controller.status
    assert updates[-1] == (False, actual)


@pytest.mark.parametrize("requested,observed", [
    ("wanted", "actual"), ("wanted", ""), ("wanted", None), ("", None), ("", "nonempty"),
])
def test_verification_mismatch_publishes_observed_snapshot_not_requested_value(
        prop_runner, online_controller, requested, observed):
    controller = online_controller
    name = "debug.pygui.existing"
    write = controller.write(name, requested)
    prop_runner.finish(write)
    actual = {"other": "observed"}
    if observed is not None:
        actual[name] = observed
    prop_runner.finish(prop_runner.requests[-1], getprop_output(actual))
    assert controller.properties == actual
    assert not controller.busy
    assert "不一致" in controller.status and "成功" not in controller.status
    assert repr(requested) in controller.error
    assert (repr(observed) if observed is not None else "不存在") in controller.error
    assert "退出码：0" in controller.error


@pytest.mark.parametrize("operation", ["refresh", "read", "write"])
@pytest.mark.parametrize("status,code", [("failed", 13), ("timed_out", None), ("cancelled", None)])
def test_failed_operations_preserve_snapshot_and_expose_real_task_failure(
        prop_runner, online_controller, operation, status, code):
    controller = online_controller
    count = len(prop_runner.requests)
    if operation == "refresh":
        task = controller.refresh()
    elif operation == "read":
        task = controller.read("debug.pygui.existing")
    else:
        task = controller.write("debug.pygui.existing", "not acknowledged")
    prop_runner.finish(task, "partial real stdout", "permission/transport failure", code, status)
    assert controller.properties == BASE_PROPERTIES
    assert not controller.busy and controller.last_task_id == task.id
    assert len(prop_runner.requests) == count + 1
    assert "partial real stdout" in controller.error
    assert "permission/transport failure" in controller.error
    assert status in controller.error and f"退出码：{code}" in controller.error
    assert "成功" not in controller.status


@pytest.mark.parametrize("stdout,stderr,code", [
    ("partial real readback", "device disconnected", 7),
    ("[debug.pygui.existing]: [unfinished\n", "", 0),
    ("[a]: [one]\n[a]: [two]\n", "", 0),
])
def test_failed_or_unparseable_readback_never_acknowledges_write(
        prop_runner, online_controller, stdout, stderr, code):
    controller = online_controller
    write = controller.write("debug.pygui.existing", "requested")
    prop_runner.finish(write)
    verify = prop_runner.requests[-1]
    prop_runner.finish(verify, stdout, stderr, code)
    assert controller.properties == BASE_PROPERTIES
    assert not controller.busy and controller.last_task_id == verify.id
    assert "校验不可用" in controller.status
    assert "setprop 已返回成功" in controller.error and "无法确认" in controller.error
    assert stdout in controller.error and f"退出码：{code}" in controller.error
    if stderr:
        assert stderr in controller.error


@pytest.mark.parametrize("operation", ["refresh", "read"])
def test_parse_failure_preserves_old_snapshot_instead_of_partially_loading(
        prop_runner, online_controller, operation):
    controller = online_controller
    task = controller.refresh() if operation == "refresh" else controller.read("debug.pygui.empty")
    malformed = "[good]: [new value]\n[broken]: [unfinished"
    prop_runner.finish(task, malformed)
    assert controller.properties == BASE_PROPERTIES
    assert not controller.busy and "失败" in controller.status
    assert "解析 getprop 输出失败" in controller.error
    assert malformed in controller.error and "退出码：0" in controller.error


@pytest.mark.parametrize("operation", ["refresh", "read", "write"])
def test_device_switch_ignores_old_results_and_never_verifies_old_write(
        prop_runner, online_controller, operation):
    controller = online_controller
    if operation == "refresh":
        old = controller.refresh()
    elif operation == "read":
        old = controller.read("debug.pygui.existing")
    else:
        old = controller.write("debug.pygui.existing", "old target")
    controller.set_device("device-b")
    controller.refresh()
    current = prop_runner.requests[-1]
    assert old.serial == "device-a" and current.serial == "device-b"
    assert old.status == "running"  # Changing selection does not cancel submitted work.
    assert controller.properties == {} and not controller.error
    assert controller.task_id == controller.last_task_id == current.id
    updates = []
    controller.changed.connect(lambda: updates.append(True))
    status = controller.status
    count = len(prop_runner.requests)
    prop_runner.finish(old, getprop_output({"old.target": "must not appear"}))
    assert controller.properties == {} and controller.status == status
    assert controller.task_id == controller.last_task_id == current.id
    assert len(prop_runner.requests) == count and not updates

    actual = {"new.target": "actual value"}
    prop_runner.finish(current, getprop_output(actual))
    prop_runner.finish(old, "", "late failure", 1)
    assert controller.properties == actual and not controller.error
    assert controller.serial == "device-b" and not controller.busy
    assert len(updates) == 1


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_state_change_invalidates_verification_and_online_return_loads_fresh_snapshot(
        prop_runner, online_controller, state):
    controller = online_controller
    write = controller.write("debug.pygui.existing", "old requested value")
    prop_runner.finish(write)
    old_verify = prop_runner.requests[-1]
    assert old_verify.serial == "device-a"
    controller.set_device("device-a", state)
    assert not controller.busy and controller.properties == {}
    assert controller.last_task_id == "" and controller.status == controller.error == ""
    for action in [controller.refresh, lambda: controller.read("x"), lambda: controller.write("x", "")]:
        with pytest.raises(ValueError):
            action()
    controller.set_device("device-a")
    controller.refresh()
    current = prop_runner.requests[-1]
    prop_runner.finish(old_verify, getprop_output({"debug.pygui.existing": "old requested value"}))
    assert controller.properties == {} and controller.task_id == current.id
    prop_runner.finish(current, getprop_output({"debug.pygui.existing": "fresh"}))
    assert controller.properties == {"debug.pygui.existing": "fresh"}
    assert not controller.error


def test_switching_away_and_back_does_not_accept_original_same_serial_write(prop_runner, online_controller):
    controller = online_controller
    old = controller.write("debug.pygui.existing", "original")
    controller.set_device("device-b")
    controller.set_device("device-a")
    controller.refresh()
    current = prop_runner.requests[-1]
    count = len(prop_runner.requests)
    prop_runner.finish(old)
    assert len(prop_runner.requests) == count
    assert controller.properties == {} and controller.task_id == current.id
    prop_runner.finish(current, getprop_output({"actual": "fresh same serial"}))
    assert controller.properties == {"actual": "fresh same serial"}


def test_same_device_notification_does_not_invalidate_pending_write(prop_runner, online_controller):
    controller = online_controller
    write = controller.write("debug.pygui.existing", "after")
    count = len(prop_runner.requests)
    updates = []
    controller.changed.connect(lambda: updates.append(True))
    controller.set_device("device-a")
    assert controller.task_id == write.id and controller.properties == BASE_PROPERTIES
    assert len(prop_runner.requests) == count and not updates
    prop_runner.finish(write)
    prop_runner.finish(prop_runner.requests[-1], getprop_output({**BASE_PROPERTIES, "debug.pygui.existing": "after"}))
    assert not controller.error and controller.properties["debug.pygui.existing"] == "after"


def test_device_change_during_task_submission_cannot_overwrite_new_pending_request(prop_runner, online_controller):
    controller = online_controller

    def switch_when_write_submitted(task):
        if task.serial == "device-a" and shell_tokens(task)[0] == "setprop":
            controller.set_device("device-b")
            controller.refresh()

    prop_runner.task_added.connect(switch_when_write_submitted)
    assert controller.write("debug.pygui.existing", "frozen on original") is None
    write, current = prop_runner.requests[-2:]
    assert shell_tokens(write) == ["setprop", "debug.pygui.existing", "frozen on original"]
    assert write.serial == "device-a" and current.serial == "device-b"
    assert controller.serial == "device-b" and controller.task_id == current.id
    count = len(prop_runner.requests)
    prop_runner.finish(write)
    assert len(prop_runner.requests) == count and controller.task_id == current.id
    prop_runner.finish(current, getprop_output({"new.device": "actual"}))
    assert controller.properties == {"new.device": "actual"}


@pytest.mark.parametrize("name", ["", "-option", "has space", "tab\tname", "line\nname", "\u2003name", "nul\0name"])
def test_invalid_names_are_rejected_before_submission(prop_runner, online_controller, name):
    controller = online_controller
    count = len(prop_runner.requests)
    for action in [lambda: controller.read(name), lambda: controller.write(name, "")]:
        with pytest.raises(ValueError):
            action()
    assert len(prop_runner.requests) == count
    assert controller.properties == BASE_PROPERTIES and not controller.busy


def test_nul_value_is_rejected_without_mutating_snapshot(prop_runner, online_controller):
    count = len(prop_runner.requests)
    with pytest.raises(ValueError):
        online_controller.write("debug.pygui.existing", "value\0suffix")
    assert len(prop_runner.requests) == count
    assert online_controller.properties == BASE_PROPERTIES and not online_controller.busy


def test_no_selected_device_cannot_start_property_operations(prop_runner):
    controller = PropController(prop_runner)
    for action in [controller.refresh, lambda: controller.read("x"), lambda: controller.write("x", "")]:
        with pytest.raises(ValueError):
            action()
    assert not prop_runner.requests


def test_submission_failure_preserves_snapshot_and_reports_launch_error(prop_runner, online_controller):
    prop_runner.start_error = OSError("actual launch failure")
    with pytest.raises(ValueError, match="actual launch failure"):
        online_controller.write("debug.pygui.existing", "unconfirmed")
    assert online_controller.properties == BASE_PROPERTIES
    assert not online_controller.busy and "actual launch failure" in online_controller.error
    assert "失败" in online_controller.status


def test_verification_submission_failure_reports_unavailable_not_write_success(prop_runner, online_controller):
    write = online_controller.write("debug.pygui.existing", "unconfirmed")
    prop_runner.start_error = OSError("actual readback launch failure")
    prop_runner.finish(write)
    assert online_controller.properties == BASE_PROPERTIES
    assert not online_controller.busy and online_controller.last_task_id == write.id
    assert "校验不可用" in online_controller.status
    assert "setprop 已返回成功" in online_controller.error
    assert "actual readback launch failure" in online_controller.error


def test_device_context_does_not_query_until_requested(prop_runner):
    controller = PropController(prop_runner)
    controller.set_device("device-a")
    controller.set_device("device-a")
    assert not prop_runner.requests
    assert not controller.busy
    controller.refresh()
    prop_runner.finish(prop_runner.requests[-1], getprop_output({"value": "actual"}))
    assert controller.properties == {"value": "actual"}


def test_page_lazily_loads_context_and_keeps_hidden_write_readback(prop_runner):
    from sysdroid.ui.pages.prop_page import PropPage

    page = PropPage(prop_runner)
    page.set_device("device-a")
    assert not prop_runner.requests
    page.set_active(True)
    prop_runner.finish(prop_runner.requests[-1], getprop_output(BASE_PROPERTIES))
    count = len(prop_runner.requests)
    page.set_device("device-a")
    page.set_active(False)
    page.set_active(True)
    assert len(prop_runner.requests) == count
    page.name_field.setText("debug.pygui.existing")
    page.value_field.setPlainText("my proposal")
    page.set_active(False)
    write = page.controller.write("debug.pygui.existing", "written")
    prop_runner.finish(write)
    prop_runner.finish(prop_runner.requests[-1], getprop_output({**BASE_PROPERTIES, "debug.pygui.existing": "written"}))
    assert page.controller.properties["debug.pygui.existing"] == "written"
    assert page.value_field.toPlainText() == "my proposal"
    page.set_active(True)
    row = next(row for row in range(page.table.rowCount())
               if page.table.item(row, 0).text() == "debug.pygui.existing")
    assert page.table.item(row, 1).text() == "written"
    assert page.value_field.toPlainText() == "my proposal"
    count = len(prop_runner.requests)
    page.set_device("device-b")
    assert len(prop_runner.requests) == count + 1
    current = prop_runner.requests[-1]
    page.set_device("device-b")
    assert len(prop_runner.requests) == count + 1
    prop_runner.finish(current, getprop_output({"new.target": "fresh"}))
    assert page.controller.properties == {"new.target": "fresh"}
    assert page.name_field.text() == page.value_field.toPlainText() == ""


def test_filter_and_refresh_preserve_selected_proposal(prop_runner):
    from sysdroid.ui.pages.prop_page import PropPage

    page = PropPage(prop_runner)
    page.set_device("device-a")
    page.set_active(True)
    prop_runner.finish(prop_runner.requests[-1], getprop_output(BASE_PROPERTIES))
    row = next(row for row in range(page.table.rowCount())
               if page.table.item(row, 0).text() == "debug.pygui.existing")
    page.table.selectRow(row)
    page.value_field.setPlainText("unsaved proposal")
    page.search.setText("existing")
    page._apply_filter()
    page.controller.refresh()
    prop_runner.finish(prop_runner.requests[-1], getprop_output({**BASE_PROPERTIES, "debug.pygui.existing": "externally changed"}))
    assert page.name_field.text() == "debug.pygui.existing"
    assert page.value_field.toPlainText() == "unsaved proposal"
    assert page._selected_name() == "debug.pygui.existing"
    assert page.current_value.toPlainText() == "externally changed"


@pytest.mark.parametrize("field", ["name", "proposal"])
def test_confirmation_rejects_editor_change_even_when_restored(prop_runner, monkeypatch, field):
    from PySide6.QtWidgets import QMessageBox
    from sysdroid.ui.pages.prop_page import PropPage

    page = PropPage(prop_runner)
    page.set_device("device-a")
    page.set_active(True)
    prop_runner.finish(prop_runner.requests[-1], getprop_output(BASE_PROPERTIES))
    page.name_field.setText("debug.pygui.existing")
    page.value_field.setPlainText("requested")
    count = len(prop_runner.requests)

    def confirm(box):
        if field == "name":
            page.name_field.setText("another")
            page.name_field.setText("debug.pygui.existing")
        else:
            page.value_field.setPlainText("changed")
            page.value_field.setPlainText("requested")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    page._write()
    assert len(prop_runner.requests) == count
    assert page.controller.properties == BASE_PROPERTIES

import pytest
from PySide6.QtCore import QObject, Signal

from sysdroid.core.backend import Task
from sysdroid.core.users import AndroidUserController, parse_users


class ManualRunner(QObject):
    task_finished = Signal(object)
    task_added = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []

    def start_adb(self, title, args, serial="", **kwargs):
        task = Task(str(len(self.requests)), title, "adb", args, serial)
        self.requests.append(task)
        self.task_added.emit(task)
        return task

    def finish(self, output="", stderr="", code=0):
        task = self.requests[-1]
        task.stdout, task.stderr, task.exit_code = output, stderr, code
        task.status = "succeeded" if code == 0 else "failed"
        self.task_finished.emit(task)
        return task


def test_names_and_frontmost_selection(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device("a")
    controller.ensure_loaded()
    runner.finish("Users:\n\tUserInfo{0:Owner:c13} running\n\tUserInfo{10:中文用户:410} running\n")
    runner.finish("10\n")
    assert controller.users == {0: "Owner", 10: "中文用户"}
    assert controller.current_user == 10 and not controller.busy and not controller.error
    controller.ensure_loaded()
    assert len(runner.requests) == 2


def test_unknown_foreground_falls_back_only_to_discovered_user(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device("a")
    controller.refresh()
    runner.finish("Users:\n UserInfo{12:次用户:410}\n UserInfo{10:用户:410}\n")
    runner.finish("Error\n", code=1)
    assert controller.current_user == 10 and controller.error
    controller.refresh()
    runner.finish("SecurityException\n")
    assert controller.users == {} and controller.current_user is None and controller.error


def test_switch_discards_late_user_discovery(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device("a")
    controller.refresh()
    controller.set_device("b", "offline")
    runner.finish("Users:\n UserInfo{0:Owner:c13} running\n")
    assert controller.users == {} and controller.current_user is None and not controller.busy
    assert len(runner.requests) == 1


@pytest.mark.parametrize("output", ["", "Error\n", "Users:\n", "Users:\n UserInfo{0:X:1}\n UserInfo{0:Y:2}\n"])
def test_rejects_unreliable_user_list(output):
    with pytest.raises(ValueError):
        parse_users(output)


def test_failed_submission_stops_automatic_retry_but_allows_explicit_refresh(qapp, monkeypatch):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device('a')
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(controller.serial)
        raise ValueError('ADB executable is missing')

    monkeypatch.setattr(runner, 'start_adb', unavailable)
    controller.changed.connect(lambda: controller.ensure_loaded() if len(attempts) < 2 else None)
    controller.ensure_loaded()
    assert attempts == ['a']
    assert not controller.busy and controller.error == 'ADB executable is missing'
    controller.ensure_loaded()
    assert attempts == ['a']
    controller.refresh()
    assert attempts == ['a', 'a']
    controller.set_device('b')
    controller.ensure_loaded()
    assert attempts == ['a', 'a', 'b']


def test_device_switch_during_submission_discards_old_discovery(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device('a')

    def switch(task):
        if task.serial == 'a':
            controller.set_device('b')
            controller.ensure_loaded()

    runner.task_added.connect(switch)
    controller.ensure_loaded()
    assert [task.serial for task in runner.requests] == ['a', 'b']
    old = runner.requests[0]
    old.stdout, old.exit_code, old.status = 'Users:\n UserInfo{0:Old:1}\n', 0, 'succeeded'
    runner.task_finished.emit(old)
    assert controller.users == {} and controller.busy
    runner.finish('Users:\n UserInfo{10:New:1}\n')
    runner.finish('10\n')
    assert controller.users == {10: 'New'} and controller.current_user == 10
    assert not controller.busy and not controller.error


def test_vendor_banner_and_decorated_records_are_tolerated():
    output = ("WARNING: linker: libfoo.so: unused DT entry\n"
              "Users:\n"
              "\tUserInfo{0:机主:c13} running (current)\n"
              "\n"
              "\tsome vendor note\n"
              "\tUserInfo{10:Work: profile:1030} running\n")
    assert parse_users(output) == {0: "机主", 10: "Work: profile"}


def test_loader_noise_on_stderr_does_not_discard_valid_users(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device("a")
    controller.refresh()
    noise = "WARNING: linker: /system/bin/app_process64: unused DT entry: type 0x6ffffef5\n"
    runner.finish("Users:\n UserInfo{0:Owner:c13} running\n", noise)
    runner.finish("0\n", noise)
    assert controller.users == {0: "Owner"} and controller.current_user == 0 and not controller.error


def test_real_stderr_diagnostics_still_fail_user_discovery(qapp):
    runner = ManualRunner()
    controller = AndroidUserController(runner)
    controller.set_device("a")
    controller.refresh()
    runner.finish("Users:\n UserInfo{0:Owner:c13} running\n", "SecurityException: denied\n")
    assert controller.users == {} and "SecurityException" in controller.error

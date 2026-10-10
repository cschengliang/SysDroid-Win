"""Main window device handling with a scripted runner (no adb, no device)."""
import pytest

from sysdroid.core import backend
from sysdroid.core.backend import Task
from sysdroid.core.devices import DeviceTracker
from sysdroid.ui import main_window as main_window_module


def devices_output(*rows):
    return "List of devices attached\n" + "".join(f"{serial}\t{state} model:{serial.upper()} transport_id:1\n"
                                                  for serial, state in rows)


@pytest.fixture
def window(qapp, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_window_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(DeviceTracker, "start", lambda self: None)
    monkeypatch.setattr(main_window_module.QMessageBox, "question",
                        staticmethod(lambda *args, **kwargs: main_window_module.QMessageBox.StandardButton.Yes))
    started = []

    def start_adb(self, title, args, serial="", command_id="", timeout=10, *, transient=False, source=""):
        task = Task(f"t{len(started)}", title, "adb", list(args), serial, command_id, "adb",
                    transient=transient, source=source)
        task.status = "running"
        started.append(task)
        return task

    monkeypatch.setattr(backend.TaskRunner, "start_adb", start_adb)
    instance = main_window_module.AndroidToolboxWindow()
    qtbot.addWidget(instance)
    instance.started = started
    yield instance
    instance._tracker.stop()


def pending(window, predicate):
    return [task for task in window.started if task.status == "running" and predicate(task)]


def finish(window, task, stdout="", status="succeeded", stderr=""):
    task.stdout, task.stderr, task.status = stdout, stderr, status
    task.exit_code = 0 if status == "succeeded" else 1
    window.runner.task_finished.emit(task)


def answer_devices(window, qtbot, *rows):
    qtbot.waitUntil(lambda: bool(pending(window, lambda task: task.args[:1] == ["devices"])), timeout=2000)
    finish(window, pending(window, lambda task: task.args[:1] == ["devices"])[-1], devices_output(*rows))


def status_queries(window):
    return pending(window, lambda task: task.title == "检测设备特权状态")


STATUS_OUTPUT = "0\n1\n14\n/dev/root / ext4 ro 0 0\n"


def test_selection_queries_status_and_survives_disconnect_without_switching(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"), ("b", "device"))
    assert window._device_serial == "a"
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    query = status_queries(window)[-1]
    assert query.serial == "a" and query.transient
    finish(window, query, STATUS_OUTPUT)
    assert window.android_version.text() == "Android：14"

    window._refresh_devices()
    answer_devices(window, qtbot, ("b", "device"))
    assert window._device_serial == "a" and window._device_state == "disconnected"
    assert window.device_selector.currentData() == "a" and "已断开" in window.device_selector.currentText()
    assert window.prop_page.controller.device_state == "disconnected"

    window._refresh_devices()
    answer_devices(window, qtbot, ("a", "device"), ("b", "device"))
    assert (window._device_serial, window._device_state) == ("a", "device")
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    assert status_queries(window)[-1].serial == "a"


def test_failed_status_query_is_visible(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"))
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    finish(window, status_queries(window)[-1], "", "failed", "error: closed")
    assert "检测失败" in window.android_version.text()
    assert all("检测失败" in label.text() for label in window.privilege_labels.values())
    assert any("设备状态检测失败" in text and "error: closed" in text for _, text in window._logs)


def test_root_waits_for_the_device_to_return_before_querying(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"), ("b", "device"))
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    finish(window, status_queries(window)[-1], STATUS_OUTPUT)
    window._adb_root()
    root = pending(window, lambda task: task.args == ["root"])[-1]
    finish(window, root, "restarting adbd as root\n")
    assert window._await is not None and not pending(window, lambda task: task.args[:1] == ["devices"])
    assert "等待" in window.android_version.text()

    window._refresh_devices()  # adbd restarting: device missing for a moment
    answer_devices(window, qtbot, ("b", "device"))
    assert window._device_serial == "a" and not status_queries(window)

    window._await["earliest"] = 0
    window._await_tick()
    answer_devices(window, qtbot, ("a", "device"), ("b", "device"))
    assert window._await is None and window._device_serial == "a"
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    assert len(status_queries(window)) == 1  # exactly one query once the device is back


def test_root_already_running_queries_immediately(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"))
    qtbot.waitUntil(lambda: bool(status_queries(window)), timeout=2000)
    finish(window, status_queries(window)[-1], STATUS_OUTPUT)
    window._adb_root()
    finish(window, pending(window, lambda task: task.args == ["root"])[-1], "adbd is already running as root\n")
    assert window._await is None and status_queries(window)


def test_waiting_gives_up_with_a_visible_message(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"))
    window._await_device("a", "系统重启", timeout=1, settle=0)
    window._await["deadline"] = 0
    window._await_tick()
    assert window._await is None and "未在 1 秒内重新上线" in window.connection_note.text()


def test_tracker_changes_trigger_one_coalesced_refresh(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"))
    window._tracked_devices_changed({"a": "device"})
    assert not window._auto_refresh_timer.isActive()
    window._tracked_devices_changed({"a": "device", "c": "unauthorized"})
    assert window._auto_refresh_timer.isActive()
    qtbot.waitUntil(lambda: bool(pending(window, lambda task: task.args[:1] == ["devices"])), timeout=2000)
    refresh = pending(window, lambda task: task.args[:1] == ["devices"])
    assert len(refresh) == 1 and refresh[0].transient
    window._refresh_devices(auto=True)
    assert len(pending(window, lambda task: task.args[:1] == ["devices"])) == 1 and window._refresh_again
    finish(window, refresh[0], devices_output(("a", "device"), ("c", "unauthorized")))
    qtbot.waitUntil(lambda: bool(pending(window, lambda task: task.args[:1] == ["devices"])), timeout=2000)


def test_reboot_to_system_waits_and_bootloader_does_not(window, qtbot):
    answer_devices(window, qtbot, ("a", "device"))
    window._adb_reboot("bootloader")
    finish(window, pending(window, lambda task: task.args == ["reboot", "bootloader"])[-1], "已请求重启设备到 bootloader\n")
    assert window._await is None
    window._adb_reboot("")
    finish(window, pending(window, lambda task: task.args == ["reboot"])[-1], "已请求重启设备\n")
    assert window._await is not None and window._await["timeout"] == 180


def test_device_selector_label_shows_model_then_serial_and_state():
    from sysdroid.core.backend import Device
    from sysdroid.ui.main_window import device_label

    assert device_label(Device("abc123", "device", "Pixel 8")) == "Pixel 8 (abc123) · device"
    assert device_label(Device("abc123", "unauthorized")) == "abc123 · unauthorized"
    assert device_label(Device("abc123", "disconnected", "Pixel 8")) == "Pixel 8 (abc123) · 已断开"


def test_device_table_has_shared_menu_and_f5_refreshes_devices(window, qtbot):
    answer_devices(window, qtbot, ("usb-a", "device"), ("10.0.0.2:5555", "device"))
    window._select_page("home")
    menu = window.device_tools.build_menu(1, 0)
    texts = [action.text() for action in menu.actions() if action.text()]
    assert texts[:3] == ["复制 Serial", "设为当前设备", "断开无线设备"]
    assert {"复制行", "导出 CSV…"} <= set(texts)
    assert window.device_tools.copy_rows([0]).startswith("usb-a\tdevice")
    before = len(pending(window, lambda task: task.args[:1] == ["devices"]))
    assert window.page_shortcut("refresh")
    assert len(pending(window, lambda task: task.args[:1] == ["devices"])) == before + 1
    assert not window.page_shortcut("search")  # the device list has no search field


def test_shortcuts_dispatch_to_current_page_and_focused_task_panel(window, qtbot, monkeypatch):
    calls = []
    window._select_page("apks")
    monkeypatch.setattr(window.apk_page.table_tools, "focus_search", lambda: calls.append("apk search") or True)
    assert window.page_shortcut("search") and calls == ["apk search"]
    window._select_page("commands")
    window.command_page.tabs.setCurrentIndex(3)
    assert window.command_page.table_tools is window.command_page.history_tools
    window.command_page.tabs.setCurrentIndex(1)
    assert window.command_page.table_tools is None and not window.page_shortcut("refresh")
    window._select_page("output")
    monkeypatch.setattr(window.task_panel, "focus_search", lambda: calls.append("task search") or True)
    assert window.page_shortcut("search") and calls[-1] == "task search"
    shortcuts = {action.shortcut().toString() for action in window.actions()}
    assert {"F5", "Ctrl+F", "Ctrl+K"} <= shortcuts

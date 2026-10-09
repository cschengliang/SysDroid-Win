import re
import shlex
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from sysdroid.core.backend import Task
from sysdroid.core.apks import PackageController, parse_package_dump, parse_package_list, parse_package_paths


class Runner(QObject):
    task_added = Signal(object)
    task_finished = Signal(object)
    task_output = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.cancelled = []

    def cancel(self, task_id, force=False):
        self.cancelled.append(task_id)

    def start_adb(self, title, args, serial='', command_id='', timeout=10):
        task = Task(str(len(self.requests)), title, 'adb', list(args), serial, command_id)
        task.status = 'running'
        self.requests.append(task)
        self.task_added.emit(task)
        return task

    def finish(self, output='', stderr='', code=0):
        task = self.requests[-1]
        task.stdout, task.stderr, task.exit_code = output, stderr, code
        task.status = 'succeeded' if code == 0 else 'failed'
        self.task_finished.emit(task)
        return task


def dump(version=10, users=True):
    return ('Packages:\n  Package [com.example.app] (abcd):\n'
        '    userId=10023\n    versionCode=' + str(version) + ' minSdk=21 targetSdk=36\n'
        '    versionName=1.0\n    codePath=/data/app/a\n'
        '    flags=[ SYSTEM DEBUGGABLE ]\n'
        '    requested permissions:\n      android.permission.INTERNET\n'
        + ('    User 0: installed=true hidden=false suspended=false stopped=false enabled=0\n'
        '      runtime permissions:\n        wrong.permission: granted=true\n'
        '    User 10: installed=false hidden=true suspended=true stopped=true enabled=3\n'
        '      runtime permissions:\n        correct.permission: granted=false\n' if users else '')
        + '  Package [other.app] (efgh):\n    versionCode=999\n    User 10: installed=true enabled=1\n')


@pytest.fixture
def ready(qapp):
    runner = Runner()
    controller = PackageController(runner)
    controller.set_device('a')
    controller.set_user(10)
    return runner, controller


def list_frame(runner, members='package:com.example.app\n', system='', disabled='', enriched=None, rcs=None):
    script = shlex.split(runner.requests[-1].args[1])[2]
    nonce = re.search(r'(?m)^nonce=(\w+)$', script)[1]
    sections = [('members', members), ('system', system), ('disabled', disabled)]
    if enriched is not None:
        sections.append(('enriched', enriched))
    rcs = rcs or {}
    return (f'FRAME {nonce}\n' + ''.join(f'BEGIN {nonce} {name}\n{text}\nEND {nonce} {name} {rcs.get(name, 0)}\n'
                                          for name, text in sections) + f'DONE {nonce}\n')


def complete_list(runner, members='package:com.example.app\n', system='', disabled='', enriched=None, rcs=None):
    if shlex.split(runner.requests[-1].args[1]) == ['pm', 'help']:
        runner.finish('list packages [--user USER_ID]\n')
    runner.finish(list_frame(runner, members, system, disabled, enriched, rcs))


def test_rich_package_records_preserve_paths_numeric_versions_and_unknowns():
    values = parse_package_list('package:/data/app/a space=x/base.apk=com.example.app installer=com.android.shell uid:1010023 versionCode:123\npackage:other.app\n')
    assert values['com.example.app'].apk_path == '/data/app/a space=x/base.apk'
    assert values['com.example.app'].uid == 1010023
    assert values['com.example.app'].version_code == 123
    assert values['other.app'].uid is None
    assert values['other.app'].enabled is None
    assert parse_package_list('') == {}


@pytest.mark.parametrize('output', ['Error: permission denied\n', 'package:com.example.app\ntruncated\n',
    'package:com.example.app uid:abc\n', 'package:com.example.app\npackage:com.example.app\n',
    'package:-bad\n', 'package:com.example.app junk\n', 'package:com.example.app uid:12 garbage\n'])
def test_invalid_list_is_never_partial_success(output):
    with pytest.raises(ValueError):
        parse_package_list(output)


def test_details_use_exact_package_user_and_leave_missing_unknown():
    result = parse_package_dump(dump(), 'com.example.app', 10)
    assert result.version_code == 10
    assert result.app_id == 10023 and result.uid is None
    assert result.installed is False and result.enabled == 3
    assert result.hidden is True and result.suspended is True
    assert 'correct.permission' in result.permissions
    assert 'wrong.permission' not in result.permissions
    assert result.primary_abi is None
    unknown = parse_package_dump(dump(users=False), 'com.example.app', 10)
    assert unknown.installed is None and unknown.enabled is None
    assert unknown.raw == dump(users=False)
    with pytest.raises(ValueError):
        parse_package_dump(dump(), 'not.present', 10)


def test_paths_preserve_special_characters_and_reject_missing_split_records():
    paths = parse_package_paths('package:/data/app/a $ space/base.apk\npackage:/data/app/a $ space/split_config.en.apk\n')
    assert len(paths) == 2 and '$ space' in paths[0]
    with pytest.raises(ValueError):
        parse_package_paths('package:/data/base.apk\nsplit incomplete\n')


def test_failed_required_list_preserves_last_complete_snapshot(ready):
    runner, controller = ready
    controller.refresh()
    complete_list(runner)
    old = controller.packages
    controller.refresh()
    assert len(runner.requests) == 3  # pm help is cached; the lists are one round trip
    runner.finish(list_frame(runner, 'package:other.app\n', 'SecurityException: denied\n'))
    assert controller.packages == old and controller.error and not controller.busy
    controller.refresh()
    runner.finish(list_frame(runner, rcs={'disabled': 1}))
    assert controller.packages == old and 'disabled' in controller.error
    controller.refresh()
    runner.finish(list_frame(runner)[:-20])
    assert controller.packages == old and controller.error and not controller.busy


def test_uninstall_and_enabled_readback_mismatch_cannot_report_success(ready):
    runner, controller = ready
    controller.refresh()
    complete_list(runner)
    controller.uninstall('com.example.app')
    runner.finish('Success\n')
    complete_list(runner)
    assert controller.error and '状态未确认' in controller.status
    controller.set_enabled('com.example.app', False)
    runner.finish('Package com.example.app new state: disabled-user\n')
    complete_list(runner)
    assert controller.packages['com.example.app'].enabled is True
    assert controller.error


@pytest.mark.parametrize('output', ['Failure [INSTALL_FAILED_ALREADY_EXISTS]\n', 'Error: denied\n', 'Success\nSecurityException\n'])
def test_install_failure_is_single_attempt_without_destructive_fallback(ready, tmp_path, output):
    runner, controller = ready
    apk = tmp_path / 'app.apk'
    apk.write_bytes(b'apk')
    controller.install([apk])
    runner.finish(output)
    assert len(runner.requests) == 1 and controller.error
    assert not controller.busy


def test_no_launcher_and_exit_zero_start_errors_are_failures(ready):
    runner, controller = ready
    controller.launch('com.example.app')
    runner.finish('No activity found\n')
    assert len(runner.requests) == 1 and controller.error
    controller.launch('com.example.app')
    runner.finish('com.example.app/.MainActivity\n')
    runner.finish('Error: Activity not started\n')
    assert controller.error and not controller.busy


def test_device_switch_and_submission_reentry_cannot_publish_old_package(ready):
    runner, controller = ready
    controller.load_details('com.example.app')
    controller.set_device('b')
    runner.finish(dump())
    assert controller.details == {} and len(runner.requests) == 1
    runner.task_added.connect(lambda _: controller.set_user(11))
    controller.refresh()
    assert not controller.busy and not controller.task_id
    runner.finish('list packages\n')
    assert controller.packages == {}


def start_export(runner, controller, target, version=10):
    controller.export('com.example.app', target)
    runner.finish(dump(version))
    runner.finish('package:/remote/base.apk\npackage:/remote/a/split.apk\npackage:/remote/b/split.apk\n')


def finish_pull(runner, content=b'apk', fail=False):
    part = Path(runner.requests[-1].args[-1])
    part.write_bytes(content)
    runner.finish('transfer failed' if fail else '1 file pulled\n', code=1 if fail else 0)
    return part


def test_export_split_collision_partial_failure_keeps_existing_files(ready, tmp_path):
    runner, controller = ready
    existing = tmp_path / 'base.apk'
    existing.write_bytes(b'original')
    start_export(runner, controller, tmp_path)
    first = finish_pull(runner)
    second = finish_pull(runner, fail=True)
    third = finish_pull(runner)
    assert existing.read_bytes() == b'original'
    assert not first.exists() and first.with_suffix('').is_file()
    assert second.is_file() and not second.with_suffix('').exists()
    assert not third.exists() and third.with_suffix('').is_file()
    assert len({local for _, local in controller.export_mapping}) == 3
    assert controller.error and not controller.busy


def test_export_version_change_never_reports_complete(ready, tmp_path):
    runner, controller = ready
    start_export(runner, controller, tmp_path)
    for _ in range(3):
        finish_pull(runner)
    runner.finish(dump(11))
    runner.finish('package:/remote/base.apk\npackage:/remote/a/split.apk\npackage:/remote/b/split.apk\n')
    assert controller.error and '期间更新' in controller.error
    assert not controller.busy


def test_export_switch_retains_partial_without_attaching_old_result(ready, tmp_path):
    runner, controller = ready
    start_export(runner, controller, tmp_path)
    part = Path(runner.requests[-1].args[-1])
    controller.set_device('b')
    part.write_bytes(b'partial')
    runner.finish('1 file pulled\n')
    assert part.exists() and not part.with_suffix('').exists()
    assert controller.details == {} and controller.error


def test_page_confirmation_context_switch_never_submits_export(qapp, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from sysdroid.core.users import AndroidUserController
    from sysdroid.ui.pages.apk_page import ApkPage

    runner = Runner()
    users = AndroidUserController(runner)
    users.set_device('a')
    page = ApkPage(runner, users)
    page.set_device('a')
    page.set_active(True)
    runner.finish('Users:\n\tUserInfo{0:Owner:13} running\n\tUserInfo{10:Work:10}\n')
    runner.finish('10\n')
    complete_list(runner)
    page.table.selectRow(0)
    before = len(runner.requests)

    def switch_and_accept(box):
        page.set_active(False)
        page.set_active(True)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    monkeypatch.setattr(QMessageBox, 'exec', switch_and_accept)
    page._export()
    assert len(runner.requests) == before
    assert page.controller.packages['com.example.app'].enabled is True
    page.deleteLater()
    assert not tuple(tmp_path.iterdir())


def test_page_version_sorting_and_filter_keep_selected_package(qapp):
    from PySide6.QtCore import Qt
    from sysdroid.core.users import AndroidUserController
    from sysdroid.ui.pages.apk_page import ApkPage

    runner = Runner()
    users = AndroidUserController(runner)
    users.set_device('a')
    page = ApkPage(runner, users)
    page.set_device('a')
    page.set_active(True)
    runner.finish('Users:\n\tUserInfo{10:Work:10} running\n')
    runner.finish('10\n')
    runner.finish('list packages [-f] [-i] [-U] [--show-versioncode] [--user USER_ID]\n')
    assert shlex.split(runner.requests[-1].args[1])[2].count('pm list packages') == 4
    runner.finish(list_frame(runner, 'package:com.example.app\npackage:other.app\n',
                             enriched='package:com.example.app uid:12 versionCode:100\npackage:other.app uid:2 versionCode:9\n'))
    page.table.sortItems(2, Qt.SortOrder.AscendingOrder)
    assert page.table.item(0, 0).text() == 'other.app'
    page.table.selectRow(1)
    selected = page._selected()
    page.search.setText('com.example')
    page._filter()
    assert page._selected() == selected
    assert page.table.isRowHidden(0)
    page.deleteLater()


@pytest.mark.parametrize('hide_during_refresh', [False, True])
def test_refresh_updates_discovered_users_and_selected_users_packages(qapp, hide_during_refresh):
    from sysdroid.core.users import AndroidUserController
    from sysdroid.ui.pages.apk_page import ApkPage

    runner = Runner()
    users = AndroidUserController(runner)
    users.set_device('a')
    page = ApkPage(runner, users)
    page.set_device('a')
    page.set_active(True)
    runner.finish('Users:\n\tUserInfo{0:Owner:13} running\n\tUserInfo{10:Work:10}\n')
    runner.finish('10\n')
    complete_list(runner)
    page.user_combo.setCurrentIndex(page.user_combo.findData(0))
    complete_list(runner)
    page.table.selectRow(0)
    page._refresh()
    if hide_during_refresh:
        page.set_active(False)
    runner.finish('Users:\n\tUserInfo{0:Owner:13} running\n\tUserInfo{10:Work:10}\n\tUserInfo{11:Guest:4}\n')
    runner.finish('10\n')
    complete_list(runner, members='package:com.example.app\npackage:new.app\n')
    page.set_active(True)
    assert page.controller.user_id == 0 and page.user_combo.currentData() == 0
    assert page.user_combo.findData(11) >= 0
    assert set(page.controller.packages) == {'com.example.app', 'new.app'}
    assert page._selected() == 'com.example.app' and not page.controller.busy
    page.deleteLater()


def test_refresh_recovers_after_failed_user_discovery(qapp):
    from sysdroid.core.users import AndroidUserController
    from sysdroid.ui.pages.apk_page import ApkPage

    runner = Runner()
    users = AndroidUserController(runner)
    users.set_device('a')
    page = ApkPage(runner, users)
    page.set_device('a')
    page.set_active(True)
    runner.finish('Error: denied\n')
    assert page.controller.user_id is None and not page.controller.packages
    page._refresh()
    runner.finish('Users:\n\tUserInfo{0:Owner:13} running\n')
    runner.finish('0\n')
    complete_list(runner)
    assert page.controller.user_id == 0 and set(page.controller.packages) == {'com.example.app'}
    assert not page.error.text() and not page.controller.busy
    page.deleteLater()


def test_diagnostic_words_inside_package_data_do_not_reject_snapshot(ready):
    runner, controller = ready
    package = 'com.example.exceptiondemo'
    controller.refresh()
    complete_list(runner, members=f'package:{package}\n')
    assert controller.packages[package].enabled is True
    assert not controller.error and not controller.busy


def test_updated_system_details_ignore_retained_factory_package():
    hidden = '\nHidden system packages:\n  Package [com.example.app] (factory):\n    versionCode=1\n    versionName=factory\n'
    detail = parse_package_dump(dump(version=12) + hidden, 'com.example.app', 10)
    assert detail.version_code == 12 and detail.version_name == '1.0'


def test_successful_replacement_invalidates_old_details_even_if_refresh_fails(ready, tmp_path):
    runner, controller = ready
    controller.details['com.example.app'] = parse_package_dump(dump(version=10), 'com.example.app', 10)
    apk = tmp_path / 'replacement.apk'
    apk.write_bytes(b'apk')
    controller.install([apk], replace=True)
    runner.finish('Success\n')
    assert controller.details == {}
    runner.finish('Error: permission denied\n')
    assert controller.details == {} and controller.error


@pytest.mark.parametrize('status', ['cancelled', 'timed_out'])
def test_stopped_split_export_preserves_partial_and_submits_no_next_pull(ready, tmp_path, status):
    runner, controller = ready
    start_export(runner, controller, tmp_path)
    part = Path(runner.requests[-1].args[-1])
    part.write_bytes(b'partial APK')
    task = runner.requests[-1]
    task.status, task.exit_code = status, None
    count = len(runner.requests)
    runner.task_finished.emit(task)
    assert len(runner.requests) == count
    assert part.read_bytes() == b'partial APK' and not part.with_suffix('').exists()
    assert controller.error and not controller.busy


# --- PR 2: batched lists, install progress, page actions --------------------

def make_page(qapp, members='package:com.example.app\n', system='', disabled=''):
    from sysdroid.core.users import AndroidUserController
    from sysdroid.ui.pages.apk_page import ApkPage

    runner = Runner()
    users = AndroidUserController(runner)
    users.set_device('a')
    page = ApkPage(runner, users)
    page.set_device('a')
    page.set_active(True)
    runner.finish('Users:\n\tUserInfo{0:Owner:13} running\n')
    runner.finish('0\n')
    complete_list(runner, members, system, disabled)
    page.table.selectRow(0)
    return runner, page


def test_list_script_runs_every_query_in_one_framed_round_trip():
    from sysdroid.core.apks import list_script, parse_list_batch
    script = list_script('abc', 10, ['-f', '-U'])
    assert script.count('pm list packages --user 10') == 4
    assert "rc=$?" in script and 'DONE' in script
    with pytest.raises(ValueError):
        parse_list_batch('FRAME abc\nBEGIN abc members\n\nEND abc members 0\nDONE abc\n', 'abc')
    with pytest.raises(ValueError):
        parse_list_batch('FRAME abc\nBEGIN abc rogue\n\nEND abc rogue 0\nDONE abc\n', 'abc')


def test_install_streams_progress_from_the_adb_binary_output(ready, tmp_path):
    runner, controller = ready
    seen = []
    controller.install_progress.connect(lambda line, percent: seen.append((line, percent)))
    apk = tmp_path / 'app.apk'
    apk.write_bytes(b'apk')
    task = controller.install([apk])
    assert task.args[0] == 'install' and task.args[-1] == str(apk.resolve())
    runner.task_output.emit(task.id, 'stdout', 'Performing Streamed Install\n')
    runner.task_output.emit(task.id, 'stdout', '[ 42%] /data/local/tmp/app.apk\r')
    runner.task_output.emit('other', 'stdout', '[ 99%] unrelated\n')
    assert seen[0] == ('Performing Streamed Install', -1)
    assert seen[-1] == ('[ 42%] /data/local/tmp/app.apk', 42)
    runner.finish('Performing Streamed Install\nSuccess\n')
    complete_list(runner)
    assert controller.status.startswith('安装命令成功') and not controller.busy


def test_page_basic_info_is_a_table_with_unknowns_marked(qapp):
    runner, page = make_page(qapp)
    page._details()
    runner.finish(dump())
    runner.finish('package:/data/app/a/base.apk\npackage:/data/app/a/split_en.apk\n')
    rows = {page.basic.item(row, 0).text(): page.basic.item(row, 1).text() for row in range(page.basic.rowCount())}
    assert rows['包名'] == 'com.example.app' and rows['版本码'] == '10' and rows['系统应用'] == '是'
    assert rows['推导 UID（非实测）'] == str(10023)
    assert rows['主 ABI'] == '设备未提供/未识别'
    assert rows['split 1'] == '/data/app/a/split_en.apk'
    page.deleteLater()


@pytest.mark.parametrize('action,expected', [
    ('_uninstall', ['pm', 'uninstall', '--user', '0', 'com.example.app']),
    ('_disable', ['pm', 'disable-user', '--user', '0', 'com.example.app']),
    ('_force_stop', ['am', 'force-stop', '--user', '0', 'com.example.app']),
])
def test_page_destructive_actions_need_confirmation(qapp, monkeypatch, action, expected):
    from PySide6.QtWidgets import QMessageBox
    runner, page = make_page(qapp)
    before = len(runner.requests)
    answers = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: answers[0])
    getattr(page, action)()
    assert len(runner.requests) == before
    answers[0] = QMessageBox.StandardButton.Yes
    getattr(page, action)()
    assert shlex.split(runner.requests[-1].args[1]) == expected
    page.deleteLater()


def test_page_enable_and_launch_run_without_confirmation(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    runner, page = make_page(qapp, disabled='package:com.example.app\n')
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: pytest.fail('unexpected confirmation'))
    assert page.controller.packages['com.example.app'].enabled is False
    page._enable()
    assert shlex.split(runner.requests[-1].args[1]) == ['pm', 'enable', '--user', '0', 'com.example.app']
    runner.finish('Package com.example.app new state: enabled\n')
    complete_list(runner)
    page._launch()
    assert 'resolve-activity' in runner.requests[-1].args[1]
    page.deleteLater()


def test_page_install_via_picker_and_drop_uses_options_and_shows_progress(qapp, monkeypatch, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    from PySide6.QtWidgets import QFileDialog
    runner, page = make_page(qapp)
    apk, split = tmp_path / 'base.apk', tmp_path / 'split_en.apk'
    apk.write_bytes(b'a')
    split.write_bytes(b'b')
    monkeypatch.setattr(QFileDialog, 'getOpenFileNames', lambda *args, **kwargs: ([str(apk), str(split)], ''))
    options = [None]
    monkeypatch.setattr(page, '_ask_install_options', lambda files: options[0])
    before = len(runner.requests)
    page._pick_install()
    assert len(runner.requests) == before  # dialog cancelled
    options[0] = (True, False)
    page._pick_install()
    task = runner.requests[-1]
    assert task.args[:4] == ['install-multiple', '--user', '0', '-r'] and len(task.args) == 6
    assert page.install_row.isVisibleTo(page) and page.install_progress.maximum() == 0
    runner.task_output.emit(task.id, 'stdout', '[ 70%] pushing\n')
    assert page.install_progress.value() == 70 and 'pushing' in page.install_label.text()
    page._cancel_install()
    assert runner.cancelled == [task.id]
    runner.finish('Failure [INSTALL_FAILED_ABORTED]\n', code=1)
    assert not page.install_row.isVisibleTo(page) and page.controller.error

    class Drop:
        def __init__(self, paths):
            self._mime = QMimeData()
            self._mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        def mimeData(self):
            return self._mime
    assert page._dropped_apks(Drop([apk])) == [apk]
    assert page._dropped_apks(Drop([apk, tmp_path / 'notes.txt'])) == []
    page.install_files([apk])
    assert runner.requests[-1].args[:4] == ['install', '--user', '0', '-r']
    page.deleteLater()


def test_page_install_rejects_non_apk_and_context_change_during_dialog(qapp, monkeypatch, tmp_path):
    runner, page = make_page(qapp)
    text = tmp_path / 'notes.txt'
    text.write_text('x')
    before = len(runner.requests)
    page.install_files([text])
    assert len(runner.requests) == before and '.apk' in page.error.text()
    apk = tmp_path / 'a.apk'
    apk.write_bytes(b'a')

    def switch(files):
        page.set_device('b')
        return (False, False)
    monkeypatch.setattr(page, '_ask_install_options', switch)
    page.install_files([apk])
    assert not any(task.args and task.args[0] == 'install' for task in runner.requests)
    page.deleteLater()

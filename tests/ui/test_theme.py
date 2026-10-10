import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget

from sysdroid.ui import theme
from sysdroid.ui import kit as ui_kit
@pytest.fixture
def clean_theme(qapp):
    yield qapp
    qapp.setStyleSheet("")
    qapp.setPalette(QPalette())
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "unsetColorScheme"):
        hints.unsetColorScheme()


def test_light_and_dark_tokens_cover_the_same_names_and_fill_the_style_sheet():
    assert set(theme.TOKENS["light"]) == set(theme.TOKENS["dark"])
    for tokens in theme.TOKENS.values():
        sheet = theme.build_style_sheet(tokens)
        assert "$" not in sheet
        assert all(QColor(value).isValid() for value in tokens.values())


def test_theme_mode_is_persisted_and_pins_accent_to_our_blue(clean_theme, tmp_path):
    settings = QSettings(str(tmp_path / "workspace.ini"), QSettings.Format.IniFormat)
    manager = theme.ThemeManager(settings)
    assert manager.mode == "system"
    seen = []
    manager.changed.connect(seen.append)
    manager.set_mode("dark")
    assert seen[-1] == "dark" and manager.scheme == "dark"
    palette = QApplication.instance().palette()
    assert palette.color(QPalette.ColorRole.Highlight) == QColor(theme.TOKENS["dark"]["accent"])
    if hasattr(QPalette.ColorRole, "Accent"):
        assert palette.color(QPalette.ColorRole.Accent) == QColor(theme.TOKENS["dark"]["accent"])
    manager.set_mode("light")
    assert QApplication.instance().palette().color(QPalette.ColorRole.Window) == QColor(theme.TOKENS["light"]["window"])
    reloaded = theme.ThemeManager(QSettings(str(tmp_path / "workspace.ini"), QSettings.Format.IniFormat))
    assert reloaded.mode == "light"
    with pytest.raises(ValueError):
        manager.set_mode("sepia")


def test_invalid_persisted_mode_falls_back_to_system(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "workspace.ini"), QSettings.Format.IniFormat)
    settings.setValue(theme.SETTINGS_KEY, "neon")
    assert theme.ThemeManager(settings).mode == "system"


def test_app_and_line_icons_are_not_empty(qapp):
    assert not theme.app_icon().isNull()
    for key in ("home", "refresh", "terminal", "info", "settings"):
        assert not theme.line_icon(key, "#000000", "#ffffff").isNull()


def test_roles_info_notes_and_empty_state_text(qapp):
    label = ui_kit.set_role(QLabel(), "pill", state="off")
    assert label.property("role") == "pill" and label.property("state") == "off"
    ui_kit.set_state(label, "ok")
    assert label.property("state") == "ok"
    note = ui_kit.info_note("short", "the full explanation")
    assert note.toolTip() == "the full explanation" and note.text().startswith("short")
    table = QTableWidget(0, 1)
    assert ui_kit.device_empty_text("", "", table).startswith("未连接设备")
    assert ui_kit.device_empty_text("abc", "offline", table).startswith("未连接设备")
    assert ui_kit.device_empty_text("abc", "device", table, "正在读取…") == "正在读取…"
    assert ui_kit.device_empty_text("abc", "device", table, "", "idle") == "idle"
    table.setRowCount(2)
    table.setRowHidden(0, True)
    assert ui_kit.has_visible_rows(table)
    table.setRowHidden(1, True)
    assert not ui_kit.has_visible_rows(table)
    assert ui_kit.device_empty_text("abc", "device", table).startswith("没有匹配")


def test_main_window_brand_pages_theme_and_dock(clean_theme, monkeypatch, tmp_path):
    from sysdroid.core import backend as android_backend
    from sysdroid.core import commands as android_commands
    from sysdroid.ui import main_window as main_window_module
    for module in (android_backend, android_commands, main_window_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    # A runtime error keeps the window from starting real ADB processes.
    window = main_window_module.SysDroidWindow("test runtime: adb disabled")
    try:
        assert window.windowTitle().startswith(main_window_module.APP_NAME)
        assert not window.windowIcon().isNull()
        for key, (title, description) in window.PAGE_INFO.items():
            window._select_page(key)
            assert window.page_title.text() == title and window.page_subtitle.text() == description
        assert window.bottom_stack.currentWidget() is window.task_panel  # output page
        window._select_page("home")
        window._set_dock_view(main_window_module.DOCK_LOG, remember=True)
        assert window.bottom_stack.currentWidget() is window.log_panel
        window._select_page("props")
        assert window.bottom_stack.currentWidget() is window.log_panel
        window._toggle_log()
        assert window._panel_collapsed and window.bottom_stack.isHidden()
        window._select_page("output")
        assert not window.bottom_stack.isHidden()
        window._set_theme("dark")
        assert window.theme.scheme == "dark"
        assert window._theme_actions["dark"].isChecked()
        settings = QSettings(str(tmp_path / "workspace.ini"), QSettings.Format.IniFormat)
        assert settings.value("ui/theme") == "dark"
        assert int(settings.value("ui/dock_view")) == main_window_module.DOCK_LOG
        ui_states = {window.connected.property("state")}
        assert ui_states == {"off"}
    finally:
        window.close()
        window.deleteLater()

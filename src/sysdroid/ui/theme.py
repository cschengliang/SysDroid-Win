"""Central light / dark theme: color tokens, palette, application style sheet and drawn icons.

Every color used by the UI lives in TOKENS. Widgets opt into semantic styling through
dynamic properties (see ui_kit.set_role) instead of inline style sheets, so switching the
theme only has to re-apply the palette and the style sheet built here.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

from sysdroid.runtime_paths import assets_dir

MODES = ("system", "light", "dark")
MODE_LABELS = {"system": "跟随系统", "light": "浅色", "dark": "深色"}
SETTINGS_KEY = "ui/theme"

TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "window": "#f3f5f7", "surface": "#ffffff", "surface_alt": "#f7f9fb", "nav": "#eaeef2",
        "border": "#d8dde3", "border_strong": "#b9c1ca", "text": "#1d232a", "text_muted": "#56616d",
        "text_subtle": "#8a949e", "accent": "#1f6fb2", "accent_hover": "#185f99", "accent_pressed": "#144f80",
        "accent_text": "#ffffff", "accent_soft": "#dceaf7", "hover": "#e8f1fa", "selection": "#cfe3f5",
        "selection_text": "#132030", "header_bg": "#eef2f6", "header_text": "#3e4a56",
        "input_bg": "#ffffff", "log_bg": "#ffffff", "disabled_text": "#9aa3ab", "disabled_bg": "#eef0f2",
        "error": "#b42318", "error_bg": "#fdeceb", "error_border": "#f1b9b4",
        "warning": "#9a4a00", "warning_bg": "#fff3e0", "warning_border": "#f2cf9a",
        "success": "#146c35", "success_bg": "#e4f4e9", "success_border": "#a9d8b8",
        "tooltip_bg": "#1d232a", "tooltip_text": "#f3f5f7",
    },
    "dark": {
        "window": "#1a1d21", "surface": "#22262b", "surface_alt": "#272c32", "nav": "#16191c",
        "border": "#343a42", "border_strong": "#4a525c", "text": "#e5e8eb", "text_muted": "#a6afb9",
        "text_subtle": "#78828c", "accent": "#3b8ee0", "accent_hover": "#55a0ea", "accent_pressed": "#2d79c4",
        "accent_text": "#ffffff", "accent_soft": "#1e3550", "hover": "#2a3542", "selection": "#28496b",
        "selection_text": "#ffffff", "header_bg": "#2a2f35", "header_text": "#c4ccd4",
        "input_bg": "#1d2125", "log_bg": "#15181b", "disabled_text": "#666e77", "disabled_bg": "#25292e",
        "error": "#ff8f86", "error_bg": "#3a1f1e", "error_border": "#6e302c",
        "warning": "#f3b664", "warning_bg": "#3a2c16", "warning_border": "#6b5025",
        "success": "#72d394", "success_bg": "#18321f", "success_border": "#2f6140",
        "tooltip_bg": "#e5e8eb", "tooltip_text": "#1a1d21",
    },
}

# 24x24 line icons: polylines plus optional circles (cx, cy, r).
_ICON_LINES: dict[str, tuple] = {
    "home": (((7, 2), (17, 2), (17, 22), (7, 22), (7, 2)), ((10, 5), (14, 5)), ((11, 19), (13, 19))),
    "commands": (((2, 4), (22, 4), (22, 20), (2, 20), (2, 4)), ((6, 9), (9, 12), (6, 15)), ((12, 15), (17, 15))),
    "props": (((3, 3), (21, 3), (21, 21), (3, 21), (3, 3)), ((6, 7), (9, 7)), ((12, 7), (18, 7)),
              ((6, 12), (9, 12)), ((12, 12), (18, 12)), ((6, 17), (9, 17)), ((12, 17), (18, 17))),
    "settings": (((10, 2), (14, 2), (14.5, 5), (16, 6), (18.5, 5), (20.5, 8.5), (18, 10.5), (18, 13.5),
                  (20.5, 15.5), (18.5, 19), (16, 18), (14.5, 19), (14, 22), (10, 22), (9.5, 19), (8, 18),
                  (5.5, 19), (3.5, 15.5), (6, 13.5), (6, 10.5), (3.5, 8.5), (5.5, 5), (8, 6), (9.5, 5), (10, 2)),),
    "apks": (((12, 2), (21, 7), (21, 17), (12, 22), (3, 17), (3, 7), (12, 2)), ((3, 7), (12, 12), (21, 7)),
             ((12, 12), (12, 22)), ((8, 4.2), (17, 9.2))),
    "processes": (((2, 3), (22, 3), (22, 21), (2, 21), (2, 3)), ((5, 14), (8, 14), (10, 8), (13, 17), (15, 11), (19, 11))),
    "scrcpy": (((3, 3), (21, 3), (21, 16), (3, 16), (3, 3)), ((12, 16), (12, 21)), ((8, 21), (16, 21)),
               ((10, 7), (15, 9.5), (10, 12), (10, 7))),
    "output": (((4, 2), (15, 2), (21, 8), (21, 22), (4, 22), (4, 2)), ((15, 2), (15, 8), (21, 8)),
               ((8, 12), (17, 12)), ((8, 16), (17, 16)), ((8, 19), (14, 19))),
    "terminal": (((2, 5), (22, 5), (22, 19), (2, 19), (2, 5)), ((5.5, 9), (8.5, 12), (5.5, 15)), ((11, 15.5), (16, 15.5))),
    "info": (((12, 11), (12, 17)), ((12, 7.6), (12, 7.8))),
    "refresh": (),
}
_ICON_CIRCLES: dict[str, tuple] = {"settings": ((12, 12, 3),), "info": ((12, 12, 9.5),)}


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 28) -> tuple:
    return tuple((cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
                  cy - r * math.sin(math.radians(start + (end - start) * i / steps))) for i in range(steps + 1))


_ICON_LINES["refresh"] = (_arc(12, 12, 8, 60, 330), ((16.2, 1.8), (16, 5.1), (19.3, 5.8)))


class ThemeManager(QObject):
    """Owns the theme mode (system / light / dark), persists it and applies it app-wide."""

    changed = Signal(str)  # effective scheme: "light" or "dark"

    def __init__(self, settings: QSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        value = settings.value(SETTINGS_KEY, "system") if settings is not None else "system"
        self._mode = value if value in MODES else "system"
        self._scheme = "light"
        self._applying = False
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._system_changed)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def scheme(self) -> str:
        return self._scheme

    def tokens(self) -> dict[str, str]:
        return TOKENS[self._scheme]

    def color(self, name: str) -> QColor:
        return QColor(TOKENS[self._scheme][name])

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown theme mode: {mode}")
        self._mode = mode
        if self._settings is not None:
            self._settings.setValue(SETTINGS_KEY, mode)
            self._settings.sync()
        self.apply()

    def _system_changed(self, *_args) -> None:
        if not self._applying and self._mode == "system":
            self.apply()

    def _resolve(self) -> str:
        hints = QGuiApplication.styleHints()
        self._applying = True
        try:
            if self._mode == "system":
                if hasattr(hints, "unsetColorScheme"):
                    hints.unsetColorScheme()
                return "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
            if hasattr(hints, "setColorScheme"):
                hints.setColorScheme(Qt.ColorScheme.Dark if self._mode == "dark" else Qt.ColorScheme.Light)
            return self._mode
        finally:
            self._applying = False

    def apply(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        self._scheme = self._resolve()
        tokens = TOKENS[self._scheme]
        app.setPalette(build_palette(tokens))
        app.setStyleSheet(build_style_sheet(tokens))
        self.changed.emit(self._scheme)


def build_palette(t: dict[str, str]) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "input_bg", QPalette.ColorRole.AlternateBase: "surface_alt",
        QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "surface",
        QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.BrightText: "error",
        QPalette.ColorRole.Highlight: "accent", QPalette.ColorRole.HighlightedText: "accent_text",
        QPalette.ColorRole.ToolTipBase: "tooltip_bg", QPalette.ColorRole.ToolTipText: "tooltip_text",
        QPalette.ColorRole.PlaceholderText: "text_subtle", QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.LinkVisited: "accent_pressed", QPalette.ColorRole.Light: "surface",
        QPalette.ColorRole.Midlight: "border", QPalette.ColorRole.Mid: "border_strong",
        QPalette.ColorRole.Dark: "border_strong", QPalette.ColorRole.Shadow: "border_strong",
    }
    for role, token in roles.items():
        palette.setColor(role, QColor(t[token]))
    # The Windows 11 style paints check boxes, radio buttons, sliders and focus frames with
    # the Accent role; pin it to our blue instead of the user's Windows accent color.
    if hasattr(QPalette.ColorRole, "Accent"):
        palette.setColor(QPalette.ColorRole.Accent, QColor(t["accent"]))
    for role, token in ((QPalette.ColorRole.WindowText, "disabled_text"), (QPalette.ColorRole.Text, "disabled_text"),
                        (QPalette.ColorRole.ButtonText, "disabled_text"), (QPalette.ColorRole.Button, "disabled_bg"),
                        (QPalette.ColorRole.Base, "disabled_bg"), (QPalette.ColorRole.Highlight, "border_strong")):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(t[token]))
    return palette


_STYLE_SHEET = """
QWidget { color: $text; }
QMainWindow, QWidget#shell, QStackedWidget#pages, QWidget#pageHeader { background: $window; }
QToolTip { background: $tooltip_bg; color: $tooltip_text; border: 1px solid $border_strong; padding: 4px 6px; }

QMenuBar { background: $window; color: $text; border-bottom: 1px solid $border; padding: 0 4px; }
QMenuBar::item { padding: 4px 9px; background: transparent; }
QMenuBar::item:selected { background: $hover; border-radius: 4px; }
QMenu { background: $surface; color: $text; border: 1px solid $border; }
QMenu::item:selected { background: $selection; color: $selection_text; }
QMenu::item:disabled { color: $disabled_text; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }

QToolBar#mainToolbar { background: $window; border: 0; border-bottom: 1px solid $border; spacing: 4px; padding: 3px 8px; }
QToolBar#mainToolbar QToolButton { min-width: 28px; min-height: 26px; border: 1px solid transparent; border-radius: 5px; padding: 2px; }
QToolBar#mainToolbar QToolButton:hover { background: $hover; border-color: $border; }
QToolBar#mainToolbar QToolButton:pressed { background: $accent_soft; }
QLabel#brandLabel { font-size: 11pt; font-weight: 600; padding: 0 6px 0 2px; }

QWidget#navPanel { background: $nav; border-right: 1px solid $border; }
QListWidget#navigation { background: transparent; border: 0; outline: 0; padding: 6px 0; }
QListWidget#navigation::item { color: $text; padding: 5px 8px; margin: 1px 8px; border-radius: 6px; border-left: 3px solid transparent; }
QListWidget#navigation::item:hover { background: $hover; }
QListWidget#navigation::item:selected { background: $accent_soft; color: $accent; border-left: 3px solid $accent; }
QListWidget#navigation::item:disabled { color: $text_subtle; background: transparent; border-left-color: transparent; }

QLabel[role="pageTitle"] { font-size: 13pt; font-weight: 600; }
QLabel[role="pageSubtitle"] { color: $text_muted; }
QLabel[role="cardTitle"] { font-size: 14pt; font-weight: 600; }
QLabel[role="section"] { font-weight: 600; }
QLabel[role="hint"] { color: $text_muted; }
QLabel[role="error"] { color: $error; }
QLabel[role="pill"] { background: $surface_alt; color: $text_muted; border: 1px solid $border; border-radius: 10px; padding: 2px 10px; }
QLabel[role="pill"][state="ok"] { background: $success_bg; color: $success; border-color: $success_border; }
QLabel[role="pill"][state="warn"] { background: $warning_bg; color: $warning; border-color: $warning_border; }

QFrame[role="card"] { background: $surface; border: 1px solid $border; border-radius: 8px; }
QFrame[role="card"] QLabel { background: transparent; border: 0; }
QFrame[role="card"] QLabel[role="pill"] { border: 1px solid $border; background: $surface_alt; }
QFrame[role="card"] QLabel[role="pill"][state="ok"] { background: $success_bg; border-color: $success_border; }
QFrame[role="card"] QLabel[role="pill"][state="warn"] { background: $warning_bg; border-color: $warning_border; }
QLineEdit#currentSerial { border: 0; background: transparent; padding: 0; color: $text_muted; }

QScrollArea[role="banner"] { background: $error_bg; border: 1px solid $error_border; border-radius: 6px; }
QScrollArea[role="banner"] > QWidget, QScrollArea[role="banner"] QLabel { background: transparent; }
QScrollArea[role="statusBox"] { background: $surface; border: 1px solid $border; border-radius: 6px; }
QScrollArea[role="statusBox"] > QWidget, QScrollArea[role="statusBox"] QWidget#statusBoxContent { background: transparent; }

QGroupBox { background: $surface; border: 1px solid $border; border-radius: 8px; margin-top: 10px; padding: 12px 8px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: $text; }
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QPushButton, QGroupBox QLineEdit, QGroupBox QComboBox,
QGroupBox QPlainTextEdit, QGroupBox QTextEdit, QGroupBox QSpinBox, QGroupBox QTableView, QGroupBox QTabBar { font-weight: normal; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 24px; border: 1px solid $border_strong; border-radius: 5px; background: $input_bg; padding: 0 6px; selection-background-color: $accent; selection-color: $accent_text; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color: $accent; }
QLineEdit:read-only { background: $surface_alt; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: $disabled_bg; color: $disabled_text; border-color: $border; }
QComboBox QAbstractItemView { background: $surface; color: $text; border: 1px solid $border; selection-background-color: $selection; selection-color: $selection_text; outline: 0; }
QPlainTextEdit, QTextEdit { background: $input_bg; border: 1px solid $border_strong; border-radius: 5px; selection-background-color: $accent; selection-color: $accent_text; }
QPlainTextEdit[readOnly="true"], QTextEdit[readOnly="true"] { background: $surface_alt; }
QPlainTextEdit#logOutput, QPlainTextEdit#taskStdout, QPlainTextEdit#taskStderr {
    background: $log_bg; font-family: "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace; font-size: 9pt; }

QPushButton { min-height: 24px; padding: 0 12px; border: 1px solid $border_strong; border-radius: 5px; background: $surface; color: $text; }
QPushButton:hover { background: $hover; border-color: $accent; }
QPushButton:pressed, QPushButton:checked { background: $accent_soft; border-color: $accent; }
QPushButton:disabled { color: $disabled_text; background: $disabled_bg; border-color: $border; }
QPushButton#primaryButton, QPushButton[variant="primary"] { background: $accent; border-color: $accent; color: $accent_text; font-weight: 600; }
QPushButton#primaryButton:hover, QPushButton[variant="primary"]:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primaryButton:pressed, QPushButton[variant="primary"]:pressed { background: $accent_pressed; }
QPushButton#primaryButton:disabled, QPushButton[variant="primary"]:disabled { background: $disabled_bg; border-color: $border; color: $disabled_text; }
QPushButton[role="segment"] { border-radius: 0; min-width: 64px; background: transparent; border: 0; border-bottom: 2px solid transparent; color: $text_muted; padding: 0 10px; }
QPushButton[role="segment"]:hover { color: $text; background: $hover; }
QPushButton[role="segment"]:checked { color: $text; border-bottom-color: $accent; background: transparent; font-weight: 600; }
QPushButton#connectedButton { border-radius: 12px; padding: 0 12px; background: $surface_alt; color: $text_muted; border-color: $border; }
QPushButton#connectedButton[state="ok"] { background: $success_bg; color: $success; border-color: $success_border; }
QPushButton#connectedButton[state="warn"] { background: $warning_bg; color: $warning; border-color: $warning_border; }
QPushButton#connectedButton:hover { border-color: $accent; }

QTabWidget::pane { background: $surface; border: 1px solid $border; border-radius: 6px; top: -1px; }
QTabBar::tab { background: transparent; color: $text_muted; padding: 5px 12px; border: 0; border-bottom: 2px solid transparent; margin-right: 2px; }
QTabBar::tab:hover { color: $text; background: $hover; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; font-weight: 600; }
QTabBar::tab:disabled { color: $disabled_text; }

QTableView { background: $surface; alternate-background-color: $surface_alt; gridline-color: $border; border: 1px solid $border; border-radius: 6px;
             selection-background-color: $selection; selection-color: $selection_text; outline: 0; }
QTableView::item { padding: 0 4px; border: 0; }
QTableView::item:selected { background: $selection; color: $selection_text; }
QHeaderView { background: $header_bg; border: 0; }
QHeaderView::section { background: $header_bg; color: $header_text; border: 0; border-right: 1px solid $border; border-bottom: 1px solid $border; padding: 4px 6px; font-weight: 600; }
QTableCornerButton::section { background: $header_bg; border: 0; }

QSplitter::handle { background: $window; }
QSplitter::handle:hover { background: $accent_soft; }
QWidget#dockHeader { background: $window; border-top: 1px solid $border; }
QStatusBar { background: $nav; border-top: 1px solid $border; color: $text_muted; }
QStatusBar::item { border: 0; }
QStatusBar QLabel { color: $text_muted; padding: 0 6px; }
QCheckBox { spacing: 6px; }
"""


def build_style_sheet(tokens: dict[str, str]) -> str:
    sheet = _STYLE_SHEET
    for name in sorted(tokens, key=len, reverse=True):
        sheet = sheet.replace("$" + name, tokens[name])
    return sheet


def line_icon(key: str, normal: QColor | str, selected: QColor | str | None = None, size: int = 24) -> QIcon:
    """Draw a 24x24 line icon for 1x/2x/3x device pixel ratios (crisp at any DPI)."""
    paths = [QPolygonF([QPointF(x, y) for x, y in points]) for points in _ICON_LINES[key]]
    icon = QIcon()
    modes: Iterable = ((QIcon.Mode.Normal, normal),) + (((QIcon.Mode.Selected, selected),) if selected else ())
    for mode, color in modes:
        for ratio in (1, 2, 3):
            pixmap = QPixmap(size * ratio, size * ratio)
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / 24, size / 24)
            painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for path in paths:
                painter.drawPolyline(path)
            for cx, cy, r in _ICON_CIRCLES.get(key, ()):
                painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def app_icon_files(directory: Path | None = None) -> dict[int, Path]:
    """Bundled icon PNGs by size (assets/icons/sysdroid-<size>.png); missing sizes are skipped."""
    folder = (directory if directory is not None else assets_dir()) / "icons"
    files = {size: folder / f"sysdroid-{size}.png" for size in APP_ICON_SIZES}
    return {size: path for size, path in files.items() if path.is_file()}


def app_icon(directory: Path | None = None) -> QIcon:
    """Multi-size application icon; Qt picks the hand-tuned 16/24 px masters for small sizes."""
    icon = QIcon()
    for size, path in app_icon_files(directory).items():
        icon.addFile(str(path), QSize(size, size))
    return icon


def app_font() -> QFont:
    return QFont("Microsoft YaHei UI", 9)

"""Application entry point: create the QApplication and show the main window."""
from __future__ import annotations

import ctypes
import os
import sys

from PySide6.QtWidgets import QApplication, QStyleFactory

from sysdroid.app_info import APP_NAME, APP_USER_MODEL_ID
from sysdroid.runtime_paths import configure_runtime
from sysdroid.ui import theme
from sysdroid.ui.main_window import SysDroidWindow


def set_app_user_model_id(shell32: object | None = None) -> bool:
    """Give the process an explicit AppUserModelID (Windows only) before any window exists.

    Without it the taskbar groups source runs under python.exe's identity and its icon.
    """
    if shell32 is None:
        if os.name != "nt":
            return False
        shell32 = ctypes.windll.shell32
    try:
        return shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID) == 0
    except (AttributeError, OSError):
        return False


def main() -> int:
    set_app_user_model_id()
    app = QApplication(sys.argv)
    if "windows11" in QStyleFactory.keys():
        app.setStyle("windows11")
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setFont(theme.app_font())
    app.setWindowIcon(theme.app_icon())
    runtime_error = ""
    try:
        configure_runtime()
    except ValueError as exc:
        runtime_error = str(exc)
    window = SysDroidWindow(runtime_error)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

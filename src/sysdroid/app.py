"""Application entry point: create the QApplication and show the main window."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QStyleFactory

from sysdroid.app_info import APP_NAME
from sysdroid.runtime_paths import configure_runtime
from sysdroid.ui import theme
from sysdroid.ui.main_window import AndroidToolboxWindow


def main() -> int:
    app = QApplication(sys.argv)
    if "windows11" in QStyleFactory.keys():
        app.setStyle("windows11")
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setFont(theme.app_font())
    runtime_error = ""
    try:
        configure_runtime()
    except ValueError as exc:
        runtime_error = str(exc)
    window = AndroidToolboxWindow(runtime_error)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

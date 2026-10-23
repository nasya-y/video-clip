"""QApplication bootstrap."""

from __future__ import annotations

import logging
import sys
from typing import Optional, Sequence

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_ID, APP_NAME
from ..paths import resource_path
from .style import STYLESHEET

log = logging.getLogger(__name__)


def make_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        if sys.platform == "win32":
            try:  # own taskbar icon/group instead of python.exe's
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
            except Exception:
                pass
        app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ID)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    icon = resource_path("icons", "app.png")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    return app


def run_gui(screenshot: Optional[str] = None, initial_inputs: Optional[Sequence[str]] = None) -> int:
    from .main_window import MainWindow

    app = make_app()
    win = MainWindow(list(initial_inputs or []))
    if screenshot:
        win.resize(1200, 860)
    win.show()
    if screenshot:
        def grab():
            win.grab().save(screenshot)
            log.info("Saved screenshot to %s", screenshot)
            win.close()
            app.quit()
        QTimer.singleShot(1500, grab)
    return app.exec()

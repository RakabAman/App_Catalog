"""
Entry point for the App Catalog GUI.

Usage:
    python run_gui.py                  # opens/creates catalog.db next to
                                          this script (or next to the .exe,
                                          once packaged -- see app_paths.py)
    python run_gui.py path/to/other.db # use a specific DB file; if given
                                          as a relative path it's anchored
                                          to the app's own folder, not
                                          whatever the current working
                                          directory happens to be

Logging goes to BOTH the console (if one exists -- see the frozen/
windowed check below) and a rotating file at logs/app.log next to the
catalog, so there's always a record even for a double-clicked .exe with
no visible console window. See app_paths.py's module docstring for why
paths are resolved the way they are (checkpoint 21).
"""

import ctypes
import logging
import logging.handlers
import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app_paths import (
    get_app_log_path, get_default_db_path, resolve_db_path, get_resource_path,
)


def _setup_logging(db_path: str):
    handlers = []

    # A PyInstaller build made with --windowed/--noconsole has no real
    # stdout/stderr (they're None, or a closed/non-writable stream
    # depending on platform) -- attaching a StreamHandler to a None
    # stream makes EVERY log call raise inside logging's own error
    # handling, which can spam or, in the worst case, surface as the app
    # appearing to hang. Only attach the console handler if a usable
    # stream actually exists; the file handler below is unconditional,
    # so nothing is ever lost either way.
    if sys.stdout is not None:
        try:
            sys.stdout.write("")
            handlers.append(logging.StreamHandler(stream=sys.stdout))
        except (ValueError, OSError):
            pass

    try:
        log_path = get_app_log_path(db_path)
        # Rotating so a long-lived install doesn't grow an unbounded log
        # file over months of use -- 5 files x 2MB is plenty of history
        # for troubleshooting without ever becoming a disk-space concern.
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    except OSError as e:
        # Logging setup itself must never prevent the app from starting.
        if handlers:
            handlers[0].handle(logging.LogRecord(
                "appcatalog", logging.WARNING, __file__, 0,
                f"Could not open log file ({e}) -- console logging only.", None, None,
            ))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _apply_ui_scale(db_path: str):
    """
    Qt's QT_SCALE_FACTOR env var scales the whole UI (fonts, widget sizes,
    spacing) but MUST be set before QApplication is constructed -- so this
    reads the setting via a raw DB connection first, before any Qt/GUI
    imports happen. Changing this setting therefore takes effect on the
    next launch, not live within the current session.
    """
    try:
        from database import Database
        db = Database(db_path)
        db.init_schema()
        scale = db.get_setting("ui_scale_multiplier", 1.0)
        db.close()
        if scale and float(scale) != 1.0:
            os.environ["QT_SCALE_FACTOR"] = str(scale)
    except Exception:
        pass  # never block startup over a cosmetic setting


def main():
    raw_db_path = sys.argv[1] if len(sys.argv) > 1 else get_default_db_path()
    db_path = resolve_db_path(raw_db_path)

    _setup_logging(db_path)
    logging.getLogger("appcatalog").info("Using catalog: %s", db_path)

    _apply_ui_scale(db_path)

    app = QApplication(sys.argv)
    app.setApplicationName("App Catalog")

    # Windows taskbar grouping is keyed off an explicit AppUserModelID
    # set on the PROCESS, not per-window. Without one, Windows treats the
    # process as a generic python.exe and shows Python's icon on the
    # taskbar regardless of setWindowIcon(), no matter what the title bar
    # shows. Must be set before the first top-level window is created;
    # harmless no-op on non-Windows and if the shell32 call fails.
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "AppCatalog.Gui.1"
            )
        except Exception:
            logging.getLogger("appcatalog").debug(
                "SetCurrentProcessExplicitAppUserModelID failed", exc_info=True
            )

    # The ico= stamp in the .spec gives the .exe file its icon in Explorer;
    # it does NOT make Qt use it for the title bar / taskbar. Setting the
    # application-wide icon here covers every window the app opens
    # (MainWindow, OrganizeDialog, etc.) without each one setting its own.
    icon_path = get_resource_path("ico.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logging.getLogger("appcatalog").warning(
            "ico.ico not found at %s -- window/taskbar icon will be default",
            icon_path,
        )

    from gui_main import MainWindow
    window = MainWindow(db_path)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

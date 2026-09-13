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

import logging
import logging.handlers
import os
import sys

from PySide6.QtWidgets import QApplication

from app_paths import get_app_log_path, get_default_db_path, resolve_db_path


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

    from gui_main import MainWindow
    window = MainWindow(db_path)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

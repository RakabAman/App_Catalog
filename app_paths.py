"""
app_paths.py -- where things live on disk, made safe for a PyInstaller
build.

WHY THIS MODULE EXISTS (checkpoint 21):
Before this, the app's default DB path was the bare relative string
"catalog.db" (see run_gui.py), and every other path in the app --
manifest cache files (scraper.py), the reorganize/monitor move-log JSON
(app_manager.py/monitor.py) -- was derived from THAT path's directory.
That's fine under `python run_gui.py` run from a terminal sitting in the
project folder, because the current working directory (CWD) happens to
equal the script's own folder. It quietly breaks once this is packaged
with PyInstaller: a double-clicked .exe, a Start Menu shortcut, a pinned
taskbar icon, or "Run as administrator" can all launch with a CWD that
is NOT the .exe's own folder (Windows has no guarantee here -- a
shortcut's "Start in" field can point anywhere, or be blank, in which
case behaviour varies). The visible symptom is the app silently
creating a brand new empty catalog.db (and logs/, and a fresh manifest
cache) in the wrong folder every time it's launched a different way --
looking like data loss.

THE FIX: never rely on CWD for anything. The DEFAULT database path is
anchored to the actual running app's own folder --
`sys.executable`'s folder for a frozen PyInstaller build (onefile OR
onedir -- both put the real .exe next to where you'd expect, PyInstaller
onefile's `sys._MEIPASS` extraction temp dir is NOT used here on
purpose, since that's a throwaway folder deleted after the process
exits), or this script's own folder otherwise. A user-supplied db path
(CLI arg, or a future "Open other catalog…" GUI action) is honoured
as-is if absolute, and anchored to that same app folder if given as a
relative path, rather than silently resolving against whatever CWD
happens to be.

LAYOUT: everything for ONE catalog lives next to that catalog's .db
file (not next to the .exe, if the two ever differ -- e.g. a
poweruser running two catalogs from two .db files on a data drive
should get two independent logs/ and manifest/ folders, not one shared
pair that collides):
    <catalog folder>/
        catalog.db              <- bare, next to the app, as before
        logs/                   <- every *.json move-log AND every
                                    *.html report this app writes, plus
                                    the general application log
                                    (app.log). NOTHING log-like is
                                    written anywhere else any more.
        manifest/                <- winget_manifest_cache.json and
                                    winutil_apps_cache.json (external
                                    metadata caches -- unrelated to the
                                    app's own catalog data, safe to
                                    delete any time to force a refresh)

Every function here is idempotent and creates directories as needed
(mkdir parents=True, exist_ok=True) -- callers never need to check
existence first.
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_base_dir() -> Path:
    """
    The folder the running app should be considered "installed in":
      - Frozen (PyInstaller onefile or onedir): the real .exe's own
        folder -- `sys.executable`, NOT `sys._MEIPASS` (that's a
        temporary extraction folder for onefile builds, wiped on exit;
        writing a database there would silently lose it every run).
      - Plain `python run_gui.py`: this file's own folder, same as
        before -- unaffected for anyone running from source.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_db_path(db_path: str | Path) -> str:
    """
    Anchors a possibly-relative db path to the app's own folder instead
    of the current working directory. An already-absolute path (the
    common case once the GUI remembers a "last used" catalog, or a
    poweruser's explicit CLI path to another drive) is returned as-is.
    """
    p = Path(db_path)
    if not p.is_absolute():
        p = get_app_base_dir() / p
    return str(p)


def get_default_db_path() -> str:
    """catalog.db, bare, directly in the app's own folder."""
    return str(get_app_base_dir() / "catalog.db")


def _db_dir(db_path: str | Path) -> Path:
    d = Path(db_path).resolve().parent
    return d if str(d) else Path(".")


def get_logs_dir(db_path: str | Path) -> Path:
    """<catalog folder>/logs -- created if missing."""
    d = _db_dir(db_path) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_manifest_dir(db_path: str | Path) -> Path:
    """<catalog folder>/manifest -- created if missing."""
    d = _db_dir(db_path) / "manifest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_app_log_path(db_path: str | Path) -> Path:
    """The single general application log file: <catalog folder>/logs/app.log"""
    return get_logs_dir(db_path) / "app.log"

def get_resource_path(name: str) -> Path:
    """
    Locate a bundled read-only data file (ico.ico, etc.) in all three
    run modes this app supports:

      - PyInstaller onefile: PyInstaller extracts bundled datas into a
        temp folder at launch; sys._MEIPASS points at it. The file is
        there, not next to the .exe.
      - PyInstaller onedir (PyInstaller >= 5.0): datas are placed inside
        the _internal/ subfolder, and sys._MEIPASS is set to point at
        that folder too. Same lookup works.
      - PyInstaller onedir (older versions) / plain `python run_gui.py`:
        fall back to the app's own folder, same as app_paths.py itself.

    Distinct from get_app_base_dir() on purpose: that's "where user data
    goes", this is "where bundled read-only resources are found". They
    are the same folder in dev/onedir-old and different in onefile.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / name
        return Path(sys.executable).resolve().parent / name
    return Path(__file__).resolve().parent / name
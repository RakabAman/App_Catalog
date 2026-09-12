"""
monitor.py -- self-contained "Monitor" job for newly-downloaded /
newly-stored installers in one or more watch folders.

This module owns EVERYTHING to do with monitoring: the two-phase job
engine, the compression backends, and the three dialogs that drive it.
The GUI imports exactly one public name -- `MonitorJob` -- and never
sees the worker thread, the plan dataclasses, or the dialogs. That keeps
the monitor feature independently tweakable: changing a dialog here can't
break anything in gui_main.py, and gui_main.py doesn't need to change if
this module grows.

Public API (used by gui_main.py):

    job = MonitorJob(parent_window, db, db_path)
    job.progress.connect(some_slot)         # status text for the status bar
    job.job_finished.connect(some_slot)     # emits MonitorResult | None
    job.job_failed.connect(some_slot)       # emits str
    started = job.start()                   # False if user cancelled the
                                            # config dialog; True once the
                                            # worker is running
    job.cancel()                            # ask a running job to stop

The GUI is expected to treat `job` as its `_active_worker` while it runs
(so it interleaves correctly with Scan / Resolve / Scrape jobs). It
should clear that lock in both job_finished and job_failed.

Two-phase design (see the module's design discussion in AI_MODULE_
REFERENCE.md section 10):

  Phase 1 -- PLAN (pure read-only)
      scan_monitor_folders() walks each configured folder, filters
      candidates (extension / size / not-partial-download / size-settled),
      runs each through resolver.extract_fields(), and proposes a match
      against the apps table (exact normalize_key hit / fuzzy candidates /
      none). Returns list[MonitorPlanItem].

  Phase 2 -- EXECUTE (writes)
      execute_monitor_plan() takes the possibly user-EDITED plan and, per
      item: attaches to an existing app or creates a new one
      (status=needs_review), compresses the source file if its extension
      isn't already compressed, move-or-copies the result into
      <dest_root>/<Catalog>/<Subcatalog>/<AppName>/<Version>/, records a
      variant + audit_log entry, and optionally fires a one-app Winget
      manifest scrape.

The dry-run review dialog (MonitorPlanDialog) is the "prompt" surface:
even auto-matched rows are editable (change target app, switch to
create-new, or skip) before a single byte moves.

Compression backends, tiered (matching scanner.py's "pure-Python first,
external binary fallback" pattern):
  - 7z  -> py7zr if importable, else external `7z`/`7za` binary,
           else falls back to zip with the fallback noted per-item.
  - zip -> stdlib zipfile (always available).
  - rar -> external `rar` binary only (rarely present -- falls back to
           7z then zip, noting the fallback in the report).

Archive filename = original filename with the extension swapped to the
chosen archive format ("setup_v2.1.exe" -> "setup_v2.1.7z"). Already-
compressed inputs (.zip/.rar/.7z/.iso/.tar/.gz/.tgz/.cab) are moved
as-is.

Safety, mirroring app_manager.execute_reorganize():
  - destination collisions are ALWAYS skipped, never overwritten,
    rechecked at execute time even if the preview said otherwise
  - each item is wrapped individually: one failure is recorded and
    skipped rather than aborting the batch
  - a JSON move log is written incrementally next to catalog.db so a
    crash mid-run still leaves a complete record for audit/undo
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import subprocess
import time
import traceback
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, QWaitCondition, QMutex, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from rapidfuzz import fuzz

from app_paths import get_logs_dir
from database import Database
from resolver import extract_fields, normalize_key
from app_manager import _safe_path_component
from gui_backend import AppPickerDialog

log = logging.getLogger("appcatalog.monitor")


# ======================================================================
# Defaults if a setting is missing (an old DB without the monitor keys)
# ======================================================================
_DEFAULT_MONITOR_EXT = [
    ".exe", ".msi",
    ".zip", ".rar", ".7z", ".iso", ".tar", ".gz", ".tgz", ".cab",
]
_DEFAULT_ALREADY_COMPRESSED = [
    ".zip", ".rar", ".7z", ".iso", ".tar", ".gz", ".tgz", ".cab",
    ".bz2", ".xz",
]
_DEFAULT_SKIP_PARTIAL = [
    ".crdownload", ".part", ".tmp", ".partial", ".!ut", ".aria2", ".downloading",
]

# Archive extensions we know how to write
_ARCHIVE_EXTS = {"7z": ".7z", "zip": ".zip", "rar": ".rar"}


# ======================================================================
# Data structures
# ======================================================================
@dataclass
class MonitorPlanItem:
    """One candidate file + its proposed match. Filled by the PLAN phase,
    possibly edited by the user via MonitorPlanDialog, consumed by the
    EXECUTE phase."""
    source_path: str
    file_name: str
    file_size: int
    extension: str  # lowercase, includes leading dot

    # From resolver.extract_fields()
    extracted_name: str = ""
    extracted_version: str = ""
    extracted_edition: str = ""
    extracted_architecture: str = ""
    normalized_key: str = ""

    # Match proposal (from PLAN phase)
    match_status: str = "none"              # "exact" | "fuzzy" | "none"
    matched_app_id: Optional[int] = None
    matched_app_name: Optional[str] = None
    matched_app_catalog: Optional[str] = None
    matched_app_subcatalog: Optional[str] = None
    fuzzy_candidates: list[dict] = field(default_factory=list)
    # each candidate: {"id", "name", "catalog", "subcatalog", "score"}

    # User-editable action (defaults chosen by the PLAN phase, possibly
    # rewritten by the dialog before EXECUTE runs)
    action: str = "attach"                  # "attach" | "create_new" | "skip"
    new_name: str = ""
    new_catalog: str = ""
    new_subcatalog: str = ""


@dataclass
class MonitorResultItem:
    source_path: str
    file_name: str
    outcome: str            # "moved" | "copied" | "skipped_collision"
                            # | "skipped_user" | "failed"
    app_id: Optional[int] = None
    app_name: Optional[str] = None
    dest_path: Optional[str] = None
    archive_created: bool = False
    archive_backend: Optional[str] = None   # "7z" / "zip" / "rar" / "move-as-is"
    error: Optional[str] = None
    note: Optional[str] = None              # e.g. "7z unavailable, used zip"


@dataclass
class MonitorResult:
    items: list[MonitorResultItem] = field(default_factory=list)
    move_log_path: Optional[str] = None
    cancelled: bool = False
    html_report_path: Optional[str] = None  # checkpoint 21

    @property
    def moved(self) -> int:
        return sum(1 for i in self.items if i.outcome in ("moved", "copied"))

    @property
    def skipped(self) -> int:
        return sum(1 for i in self.items if i.outcome.startswith("skipped"))

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i.outcome == "failed")


# ======================================================================
# Phase 1 -- PLAN
# ======================================================================
def _is_partial_download(file_name: str, settings: dict) -> bool:
    skip = settings.get("monitor_skip_partial_extensions") or _DEFAULT_SKIP_PARTIAL
    lower = file_name.lower()
    return any(lower.endswith(ext) for ext in skip)


def _is_candidate(entry: os.DirEntry, settings: dict) -> tuple[bool, str]:
    """Returns (is_candidate, reason_if_not)."""
    if not entry.is_file():
        return False, "not a file"
    name = entry.name
    lower = name.lower()
    if _is_partial_download(name, settings):
        return False, "partial download marker"
    ext = os.path.splitext(lower)[1]
    allowed = [e.lower() for e in
               (settings.get("monitor_extensions") or _DEFAULT_MONITOR_EXT)]
    if ext not in allowed:
        return False, f"extension '{ext}' not in monitor_extensions"
    try:
        size = entry.stat().st_size
    except OSError as e:
        return False, f"stat failed: {e}"
    min_mb = float(settings.get("monitor_min_size_mb", 1))
    if size < min_mb * 1024 * 1024:
        return False, f"size {size} below min {min_mb} MB"
    return True, ""


def _is_settled(path: str, settings: dict) -> bool:
    settle = float(settings.get("monitor_settle_seconds", 3))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return (time.time() - mtime) >= settle


def _lookup_app_by_key(db: Database, key: str) -> Optional[dict]:
    if not key:
        return None
    row = db.connect().execute(
        "SELECT id, name, catalog, subcatalog FROM apps "
        "WHERE normalized_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def _fuzzy_candidates(db: Database, key: str, threshold: int,
                       limit: int = 5) -> list[dict]:
    if not key:
        return []
    rows = db.connect().execute(
        "SELECT id, name, catalog, subcatalog, normalized_key FROM apps "
        "WHERE normalized_key IS NOT NULL AND normalized_key != ''"
    ).fetchall()
    scored = []
    for r in rows:
        score = fuzz.token_sort_ratio(key, r["normalized_key"])
        if score >= threshold:
            scored.append({
                "id": r["id"], "name": r["name"],
                "catalog": r["catalog"], "subcatalog": r["subcatalog"],
                "score": score,
            })
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


def _build_plan_item(db: Database, source_path: str,
                      settings: dict) -> Optional[MonitorPlanItem]:
    file_name = os.path.basename(source_path)
    ext = os.path.splitext(file_name.lower())[1]
    try:
        size = os.path.getsize(source_path)
    except OSError:
        return None

    # Run the same extraction pipeline the scanner uses, so a monitored
    # filename resolves to the same name/version/edition a folder scan
    # would have produced for the same file.
    #
    # Signature note: extract_fields wants folder_name / primary_file_name
    # / pe_* as POSITIONAL-shaped fields (they can be None but the names
    # matter), and it returns ExtractedFields whose name lives on
    # `.clean_name` -- NOT `.name`.
    try:
        fields = extract_fields(
            folder_name="",
            primary_file_name=file_name,
            pe_product_name=None,
            pe_product_version=None,
            pe_file_version=None,
            settings=settings,
            parent_folder_name=None,
            folder_depth=0,
        )
    except Exception as e:
        log.warning("extract_fields failed for %s: %s", source_path, e)
        fields = None

    name = (getattr(fields, "clean_name", "") or "").strip() if fields else ""
    if not name:
        name = os.path.splitext(file_name)[0]

    item = MonitorPlanItem(
        source_path=source_path,
        file_name=file_name,
        file_size=size,
        extension=ext,
        extracted_name=name,
        extracted_version=(getattr(fields, "version", "") or ""),
        extracted_edition=(getattr(fields, "edition", "") or ""),
        extracted_architecture=(getattr(fields, "architecture", "") or ""),
        normalized_key=normalize_key(name),
    )

    exact = _lookup_app_by_key(db, item.normalized_key)
    if exact is not None:
        item.match_status = "exact"
        item.matched_app_id = exact["id"]
        item.matched_app_name = exact["name"]
        item.matched_app_catalog = exact["catalog"]
        item.matched_app_subcatalog = exact["subcatalog"]
        item.action = "attach"
        return item

    threshold = int(settings.get("fuzzy_match_threshold", 88))
    candidates = _fuzzy_candidates(db, item.normalized_key, threshold)
    if candidates:
        item.match_status = "fuzzy"
        item.fuzzy_candidates = candidates
        item.matched_app_id = candidates[0]["id"]
        item.matched_app_name = candidates[0]["name"]
        item.matched_app_catalog = candidates[0]["catalog"]
        item.matched_app_subcatalog = candidates[0]["subcatalog"]
        item.action = "attach"
        return item

    item.match_status = "none"
    item.action = "create_new"
    item.new_name = name
    item.new_catalog = ""
    item.new_subcatalog = ""
    return item


def scan_monitor_folders(db: Database, folders: list[str], settings: dict,
                          on_progress=None,
                          cancel_flag=None) -> list[MonitorPlanItem]:
    """Pure read-only pass. Walks every folder, builds a plan item for
    each candidate file. Nothing on disk or in the DB is touched."""
    items: list[MonitorPlanItem] = []
    for folder in folders:
        if cancel_flag and cancel_flag():
            break
        if not os.path.isdir(folder):
            log.warning("Monitor folder does not exist: %s", folder)
            continue
        try:
            entries = list(os.scandir(folder))
        except OSError as e:
            log.warning("Cannot read monitor folder %s: %s", folder, e)
            continue
        for entry in entries:
            if cancel_flag and cancel_flag():
                break
            ok, _reason = _is_candidate(entry, settings)
            if not ok:
                continue
            if not _is_settled(entry.path, settings):
                log.info("Skipping unsettled file: %s", entry.path)
                continue
            item = _build_plan_item(db, entry.path, settings)
            if item is not None:
                items.append(item)
                if on_progress:
                    on_progress(len(items), entry.path)
    return items


# ======================================================================
# Phase 2 -- EXECUTE
# ======================================================================
def _safe_move_or_copy(src: str, dest: str, move_mode: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if move_mode == "copy":
        shutil.copy2(src, dest)
    else:
        shutil.move(src, dest)


def _compress_zip(src: str, dest: str) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(src, arcname=os.path.basename(src))
        return True, "zip"
    except Exception as e:
        return False, str(e)


def _compress_7z(src: str, dest: str) -> tuple[bool, str]:
    try:
        import py7zr  # noqa: WPS433 -- lazy, optional dependency
        with py7zr.SevenZipFile(dest, "w") as z:
            z.write(src, arcname=os.path.basename(src))
        return True, "7z"
    except ImportError:
        pass
    except Exception as e:
        log.warning("py7zr failed for %s: %s", src, e)
    for candidate in ("7z", "7za", "7z.exe"):
        exe = shutil.which(candidate)
        if not exe:
            continue
        try:
            subprocess.run([exe, "a", "-t7z", dest, src],
                            check=True, capture_output=True, timeout=1800)
            return True, "7z"
        except Exception as e:
            return False, str(e)
    return False, "no 7z backend available (py7zr not installed, 7z not on PATH)"


def _compress_rar(src: str, dest: str) -> tuple[bool, str]:
    exe = shutil.which("rar") or shutil.which("rar.exe")
    if not exe:
        return False, "rar binary not on PATH"
    try:
        subprocess.run([exe, "a", "-m5", dest, src],
                        check=True, capture_output=True, timeout=1800)
        return True, "rar"
    except Exception as e:
        return False, str(e)


def _compress(src: str, dest_without_ext: str, fmt: str
               ) -> tuple[Optional[str], str, Optional[str]]:
    """Returns (final_dest_path_or_None, backend_used, note). Tries the
    requested format first; on failure falls back through the others
    with the fallback noted for the report."""
    fmt = fmt.lower()
    if fmt == "7z":
        attempts = [("7z", _compress_7z, ".7z"),
                    ("zip", _compress_zip, ".zip")]
    elif fmt == "rar":
        attempts = [("rar", _compress_rar, ".rar"),
                    ("7z", _compress_7z, ".7z"),
                    ("zip", _compress_zip, ".zip")]
    else:
        attempts = [("zip", _compress_zip, ".zip")]

    for backend_name, fn, real_ext in attempts:
        candidate = dest_without_ext + real_ext
        ok, info = fn(src, candidate)
        if ok:
            note = None if backend_name == fmt else \
                f"{fmt} unavailable, used {backend_name}"
            return candidate, backend_name, note
        log.warning("Compression attempt %s failed for %s: %s",
                    backend_name, src, info)
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError:
                pass

    return None, "failed", "all compression backends failed"


def _resolve_destination(db: Database, app_id: int, item: MonitorPlanItem,
                          dest_root: str, settings: dict
                          ) -> tuple[str, str]:
    """Returns (dest_folder, dest_basename) -- dest_basename already
    includes the correct extension (original for already-compressed
    inputs, the chosen archive extension for compressed ones)."""
    row = db.connect().execute(
        "SELECT name, catalog, subcatalog FROM apps WHERE id = ?",
        (app_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"app_id {app_id} no longer exists")
    catalog = _safe_path_component(row["catalog"] or "Uncategorized")
    subcatalog = _safe_path_component(row["subcatalog"] or "Misc")
    app_name = _safe_path_component(row["name"])
    version = _safe_path_component(item.extracted_version or "unknown-version")
    dest_folder = os.path.join(dest_root, catalog, subcatalog, app_name, version)

    already = (item.extension in
               [e.lower() for e in
                (settings.get("monitor_already_compressed_extensions")
                 or _DEFAULT_ALREADY_COMPRESSED)])
    if already:
        return dest_folder, item.file_name
    stem = os.path.splitext(item.file_name)[0]
    fmt = (settings.get("monitor_archive_format") or "7z").lower()
    ext = _ARCHIVE_EXTS.get(fmt, ".7z")
    return dest_folder, stem + ext


def _create_new_app(db: Database, item: MonitorPlanItem) -> int:
    """Creates a new app with status='needs_review' (user-created, no
    scan history behind it, so it lands in the existing review workflow)."""
    name = (item.new_name or item.extracted_name or item.file_name).strip()
    catalog = (item.new_catalog or "").strip()
    subcatalog = (item.new_subcatalog or "").strip()
    conn = db.connect()
    settings_version = db.current_settings_version()
    cur = conn.execute(
        """INSERT INTO apps (name, catalog, subcatalog, status, confidence,
                              normalized_key, name_locked,
                              resolved_with_settings_version)
           VALUES (?, ?, ?, 'needs_review', 1.0, ?, 1, ?)""",
        (name, catalog, subcatalog, normalize_key(name), settings_version),
    )
    app_id = cur.lastrowid
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) "
        "VALUES (?,?,?,?)",
        ("app", app_id, "monitor_create",
         json.dumps({"source": item.source_path, "name": name,
                     "catalog": catalog, "subcatalog": subcatalog})),
    )
    conn.commit()
    return app_id

def _sync_monitor_app_tags(db: Database, app_id: int,
                            catalog: Optional[str], subcatalog: Optional[str]) -> None:
    """Mirrors resolver._sync_app_tags -- catalog/subcatalog become tags
    on the app, so the Tags column isn't blank on monitor-created apps."""
    conn = db.connect()
    for name in (catalog, subcatalog):
        if not name or not name.strip():
            continue
        name = name.strip()
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        tag_id = row["id"] if row else \
            conn.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)",
            (app_id, tag_id),
        )
    conn.commit()


def _record_variant(db: Database, app_id: int, item: MonitorPlanItem,
                    dest_path: str, raw_source: str) -> None:
    """Records a variant for the moved file.

    Column set mirrors resolver._upsert_variant's INSERT as closely as a
    monitor-originated variant can: monitored files never entered
    raw_candidates, so raw_candidate_id is left NULL. `name_source` is
    stamped 'monitor' so these variants are distinguishable in the
    variants table from resolver-produced ones.
    """
    conn = db.connect()
    conn.execute(
        """INSERT INTO variants (app_id, version, edition, architecture,
                                  source_path, file_type, file_size,
                                  confidence, updated_at, file_name,
                                  name_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 'monitor')""",
        (app_id,
         item.extracted_version or None,
         item.extracted_edition or None,
         item.extracted_architecture or None,
         os.path.dirname(dest_path),
         item.extension.lstrip("."),
         item.file_size,
         0.9,   # monitored files are user-confirmed: high confidence
         os.path.basename(dest_path)),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) "
        "VALUES (?,?,?,?)",
        ("app", app_id, "monitor_attach",
         json.dumps({"source": raw_source, "dest": dest_path,
                     "version": item.extracted_version})),
    )
    conn.commit()


def execute_monitor_plan(db: Database, plan: list[MonitorPlanItem],
                          dest_root: str, move_mode: str = "move",
                          settings: Optional[dict] = None,
                          on_progress=None, cancel_flag=None,
                          move_log_dir: Optional[str] = None) -> MonitorResult:
    """Executes EXACTLY the plan handed in. Never recomputes matches or
    destinations -- the user reviewed (and possibly edited) this exact
    plan, and we honour it."""
    settings = settings or db.get_all_settings()
    result = MonitorResult()

    # checkpoint 21: logs live in logs/ next to catalog.db (see app_paths.py)
    log_dir = Path(move_log_dir) if move_log_dir else get_logs_dir(db.path)
    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = log_dir / f"monitor_log_{run_stamp}.json"
    result.move_log_path = str(log_path)
    entries: list[dict] = []

    def _flush():
        try:
            log_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not write monitor move log: %s", e)

    # Apps created DURING THIS RUN, keyed by normalize_key(new_name). If
    # two files both need "create new" with the same name (e.g. two
    # versions of a brand-new app downloaded in the same batch), the
    # second one attaches to the app the first one just created rather
    # than creating a duplicate.
    created_in_this_run: dict[str, int] = {}

    # Every app that got a variant attached/created this run -- drives a
    # single batched post-run scrape (see the "Batched post-run scrape"
    # block after the loop) instead of one scrape call per item inline.
    touched_app_ids: set[int] = set()

    for idx, item in enumerate(plan):
        if cancel_flag and cancel_flag():
            result.cancelled = True
            break

        entry = {
            "source": item.source_path, "file_name": item.file_name,
            "action": item.action, "timestamp": datetime.now().isoformat(),
            "status": "pending",
        }
        # checkpoint 21: resolved up front (best-effort, works even if the
        # try block below fails before app_id is known) -- MonitorResultItem
        # .app_name was never actually being populated anywhere, so both
        # the in-app report table and the new HTML report would have shown
        # every single row as blank/"(new app)", including successful
        # attaches to a perfectly well-known existing app.
        resolved_app_name = (
            (item.new_name or item.extracted_name or item.file_name).strip()
            if item.action == "create_new"
            else (item.matched_app_name or item.extracted_name or item.file_name)
        )
        entry["app_name"] = resolved_app_name
        entries.append(entry)
        _flush()

        if item.action == "skip":
            entry["status"] = "skipped_user"
            result.items.append(MonitorResultItem(
                source_path=item.source_path, file_name=item.file_name,
                outcome="skipped_user", app_name=resolved_app_name,
            ))
            _flush()
            if on_progress:
                on_progress(idx + 1, len(plan), item)
            continue
        try:
            if item.action == "create_new":
                new_name = (item.new_name or item.extracted_name or item.file_name).strip()
                key = normalize_key(new_name)
                if key in created_in_this_run:
                    # Same app already created earlier in this run --
                    # attach to it instead of duplicating.
                    app_id = created_in_this_run[key]
                else:
                    app_id = _create_new_app(db, item)
                    created_in_this_run[key] = app_id
                    _sync_monitor_app_tags(db, app_id,
                                            item.new_catalog, item.new_subcatalog)
            else:
                app_id = item.matched_app_id
                if app_id is None:
                    raise RuntimeError("attach action with no matched_app_id")

            dest_folder, dest_basename = _resolve_destination(
                db, app_id, item, dest_root, settings)
            dest_path = os.path.join(dest_folder, dest_basename)

            if os.path.exists(dest_path):
                entry["status"] = "skipped_collision"
                entry["dest"] = dest_path
                result.items.append(MonitorResultItem(
                    source_path=item.source_path, file_name=item.file_name,
                    outcome="skipped_collision", app_id=app_id,
                    app_name=resolved_app_name,
                    dest_path=dest_path,
                    error="destination already exists",
                ))
                _flush()
                if on_progress:
                    on_progress(idx + 1, len(plan), item)
                continue

            already = (item.extension in
                       [e.lower() for e in
                        (settings.get("monitor_already_compressed_extensions")
                         or _DEFAULT_ALREADY_COMPRESSED)])
            archive_created = False
            archive_backend = "move-as-is"
            note = None

            if already:
                _safe_move_or_copy(item.source_path, dest_path, move_mode)
            else:
                os.makedirs(dest_folder, exist_ok=True)
                stem_no_ext = os.path.splitext(dest_path)[0]
                fmt = (settings.get("monitor_archive_format") or "7z").lower()
                final_path, backend, note = _compress(
                    item.source_path, stem_no_ext, fmt)
                if final_path is None:
                    raise RuntimeError(note or "compression failed")
                dest_path = final_path
                archive_created = True
                archive_backend = backend
                if move_mode == "move":
                    try:
                        os.remove(item.source_path)
                    except OSError as e:
                        log.warning(
                            "Compressed OK but couldn't delete source %s: %s",
                            item.source_path, e)

            _record_variant(db, app_id, item, dest_path, item.source_path)

            outcome = "copied" if move_mode == "copy" else "moved"
            entry["status"] = outcome
            entry["dest"] = dest_path
            entry["archive_backend"] = archive_backend
            result.items.append(MonitorResultItem(
                source_path=item.source_path, file_name=item.file_name,
                outcome=outcome, app_id=app_id, app_name=resolved_app_name, dest_path=dest_path,
                archive_created=archive_created,
                archive_backend=archive_backend, note=note,
            ))
            _flush()

            # Collect, don't scrape yet -- see the batched call after the
            # loop. Scraping inline here would block this item's progress
            # for 5-30 s per app on a cold manifest cache.
            touched_app_ids.add(app_id)

        except Exception as e:
            log.exception("Monitor execute failed for %s", item.source_path)
            entry["status"] = "failed"
            entry["error"] = str(e)
            result.items.append(MonitorResultItem(
                source_path=item.source_path, file_name=item.file_name,
                outcome="failed", app_name=resolved_app_name, error=str(e),
            ))
            _flush()

        if on_progress:
            on_progress(idx + 1, len(plan), item)

    # Batched post-run scrape: one run_scrape() call for every app that
    # was attached to or created this run, instead of one call per item
    # inline in the loop. The earlier per-item version stalled the
    # execute loop by 5-30 s per app whenever the Winget manifest cache
    # needed a refresh; batching lets the loop run at full speed and
    # makes the scrape one final, visible step at the end.
    if settings.get("monitor_auto_scrape_on_attach", True) and touched_app_ids:
        try:
            from scraper import run_scrape
            run_scrape(db, app_ids=sorted(touched_app_ids))
        except Exception as e:
            log.warning(
                "Post-attach batch scrape failed for %d app(s): %s",
                len(touched_app_ids), e,
            )

    try:
        result.html_report_path = generate_monitor_html_report(result, move_mode=move_mode, report_dir=log_dir)
    except OSError as e:
        log.warning("Could not write HTML monitor report: %s", e)

    return result


def generate_monitor_html_report(
    result: MonitorResult, *, move_mode: str,
    report_dir: Optional[Path] = None, title: str = "Monitor Folders Report",
) -> str:
    """Same idea as app_manager.generate_reorganize_html_report -- see
    html_report.py's module docstring for why this exists (checkpoint 21)."""
    from html_report import ReportRow, render_operation_html_report

    report_dir = Path(report_dir) if report_dir else (
        Path(os.path.dirname(result.move_log_path)) if result.move_log_path else Path(".")
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = report_dir / f"monitor_report_{stamp}.html"

    _STATUS_MAP = {
        "moved": ("Moved", "good"), "copied": ("Copied", "good"),
        "failed": ("Failed", "bad"),
        "skipped_collision": ("Skipped (exists)", "warn"),
        "skipped_user": ("Skipped (by you)", "neutral"),
    }

    def _detail(it: MonitorResultItem) -> str:
        bits = []
        if it.outcome == "failed" and it.error:
            bits.append(it.error)
        if it.note:
            bits.append(it.note)
        if it.archive_created and it.archive_backend:
            bits.append(f"archived via {it.archive_backend}")
        return "; ".join(bits)

    rows = [
        ReportRow(
            name=it.app_name or "(new app)",
            source=it.source_path, dest=it.dest_path or "",
            status_label=_STATUS_MAP.get(it.outcome, (it.outcome, "neutral"))[0],
            status_class=_STATUS_MAP.get(it.outcome, (it.outcome, "neutral"))[1],
            detail=_detail(it),
        )
        for it in result.items
    ]

    html = render_operation_html_report(
        title=title,
        subtitle=f"Mode: {move_mode}",
        summary_cards=[
            ("Total planned", len(result.items), "neutral"),
            ("Succeeded", result.moved, "good"),
            ("Skipped", result.skipped, "warn" if result.skipped else "good"),
            ("Failed", result.failed, "bad" if result.failed else "good"),
        ],
        rows=rows,
        json_log_path=result.move_log_path,
    )
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


# ======================================================================
# GUI -- dialogs (private to this module; the GUI never touches these)
# ======================================================================
class _MonitorStartDialog(QDialog):
    """Pre-run config. Seeded from settings; the values picked here are
    used for THIS run only (the user can persist them in Settings >
    Monitor)."""

    def __init__(self, db: Database, settings: dict, parent=None):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.folders: list[str] = []
        self.dest_root: str = ""
        self.move_mode: str = "move"
        self.archive_format: str = "7z"

        self.setWindowTitle("Start monitoring")
        self.resize(620, 480)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "<b>Folders to monitor</b>. New .exe / .msi / archive files "
            "dropped into any of these folders will be picked up. Files "
            "still being written are ignored (partial-download markers + "
            "a size-settle delay)."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.folders_table = QTableWidget(0, 1)
        self.folders_table.setHorizontalHeaderLabels(["Folder"])
        self.folders_table.horizontalHeader().setStretchLastSection(True)
        self.folders_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.folders_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.folders_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.folders_table.setMinimumHeight(120)
        for f in settings.get("monitor_folders", []):
            self._append_folder_row(f)
        layout.addWidget(self.folders_table)

        folder_btns = QHBoxLayout()
        add_btn = QPushButton("Add folder…")
        add_btn.clicked.connect(self._add_folder)
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self._remove_folders)
        folder_btns.addWidget(add_btn)
        folder_btns.addWidget(del_btn)
        folder_btns.addStretch()
        layout.addLayout(folder_btns)

        form = QFormLayout()

        dest_row = QHBoxLayout()
        self.dest_combo = QComboBox()
        self.dest_combo.setEditable(True)
        self.dest_combo.setMinimumWidth(300)
        roots = db.connect().execute(
            "SELECT path FROM scan_roots ORDER BY path").fetchall()
        root_paths = [r["path"] for r in roots]
        if root_paths:
            self.dest_combo.addItems(root_paths)
        else:
            self.dest_combo.addItem("")
        dest_row.addWidget(self.dest_combo, stretch=1)
        dest_browse = QPushButton("Browse…")
        dest_browse.clicked.connect(self._browse_dest)
        dest_row.addWidget(dest_browse)
        dest_widget = QWidget()
        dest_widget.setLayout(dest_row)
        form.addRow("Destination root:", dest_widget)

        self.move_mode_combo = QComboBox()
        self.move_mode_combo.addItems([
            "Move (source is deleted after a successful archive+placement)",
            "Copy (source folder keeps the original file)",
        ])
        if settings.get("monitor_move_mode", "move") == "copy":
            self.move_mode_combo.setCurrentIndex(1)
        form.addRow("Mode:", self.move_mode_combo)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["7z", "zip", "rar"])
        default_fmt = settings.get("monitor_archive_format", "7z")
        idx = self.format_combo.findText(default_fmt)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        self.format_combo.setToolTip(
            "7z needs py7zr or a 7z binary on PATH; rar needs a rar binary.\n"
            "If the chosen backend isn't available, the run falls back to zip\n"
            "and the per-item report notes the fallback."
        )
        form.addRow("Archive format:", self.format_combo)

        layout.addLayout(form)

        note = QLabel(
            "Already-compressed inputs (.zip/.rar/.7z/.iso/.tar/.gz/.cab) "
            "are moved as-is rather than re-compressed. Configure the exact "
            "list in Settings > Monitor."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        start_btn = QPushButton("Scan folders…")
        start_btn.clicked.connect(self._start)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(start_btn)
        layout.addLayout(btn_row)

    def _start(self):
        folders = self._current_folders()
        if not folders:
            QMessageBox.warning(self, "No folders",
                                 "Add at least one folder to monitor.")
            return
        missing = [f for f in folders if not os.path.isdir(f)]
        if missing:
            QMessageBox.warning(
                self, "Folder not found",
                "These folders don't exist (or aren't readable):\n\n"
                + "\n".join(missing),
            )
            return
        dest = self.dest_combo.currentText().strip()
        if not dest:
            QMessageBox.warning(self, "No destination",
                                 "Choose a destination root for the organized files.")
            return
        if not os.path.isdir(dest):
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(self, "Cannot create destination",
                                     f"{dest}\n\n{e}")
                return

        self.folders = folders
        self.dest_root = dest
        self.move_mode = "copy" if self.move_mode_combo.currentIndex() == 1 else "move"
        self.archive_format = self.format_combo.currentText()
        self.accept()

    def _append_folder_row(self, folder: str):
        row = self.folders_table.rowCount()
        self.folders_table.insertRow(row)
        self.folders_table.setItem(row, 0, QTableWidgetItem(folder))

    def _current_folders(self) -> list[str]:
        out = []
        for r in range(self.folders_table.rowCount()):
            item = self.folders_table.item(r, 0)
            if item and item.text().strip():
                out.append(item.text().strip())
        return out

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to monitor")
        if not folder:
            return
        if folder in self._current_folders():
            return
        self._append_folder_row(folder)

    def _remove_folders(self):
        rows = sorted(
            {idx.row() for idx in self.folders_table.selectedIndexes()},
            reverse=True,
        )
        if not rows:
            return
        for r in rows:
            self.folders_table.removeRow(r)

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose destination root")
        if folder:
            self.dest_combo.setCurrentText(folder)

class _MonitorPlanDialog(QDialog):
    """Editable dry-run review. Every row is a proposed action the user
    can override before a single byte moves."""

    def __init__(self, db: Database, plan: list, parent=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self._rows: list[dict] = []
        self.setWindowTitle("Monitor — review plan (nothing has moved yet)")
        self.resize(1180, 640)
        self._load_reference_data()
        self._build_ui()

    def _load_reference_data(self):
        conn = self.db.connect()
        self._all_apps = [dict(r) for r in conn.execute(
            "SELECT id, name, catalog, subcatalog FROM apps ORDER BY name"
        ).fetchall()]
        self._all_catalogs = [r["catalog"] for r in conn.execute(
            "SELECT DISTINCT catalog FROM apps "
            "WHERE catalog IS NOT NULL AND catalog != '' ORDER BY catalog"
        ).fetchall()]
        self._all_subcats = [r["subcatalog"] for r in conn.execute(
            "SELECT DISTINCT subcatalog FROM apps "
            "WHERE subcatalog IS NOT NULL AND subcatalog != '' ORDER BY subcatalog"
        ).fetchall()]

    def _build_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"<b>{len(self.plan)} file(s)</b> to process. Review and adjust "
            "each row, then click <b>Execute plan</b> to move/compress them. "
            "Nothing on disk changes until you click Execute."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(self.plan), 8)
        self.table.setHorizontalHeaderLabels([
            "File", "Extracted", "Status", "Action", "Target App",
            "New Name", "Catalog", "Subcatalog",
        ])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        for i, item in enumerate(self.plan):
            self._build_row(i, item)

        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, stretch=1)

        self.summary_label = QLabel()
        layout.addWidget(self.summary_label)

        btn_row = QHBoxLayout()
        mark_all_skip = QPushButton("Skip all")
        mark_all_skip.clicked.connect(lambda: self._set_all_actions(2))
        mark_all_attach = QPushButton("Attach all matched")
        mark_all_attach.clicked.connect(
            lambda: self._set_all_actions(0, only_matched=True))
        btn_row.addWidget(mark_all_skip)
        btn_row.addWidget(mark_all_attach)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel run")
        cancel_btn.clicked.connect(self.reject)
        self.execute_btn = QPushButton("Execute plan")
        self.execute_btn.clicked.connect(self._execute)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.execute_btn)
        layout.addLayout(btn_row)

        self._update_summary()

    def _build_row(self, row_idx: int, item: MonitorPlanItem):
        fitem = QTableWidgetItem(item.file_name)
        fitem.setToolTip(f"{item.source_path}\n"
                         f"Size: {item.file_size:,} bytes\n"
                         f"Extension: {item.extension}")
        self.table.setItem(row_idx, 0, fitem)

        self.table.setItem(row_idx, 1, QTableWidgetItem(item.extracted_name))

        if item.match_status == "exact":
            status_text = "exact match"
            status_color = None
        elif item.match_status == "fuzzy":
            top = item.fuzzy_candidates[0] if item.fuzzy_candidates else None
            status_text = f"fuzzy ({top['score']:.0f}%)" if top else "fuzzy"
            status_color = QColor(180, 120, 0)
        else:
            status_text = "no match"
            status_color = QColor(180, 60, 0)
        sitem = QTableWidgetItem(status_text)
        if status_color:
            sitem.setForeground(status_color)
        self.table.setItem(row_idx, 2, sitem)

        action_combo = QComboBox()
        action_combo.addItems(["Attach to existing", "Create new app", "Skip"])
        if item.action == "skip":
            action_combo.setCurrentIndex(2)
        elif item.action == "create_new":
            action_combo.setCurrentIndex(1)
        else:
            action_combo.setCurrentIndex(0)
        self.table.setCellWidget(row_idx, 3, action_combo)

        # Target app: a combo for quick picks from nearby options, plus a
        # "…" button that opens the shared AppPickerDialog for searching
        # the full app list by name/catalog/subcatalog. The combo stays
        # because auto-matched rows come pre-filled and a user only
        # changing the picked neighbour shouldn't need to open a modal;
        # the button is for when the list is long and searching beats
        # scrolling.
        target_container = QWidget()
        target_row = QHBoxLayout(target_container)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(2)

        target_combo = QComboBox()
        target_combo.setEditable(True)
        target_combo.addItem("(choose app…)", None)
        for a in self._all_apps:
            target_combo.addItem(f"{a['name']}  (#{a['id']})", a["id"])
        if item.matched_app_id:
            idx = target_combo.findData(item.matched_app_id)
            if idx >= 0:
                target_combo.setCurrentIndex(idx)
        target_row.addWidget(target_combo, stretch=1)

        target_pick_btn = QPushButton("…")
        target_pick_btn.setFixedWidth(28)
        target_pick_btn.setToolTip("Search for an app…")
        target_row.addWidget(target_pick_btn)

        self.table.setCellWidget(row_idx, 4, target_container)

        name_edit = QLineEdit(item.new_name or item.extracted_name or "")
        self.table.setCellWidget(row_idx, 5, name_edit)

        catalog_combo = QComboBox()
        catalog_combo.setEditable(True)
        catalog_combo.addItems(self._all_catalogs)
        catalog_combo.setCurrentText(
            item.new_catalog or item.matched_app_catalog or "")
        self.table.setCellWidget(row_idx, 6, catalog_combo)

        subcat_combo = QComboBox()
        subcat_combo.setEditable(True)
        subcat_combo.addItems(self._all_subcats)
        subcat_combo.setCurrentText(
            item.new_subcatalog or item.matched_app_subcatalog or "")
        self.table.setCellWidget(row_idx, 7, subcat_combo)

        refs = {
            "item": item,
            "action": action_combo,
            "target": target_combo,
            "new_name": name_edit,
            "catalog": catalog_combo,
            "subcatalog": subcat_combo,
        }
        self._rows.append(refs)

        def _on_action_changed(_idx, r=refs):
            self._apply_row_enabled(r)
            self._update_summary()
        action_combo.currentIndexChanged.connect(_on_action_changed)

        # On attach rows, changing the target quietly refreshes the
        # catalog/subcat fields from the newly-picked app. Skipped in
        # create-new mode so a user's manual edits survive.
        def _on_target_changed(_idx, r=refs):
            if r["action"].currentIndex() != 0:
                return
            app_id = r["target"].currentData()
            if app_id is None:
                return
            row = self.db.connect().execute(
                "SELECT catalog, subcatalog FROM apps WHERE id = ?",
                (app_id,)).fetchone()
            if row:
                r["catalog"].setCurrentText(row["catalog"] or "")
                r["subcatalog"].setCurrentText(row["subcatalog"] or "")
        target_combo.currentIndexChanged.connect(_on_target_changed)

        # Opens the shared AppPickerDialog (same dialog the main window's
        # "Move to different app…" uses). The monitor's cached
        # self._all_apps was populated at dialog construction time, so
        # an app that didn't exist then won't be in the combo -- handle
        # that case by fetching it fresh and appending on the fly.
        def _pick_via_dialog(_checked=False, r=refs):
            dlg = AppPickerDialog(self.db, parent=self,
                                   title="Select target app")
            if not dlg.exec():
                return
            app_id = dlg.selected_app_id()
            if app_id is None:
                return
            idx = r["target"].findData(app_id)
            if idx < 0:
                row = self.db.connect().execute(
                    "SELECT name, catalog, subcatalog FROM apps WHERE id = ?",
                    (app_id,),
                ).fetchone()
                if row is None:
                    return
                r["target"].addItem(f"{row['name']}  (#{app_id})", app_id)
                idx = r["target"].findData(app_id)
            if idx >= 0:
                r["target"].setCurrentIndex(idx)
                # setCurrentIndex fires currentIndexChanged, which routes
                # through _on_target_changed and refreshes the
                # catalog/subcatalog fields from the newly-picked app.
        target_pick_btn.clicked.connect(_pick_via_dialog)

        self._apply_row_enabled(refs)

    def _apply_row_enabled(self, refs: dict):
        mode = refs["action"].currentIndex()
        refs["target"].setEnabled(mode == 0)
        refs["new_name"].setEnabled(mode == 1)
        refs["catalog"].setEnabled(mode == 1)
        refs["subcatalog"].setEnabled(mode == 1)

    def _set_all_actions(self, mode_idx: int, only_matched: bool = False):
        for refs in self._rows:
            if only_matched and refs["item"].match_status == "none":
                continue
            refs["action"].setCurrentIndex(mode_idx)
        self._update_summary()

    def _update_summary(self):
        attach = sum(1 for r in self._rows if r["action"].currentIndex() == 0)
        create = sum(1 for r in self._rows if r["action"].currentIndex() == 1)
        skip = sum(1 for r in self._rows if r["action"].currentIndex() == 2)
        self.summary_label.setText(
            f"Attach: <b>{attach}</b>  |  "
            f"Create new: <b>{create}</b>  |  "
            f"Skip: <b>{skip}</b>"
        )

    def _execute(self):
        # Warn (but don't block) if any "create new" row is missing a
        # catalog -- this is the most common reason monitor-created apps
        # end up looking blank in the main table.
        missing_catalog = []
        for refs in self._rows:
            if refs["action"].currentIndex() == 1:  # create new
                if not refs["catalog"].currentText().strip():
                    missing_catalog.append(refs["item"].file_name)
        if missing_catalog:
            sample = "\n".join("  • " + f for f in missing_catalog[:5])
            more = f"\n  • …and {len(missing_catalog) - 5} more" if len(missing_catalog) > 5 else ""
            reply = QMessageBox.question(
                self, "Missing catalog",
                f"{len(missing_catalog)} new app(s) have no catalog:\n\n"
                f"{sample}{more}\n\n"
                "Continue anyway? (You can fill in the catalog later from "
                "the detail panel.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        for refs in self._rows:
            item = refs["item"]
            mode = refs["action"].currentIndex()
            if mode == 0:  # attach
                app_id = refs["target"].currentData()
                if app_id is None:
                    QMessageBox.warning(
                        self, "No target app",
                        f"Row for '{item.file_name}' is set to Attach but no "
                        "target app is selected. Fix it or set the row to Skip."
                    )
                    return
                item.action = "attach"
                item.matched_app_id = app_id
            elif mode == 1:  # create new
                new_name = refs["new_name"].text().strip()
                if not new_name:
                    QMessageBox.warning(
                        self, "Missing name",
                        f"Row for '{item.file_name}' is set to Create new but "
                        "the new app name is empty."
                    )
                    return
                item.action = "create_new"
                item.new_name = new_name
                item.new_catalog = refs["catalog"].currentText().strip()
                item.new_subcatalog = refs["subcatalog"].currentText().strip()
            else:
                item.action = "skip"
        self.accept()

    def edited_plan(self) -> list:
        return self.plan


class _MonitorReportDialog(QDialog):
    """Quick end-of-run summary + CSV export of the same rows."""

    def __init__(self, result: MonitorResult, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle("Monitor — run report")
        self.resize(960, 520)
        layout = QVBoxLayout(self)

        summary = QLabel(
            f"<b>Moved/Copied:</b> {result.moved}  |  "
            f"<b>Skipped:</b> {result.skipped}  |  "
            f"<b>Failed:</b> {result.failed}"
        )
        layout.addWidget(summary)

        log_line = QLabel(f"Move log: {result.move_log_path or '(none)'}")
        log_line.setWordWrap(True)
        log_line.setStyleSheet("color: palette(mid);")
        layout.addWidget(log_line)

        if result.cancelled:
            warn = QLabel("<b>Run was cancelled</b> — some rows were not processed.")
            warn.setStyleSheet("color: #b04a00;")
            layout.addWidget(warn)

        # checkpoint 21: auto-open the HTML report the same way reorganize
        # does (see app_organizer.py's _on_reorg_finished) -- the in-app
        # table below already shows per-row notes/errors, so this is
        # mainly for a shareable/printable copy and to keep the two
        # "ran a batch file operation" flows in this app consistent.
        if result.html_report_path and os.path.exists(result.html_report_path):
            try:
                webbrowser.open(Path(result.html_report_path).as_uri())
            except Exception as e:
                log.warning("Could not auto-open monitor HTML report: %s", e)

        self.table = QTableWidget(len(result.items), 6)
        self.table.setHorizontalHeaderLabels(
            ["File", "Outcome", "App", "Destination", "Archive", "Notes"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for i, it in enumerate(result.items):
            self.table.setItem(i, 0, QTableWidgetItem(it.file_name))
            self.table.setItem(i, 1, QTableWidgetItem(it.outcome))
            self.table.setItem(i, 2, QTableWidgetItem(
                it.app_name or (f"#{it.app_id}" if it.app_id else "")))
            self.table.setItem(i, 3, QTableWidgetItem(it.dest_path or ""))
            if it.archive_created:
                archive = it.archive_backend or "created"
            elif it.archive_backend == "move-as-is":
                archive = "moved as-is"
            else:
                archive = "—"
            self.table.setItem(i, 4, QTableWidgetItem(archive))
            note = it.note or it.error or ""
            self.table.setItem(i, 5, QTableWidgetItem(note))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, stretch=1)

        btn_row = QHBoxLayout()
        export_btn = QPushButton("Export to CSV…")
        export_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(export_btn)
        if result.html_report_path:
            report_btn = QPushButton("Open HTML Report")
            report_btn.clicked.connect(self._open_html_report)
            btn_row.addWidget(report_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _open_html_report(self):
        if self.result.html_report_path and os.path.exists(self.result.html_report_path):
            try:
                webbrowser.open(Path(self.result.html_report_path).as_uri())
            except Exception as e:
                QMessageBox.warning(self, "Could not open report", str(e))
        else:
            QMessageBox.warning(self, "No report", "No HTML report is available for this run.")

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save monitor report",
            f"monitor_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV files (*.csv)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["file_name", "source_path", "outcome", "app_id",
                            "app_name", "dest_path", "archive_created",
                            "archive_backend", "note", "error"])
                for it in self.result.items:
                    w.writerow([
                        it.file_name, it.source_path, it.outcome,
                        it.app_id or "", it.app_name or "",
                        it.dest_path or "",
                        "yes" if it.archive_created else "no",
                        it.archive_backend or "",
                        it.note or "", it.error or "",
                    ])
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Export complete", f"Wrote:\n{path}")


# ======================================================================
# GUI -- worker (private)
# ======================================================================
class _MonitorWorker(QThread):
    """Two-phase worker. Phase 1 scans and emits plan_ready; the worker
    then blocks on a QWaitCondition until MonitorJob submits or rejects
    the plan. Phase 2 executes and emits finished_ok."""
    scan_progress = Signal(object)      # {"scanned": n, "path": str}
    plan_ready = Signal(list)           # list[MonitorPlanItem]
    execute_progress = Signal(object)   # {"done": n, "total": n, "item": ...}
    finished_ok = Signal(object)        # MonitorResult | None if cancelled
    failed = Signal(str)

    def __init__(self, db_path: str, folders: list[str], dest_root: str,
                 move_mode: str, archive_format: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.folders = folders
        self.dest_root = dest_root
        self.move_mode = move_mode
        self.archive_format = archive_format

        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._plan_submitted = False
        self._plan_rejected = False
        self._edited_plan = None
        self._cancelled = False

    # -- called from the GUI thread --
    def submit_plan(self, edited_plan: list):
        self._mutex.lock()
        self._edited_plan = edited_plan
        self._plan_submitted = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def reject_plan(self):
        self._mutex.lock()
        self._plan_rejected = True
        self._plan_submitted = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            db = Database(self.db_path)
            db.init_schema()
            settings = dict(db.get_all_settings())
            settings["monitor_archive_format"] = self.archive_format
            settings["monitor_move_mode"] = self.move_mode

            plan = scan_monitor_folders(
                db, self.folders, settings,
                on_progress=lambda n, path: self.scan_progress.emit(
                    {"scanned": n, "path": path}),
                cancel_flag=lambda: self._cancelled,
            )
            self.plan_ready.emit(plan)

            self._mutex.lock()
            while not self._plan_submitted:
                self._condition.wait(self._mutex)
            self._mutex.unlock()

            if self._plan_rejected:
                self.finished_ok.emit(None)
                return

            result = execute_monitor_plan(
                db, self._edited_plan, self.dest_root,
                move_mode=self.move_mode,
                settings=settings,
                on_progress=lambda done, total, item: self.execute_progress.emit(
                    {"done": done, "total": total, "item": item}),
                cancel_flag=lambda: self._cancelled,
            )
            self.finished_ok.emit(result)

        except Exception as e:
            log.error("Monitor job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


# ======================================================================
# Public API -- the ONLY thing the GUI needs to import
# ======================================================================
class MonitorJob(QObject):
    """
    Self-contained Monitor job. The GUI does:

        job = MonitorJob(parent_window, db, db_path)
        job.progress.connect(...)        # status text for the status bar
        job.job_finished.connect(...)    # emits MonitorResult | None
        job.job_failed.connect(...)      # emits str
        if job.start():                  # False if user cancelled the
            self._active_worker = job    # config dialog; True once running
            self.progress_bar.setVisible(True)

    The GUI must clear `_active_worker` in both `job_finished` and
    `job_failed`, exactly like the other job types.
    """

    progress = Signal(str)              # status text for the GUI status bar
    job_finished = Signal(object)       # MonitorResult | None (None = cancelled)
    job_failed = Signal(str)

    def __init__(self, parent_window, db: Database, db_path: str):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._db = db
        self._db_path = db_path
        self._worker: Optional[_MonitorWorker] = None

    def start(self) -> bool:
        """Opens the start-config dialog; returns False if the user
        cancelled (nothing was started), True once the worker is running."""
        settings = self._db.get_all_settings()
        start_dialog = _MonitorStartDialog(
            self._db, settings, parent=self._parent_window)
        if not start_dialog.exec():
            return False

        self._worker = _MonitorWorker(
            self._db_path,
            start_dialog.folders,
            start_dialog.dest_root,
            start_dialog.move_mode,
            start_dialog.archive_format,
        )
        self._worker.scan_progress.connect(self._on_scan_progress)
        self._worker.plan_ready.connect(self._on_plan_ready)
        self._worker.execute_progress.connect(self._on_execute_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        return True

    def cancel(self):
        """Ask a running job to stop. Plan-phase waits are woken; execute-
        phase items stop after the current one finishes."""
        if self._worker is not None:
            self._worker.reject_plan()   # wakes the plan-wait if applicable
            self._worker.cancel()        # stops execute at the next item

    # --- Slots run on the main thread (worker emits from its own) ---
    def _on_scan_progress(self, info: dict):
        self.progress.emit(
            f"Monitor: {info['scanned']} candidate(s) found — {info['path']}")

    def _on_plan_ready(self, plan: list):
        self.progress.emit(
            f"Monitor: {len(plan)} candidate(s) — review the plan.")
        if not plan:
            # Nothing to do; submit an empty plan so the worker exits
            # cleanly and _on_finished runs (with an empty result).
            self._worker.submit_plan([])
            return
        dialog = _MonitorPlanDialog(self._db, plan, parent=self._parent_window)
        if dialog.exec():
            self._worker.submit_plan(dialog.edited_plan())
            self.progress.emit("Monitor: executing plan…")
        else:
            self._worker.reject_plan()

    def _on_execute_progress(self, info: dict):
        item = info.get("item")
        name = item.file_name if item is not None else ""
        self.progress.emit(
            f"Monitor: {info['done']}/{info['total']} — {name}")

    def _on_finished(self, result):
        self._worker = None
        # Emit FIRST so MainWindow._on_monitor_finished runs (status bar
        # update, table refresh, _active_worker release) before the report
        # dialog blocks the main thread. The report becomes a follow-up
        # summary the user can read at leisure, not a gate.
        self.job_finished.emit(result)
        if result is not None and result.items:
            dialog = _MonitorReportDialog(result, parent=self._parent_window)
            dialog.exec()

    def _on_failed(self, err: str):
        self._worker = None
        self.job_failed.emit(err)
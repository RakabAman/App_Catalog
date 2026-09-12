"""
gui_backend.py – Non‑UI components: table model, background workers, CSV import/export.
"""

import csv
import logging
import math
import os
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout,
)

from database import Database
from scanner import run_scan, ScanProgress
from resolver import run_resolve, ResolveProgress, edit_app_field
from scraper import (
    run_scrape, ScrapeProgress, ScrapeResult, load_manifest,
    apply_choco_candidate, apply_manifest_candidate,
)

log = logging.getLogger("appcatalog.gui_backend")

# =============================================================
# Apps table model (formerly gui/models.py)
# =============================================================

# Every column here maps directly to either a real column on the
# `apps` table (SELECT a.*) or to one of the aggregated aliases
# produced in AppsTableModel.refresh() (version_summary,
# variant_count, scan_paths, tags, added_at, last_scanned_at).
#
# Order is grouped: identity → categorization → variants →
# resolver metadata → scraper metadata → timestamps → misc.
COLUMNS = [
    # --- identity ---
    ("id",                "ID"),
    ("name",              "App Name"),
    ("name_locked",       "Name Locked"),
    ("normalized_key",    "Normalized Key"),

    # --- categorization ---
    ("catalog",           "Catalog"),
    ("catalog_locked",    "Catalog Locked"),
    ("subcatalog",        "Subcatalog"),
    ("subcatalog_locked", "Subcatalog Locked"),
    ("tags",              "Tags"),

    # --- variants / disk ---
    ("version_summary",   "Versions"),
    ("variant_count",     "Version Count"),
    ("scan_paths",        "Scan Paths"),

    # --- resolver metadata ---
    ("confidence",                    "Confidence"),
    ("status",                        "Status"),
    ("resolved_with_settings_version","Settings Version"),
    ("alt_name_candidate",            "Alt Name"),
    ("alt_name_source",               "Alt Name Source"),

    # --- scraper metadata ---
    ("scrape_status",     "Scraped"),
    ("publisher",         "Publisher"),
    ("description",       "Description"),
    ("homepage_url",      "Homepage"),
    ("license",           "License"),
    ("latest_version",    "Latest Version"),
    ("winget_id",         "Winget ID"),
    ("choco_id",          "Choco ID"),
    ("manifest_name",     "Manifest Name"),
    ("alt_source_name",   "Alt Source Name"),
    ("last_scraped",      "Last Scraped"),

    # --- timestamps ---
    ("added_at",          "Added"),
    ("last_scanned_at",   "Last Scanned"),
    ("created_at",        "Created At"),
    ("updated_at",        "Updated At"),

    # --- misc ---
    ("icon_path",         "Icon Path"),
]

EDITABLE_FIELDS = {"name", "catalog", "subcatalog"}

# =============================================================
# Shared dialogs
# =============================================================

class AppPickerDialog(QDialog):
    """Dialog to pick an existing app by name, with a live filter box.

    Used by BOTH:
      - DetailPanel's "Move to different app…" (variant reassignment)
      - _MonitorPlanDialog's per-row target-app picker (monitor.py)

    The filter matches against name + catalog + subcatalog, so an app can
    be found by any of those. Double-click, Enter, or OK selects.

    Call exec(); on Accepted, selected_app_id() returns the picked app's
    id, or None if nothing was picked (e.g. the filter matched nothing
    and the user clicked OK anyway).
    """
    def __init__(self, db: Database, exclude_app_id: Optional[int] = None,
                 parent=None, title: str = "Select target app"):
        super().__init__(parent)
        self.db = db
        self.exclude_app_id = exclude_app_id
        self.setWindowTitle(title)
        self.resize(500, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select target app:"))

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            "Filter by name / catalog / subcatalog…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        # Enter in the filter box accepts with whatever's currently
        # selected -- matches the "quick open" pattern users expect
        # from IDE / file-picker search boxes, so a filter-then-Enter
        # is a two-keystroke pick.
        self.filter_edit.returnPressed.connect(self._accept_if_selected)
        layout.addWidget(self.filter_edit)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.list_widget)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self._populate()
        self.filter_edit.setFocus()

    def _populate(self):
        rows = self.db.connect().execute(
            "SELECT id, name, catalog, subcatalog FROM apps "
            "WHERE id != ? ORDER BY name",
            (self.exclude_app_id or -1,)
        ).fetchall()
        self.list_widget.clear()
        for r in rows:
            # Show the catalog/subcatalog inline when present, so two
            # apps sharing a name (which happen -- e.g. an "Adobe Reader"
            # filed under two different catalogs) are distinguishable
            # at pick time.
            loc = "/".join(x for x in (r["catalog"], r["subcatalog"]) if x)
            label = r["name"] + (f"   ({loc})" if loc else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, r["id"])
            # Precompute the lowercase search blob once, per item, so
            # _apply_filter is a cheap substring check on a hidden column
            # of data regardless of how many apps are in the list.
            item.setData(Qt.UserRole + 1,
                         f"{r['name'] or ''} {r['catalog'] or ''} "
                         f"{r['subcatalog'] or ''}".lower())
            self.list_widget.addItem(item)

    def _apply_filter(self, text: str):
        needle = (text or "").strip().lower()
        first_visible = None
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            blob = item.data(Qt.UserRole + 1) or ""
            hidden = bool(needle) and needle not in blob
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        # Auto-select the first visible match so the filter box's Enter
        # shortcut (and the OK button, if pressed without clicking)
        # always has something to accept.
        if first_visible is not None:
            self.list_widget.setCurrentItem(first_visible)

    def _accept_if_selected(self):
        if self.selected_app_id() is not None:
            self.accept()

    def selected_app_id(self):
        item = self.list_widget.currentItem()
        if item is not None and not item.isHidden():
            return item.data(Qt.UserRole)
        return None
        

class AppsTableModel(QAbstractTableModel):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._rows: list[dict] = []
        self.search_text = ""
        self.catalog_filter: str | None = None
        self.subcatalog_filter: str | None = None
        self.status_filter: str | None = None
        self.scrape_status_filter: str | None = None

    def refresh(self):
        self.beginResetModel()
        conn = self.db.connect()

        query = """
            SELECT a.*,
                   GROUP_CONCAT(DISTINCT v.version)          AS version_summary,
                   COUNT(DISTINCT v.id)                      AS variant_count,
                   (SELECT GROUP_CONCAT(path, ' | ') FROM (
                        SELECT DISTINCT v2.source_path AS path
                        FROM variants v2
                        WHERE v2.app_id = a.id AND v2.is_ignored = 0
                    ))                                        AS scan_paths,
                   (SELECT GROUP_CONCAT(t.name, ', ') FROM app_tags at
                      JOIN tags t ON t.id = at.tag_id WHERE at.app_id = a.id) AS tags,
                   MIN(r.first_seen_at) AS added_at,
                   MAX(r.last_seen_at)  AS last_scanned_at
            FROM apps a
            LEFT JOIN variants v ON v.app_id = a.id AND v.is_ignored = 0
            LEFT JOIN raw_candidates r ON r.id = v.raw_candidate_id
            WHERE 1=1
        """
        params: list = []
        if self.search_text:
            query += " AND (a.name LIKE ? OR a.catalog LIKE ? OR a.subcatalog LIKE ?)"
            like = f"%{self.search_text}%"
            params += [like, like, like]
        if self.catalog_filter:
            query += " AND a.catalog = ?"
            params.append(self.catalog_filter)
        if self.subcatalog_filter:
            query += " AND a.subcatalog = ?"
            params.append(self.subcatalog_filter)
        if self.status_filter:
            query += " AND a.status = ?"
            params.append(self.status_filter)
        if self.scrape_status_filter:
            if self.scrape_status_filter == "not_scraped":
                query += " AND (a.scrape_status IS NULL OR a.scrape_status = 'not_scraped')"
            else:
                query += " AND a.scrape_status = ?"
                params.append(self.scrape_status_filter)
        query += " GROUP BY a.id ORDER BY a.catalog, a.subcatalog, a.name"
        rows = conn.execute(query, params).fetchall()
        self._rows = [dict(r) for r in rows]
        self.endResetModel()

    def distinct_catalogs(self) -> list[str]:
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT DISTINCT catalog FROM apps WHERE catalog IS NOT NULL ORDER BY catalog"
        ).fetchall()
        return [r["catalog"] for r in rows]

    def catalog_subcatalog_tree(self) -> dict[str, list[str]]:
        """{catalog: [subcatalog, ...]} for the left-panel tree -- catalogs
        with no subcatalogs at all (or only apps with a NULL/blank
        subcatalog) simply get an empty list, so the tree shows them as a
        plain leaf-like top-level entry rather than an empty expand arrow."""
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT DISTINCT catalog, subcatalog FROM apps "
            "WHERE catalog IS NOT NULL ORDER BY catalog, subcatalog"
        ).fetchall()
        tree: dict[str, list[str]] = {}
        for r in rows:
            subs = tree.setdefault(r["catalog"], [])
            if r["subcatalog"]:
                subs.append(r["subcatalog"])
        return tree

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return COLUMNS[section][1]
        return str(section + 1)

    _DATE_FIELDS = {"added_at", "last_scanned_at", "created_at", "updated_at", "last_scraped"}
    _BOOL_FIELDS = {"name_locked", "catalog_locked", "subcatalog_locked"}
    _LONG_TEXT_FIELDS = {"description", "homepage_url", "scan_paths", "icon_path", "alt_name_candidate"}

    # Row status colors (checkpoint 22). Each state gets an EXPLICIT
    # (background, foreground) pair rather than just a background --
    # relying on the system palette for text color was the actual bug
    # behind "yellow is too bright, text isn't visible": on a system in
    # dark mode, default cell text renders light/white, and a pale
    # background (however soft) then gets white-on-pale-yellow, which is
    # nearly unreadable. Pairing an explicit dark, muted text color with
    # every custom background makes this correct on ANY system theme,
    # light or dark, permanently -- not just a tweak to one shade.
    #
    # Priority (first match wins) reflects urgency: an app whose very
    # NAME/CATEGORY is unresolved and needs a human look wins over an app
    # that's merely un-enriched; "ignored" is deliberately least urgent
    # (the user already made a decision about it). Once an app is fully
    # done -- scraped successfully, or manually marked verified -- it
    # gets NO override at all (returns None), so it just uses the normal
    # system row color like everything else in a finished, healthy state.
    _STATUS_COLORS = {
        "needs_review":     (QColor(0xF0, 0xDF, 0xB0), QColor(0x4A, 0x39, 0x00)),  # dull amber
        "scrape_failed":    (QColor(0xF3, 0xD9, 0xD9), QColor(0x5C, 0x1A, 0x1A)),  # dull rose
        "not_yet_enriched": (QColor(0xD6, 0xE6, 0xF5), QColor(0x16, 0x32, 0x4A)),  # dull blue -- "new"/pending
        "ignored":          (QColor(0xE4, 0xE4, 0xE4), QColor(0x4A, 0x4A, 0x4A)),  # dull gray
    }

    def _row_status_key(self, row: dict) -> Optional[str]:
        status = row.get("status")
        scrape_status = row.get("scrape_status") or "not_scraped"
        if status == "verified" or scrape_status == "scraped":
            return None  # done -- normal row, no override
        if status == "needs_review":
            return "needs_review"
        if scrape_status == "failed":
            return "scrape_failed"
        if status == "ignored":
            return "ignored"
        if scrape_status in ("not_scraped", "pending"):
            return "not_yet_enriched"
        return None

    _STATUS_TOOLTIPS = {
        "needs_review": "Needs review — the resolver wasn't confident about this app's "
                         "name/catalog. Check it, then Verify or fix it manually.",
        "scrape_failed": "The last metadata lookup failed — no match was found, or the "
                          "network/manifest lookup errored. Try scraping again.",
        "not_yet_enriched": "Newly resolved — no metadata has been fetched for this app yet.",
        "ignored": "Marked as ignored — excluded from most reports and bulk actions.",
    }

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = COLUMNS[index.column()][0]
        value = row.get(key)

        if role in (Qt.DisplayRole, Qt.EditRole):
            if key in self._BOOL_FIELDS:
                return "✓" if value else ""
            if key == "confidence" and value is not None:
                return f"{value:.2f}"
            if key == "version_summary":
                count = row.get("variant_count", 0)
                return f"{value or ''} ({count})" if count else (value or "")
            if key == "scrape_status":
                return value or "not_scraped"
            if key in self._DATE_FIELDS and value:
                # Date-only for the cell; full timestamp via tooltip below
                return str(value)[:10]
            if value is None:
                return ""
            return value

        if role == Qt.ToolTipRole:
            # Full value for anything we truncated or that can be very long
            if key in self._DATE_FIELDS and value:
                return str(value)
            if key in self._LONG_TEXT_FIELDS and value:
                return str(value)
            status_key = self._row_status_key(row)
            if status_key and key in ("status", "scrape_status"):
                return self._STATUS_TOOLTIPS[status_key]
            if key == "name" and row.get("name_locked"):
                return "Manually locked — won't be overwritten by re-resolve"
            if key == "name_locked" and value:
                return "Resolver will not overwrite this app's name"
            if key == "catalog_locked" and value:
                return "Resolver will not overwrite this app's catalog"
            if key == "subcatalog_locked" and value:
                return "Resolver will not overwrite this app's subcatalog"
            return None

        if role == Qt.BackgroundRole:
            status_key = self._row_status_key(row)
            if status_key:
                return self._STATUS_COLORS[status_key][0]
            return None

        if role == Qt.ForegroundRole:
            status_key = self._row_status_key(row)
            if status_key:
                return self._STATUS_COLORS[status_key][1]
            return None

        if role == Qt.FontRole and key == "name" and row.get("status") == "ignored":
            from PySide6.QtGui import QFont
            font = QFont()
            font.setItalic(True)
            return font

        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        key = COLUMNS[index.column()][0]
        if key in EDITABLE_FIELDS:
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        row = self._rows[index.row()]
        key = COLUMNS[index.column()][0]
        if key not in EDITABLE_FIELDS:
            return False
        if not value.strip():
            return False
        edit_app_field(self.db, row["id"], key, value.strip())
        row[key] = value.strip()
        row[f"{key}_locked"] = 1
        self.dataChanged.emit(index, index)
        return True

    _NUMERIC_SORT_KEYS = {
        "id", "confidence", "variant_count",
        "resolved_with_settings_version",
    }

    def sort(self, column: int, order=Qt.AscendingOrder):
        if not (0 <= column < len(COLUMNS)):
            return
        key = COLUMNS[column][0]
        reverse = order == Qt.DescendingOrder

        def sort_key(row: dict):
            value = row.get(key)
            if key in self._NUMERIC_SORT_KEYS:
                return (value is None, value if value is not None else 0.0)
            text = "" if value is None else str(value)
            return (text == "", text.lower())

        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=sort_key, reverse=reverse)
        self.layoutChanged.emit()

    def app_id_at(self, row: int) -> int | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]["id"]
        return None

    def row_dict_at(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


# =============================================================
# Background Workers
# =============================================================

class ChocoSearchWorker(QThread):
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, search_term: str, max_results: int, parent=None):
        super().__init__(parent)
        self.search_term = search_term
        self.max_results = max_results

    def run(self):
        try:
            from choco_search import search_chocolatey
            results = search_chocolatey(self.search_term, self.max_results)
            self.finished_ok.emit(results)
        except Exception as e:
            log.error("Chocolatey search failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class WingetSearchWorker(QThread):
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, db_path: str, search_term: str, max_results: int, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.search_term = search_term
        self.max_results = max_results

    def run(self):
        try:
            from database import Database as _Database
            db = _Database(self.db_path)
            db.init_schema()
            settings = db.get_all_settings()
            manifest_result = load_manifest(self.db_path, settings)
            if not manifest_result.lookup:
                self.failed.emit(manifest_result.error or "Could not load the Winget manifest")
                return
            from scraper import search_manifest
            results = search_manifest(manifest_result.lookup, self.search_term, self.max_results)
            self.finished_ok.emit(results)
        except Exception as e:
            log.error("Winget manifest search failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class ScanWorker(QThread):
    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: str, root_path: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.root_path = root_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            db = Database(self.db_path)
            db.init_schema()
            result = run_scan(
                db,
                self.root_path,
                on_progress=lambda p: self.progress.emit(_copy_scan_progress(p)),
                cancel_flag=lambda: self._cancelled,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            log.error("Scan job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class ScanAndResolveWorker(QThread):
    scan_progress = Signal(object)
    resolve_started = Signal()
    finished_ok = Signal(object, object)
    failed = Signal(str)

    def __init__(self, db_path: str, root_path: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.root_path = root_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            db = Database(self.db_path)
            db.init_schema()
            scan_result = run_scan(
                db,
                self.root_path,
                on_progress=lambda p: self.scan_progress.emit(_copy_scan_progress(p)),
                cancel_flag=lambda: self._cancelled,
            )
            if scan_result.status == "cancelled":
                self.finished_ok.emit(scan_result, None)
                return
            self.resolve_started.emit()
            resolve_result = run_resolve(db)
            self.finished_ok.emit(scan_result, resolve_result)
        except Exception as e:
            log.error("ScanAndResolve job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class ResolveWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path

    def run(self):
        try:
            db = Database(self.db_path)
            db.init_schema()
            result = run_resolve(db)
            self.finished_ok.emit(result)
        except Exception as e:
            log.error("Resolve job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class CleanLibraryScanWorker(QThread):
    """
    Read-only "does this still exist on disk" pass -- see
    app_manager.scan_for_missing_sources()'s docstring for what this
    deliberately is NOT (it's not a re-scan; it never looks for new
    install units). Threaded like every other filesystem-touching job in
    this app because os.path.exists() on a network share or a drive
    that's gone to sleep can block for a noticeable moment per call, and
    a catalog can easily have thousands of variants.
    """
    progress = Signal(int, int)          # checked, total
    finished_ok = Signal(object)         # list[MissingItem]
    failed = Signal(str)

    def __init__(self, db_path: str, root_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.root_path = root_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            from app_manager import scan_for_missing_sources
            db = Database(self.db_path)
            db.init_schema()
            missing = scan_for_missing_sources(
                db, root_path=self.root_path,
                on_progress=lambda i, total: self.progress.emit(i, total),
                cancel_flag=lambda: self._cancelled,
            )
            self.finished_ok.emit(missing)
        except Exception as e:
            log.error("Clean-library scan failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


class ScrapeWorker(QThread):
    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: str, app_ids: Optional[list] = None,
                 force_manifest_refresh: bool = False, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.app_ids = app_ids
        self.force_manifest_refresh = force_manifest_refresh
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            db = Database(self.db_path)
            db.init_schema()
            result = run_scrape(
                db,
                app_ids=self.app_ids,
                force_manifest_refresh=self.force_manifest_refresh,
                on_progress=lambda p: self.progress.emit(_copy_scrape_progress(p)),
                cancel_flag=lambda: self._cancelled,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            log.error("Scrape job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


def _copy_scrape_progress(p: ScrapeProgress) -> ScrapeProgress:
    snap = ScrapeProgress()
    snap.total = p.total
    snap.processed = p.processed
    snap.matched = p.matched
    snap.not_found = p.not_found
    snap.current_name = p.current_name
    snap.status = p.status
    return snap


def _copy_scan_progress(p: ScanProgress) -> ScanProgress:
    snap = ScanProgress()
    snap.folders_seen = p.folders_seen
    snap.install_units_found = p.install_units_found
    snap.skipped_unchanged = p.skipped_unchanged
    snap.current_path = p.current_path
    snap.status = p.status
    return snap


# =============================================================
# CSV export/import
# =============================================================

EXPORT_FIELDS = [
    "app_id", "name", "catalog", "subcatalog", "tags", "status", "confidence",
    "alt_name_candidate", "alt_name_source", "variant_count", "versions",
    "sample_file_name", "sample_path",
]


def export_apps_csv(db: Database, output_dir: str, batch_size: int = 0,
                    base_name: str = "catalog_export") -> list[str]:
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT a.id AS app_id, a.name, a.catalog, a.subcatalog, a.status, a.confidence,
               a.alt_name_candidate, a.alt_name_source,
               (SELECT GROUP_CONCAT(t.name, ', ') FROM app_tags at
                  JOIN tags t ON t.id = at.tag_id WHERE at.app_id = a.id) AS tags,
               COUNT(v.id) AS variant_count,
               GROUP_CONCAT(DISTINCT v.version) AS versions,
               (SELECT v2.file_name FROM variants v2 WHERE v2.app_id = a.id AND v2.file_name IS NOT NULL LIMIT 1) AS sample_file_name,
               (SELECT v2.source_path FROM variants v2 WHERE v2.app_id = a.id LIMIT 1) AS sample_path
        FROM apps a
        LEFT JOIN variants v ON v.app_id = a.id AND v.is_ignored = 0
        GROUP BY a.id
        ORDER BY a.catalog, a.subcatalog, a.name
        """
    ).fetchall()
    os.makedirs(output_dir, exist_ok=True)
    written = []
    if batch_size <= 0:
        path = os.path.join(output_dir, f"{base_name}.csv")
        _write_csv(path, rows)
        written.append(path)
    else:
        total_batches = math.ceil(len(rows) / batch_size) if rows else 0
        for i in range(total_batches):
            batch = rows[i * batch_size:(i + 1) * batch_size]
            path = os.path.join(output_dir, f"{base_name}_part{i+1:02d}.csv")
            _write_csv(path, batch)
            written.append(path)
    return written


def _write_csv(path: str, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] if r[k] is not None else "" for k in EXPORT_FIELDS})


def import_apps_csv(db: Database, csv_path: str) -> dict:
    conn = db.connect()
    counts = {"updated": 0, "skipped": 0, "not_found": 0}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            app_id_raw = (row.get("app_id") or "").strip()
            if not app_id_raw:
                counts["skipped"] += 1
                continue
            try:
                app_id = int(app_id_raw)
            except ValueError:
                counts["skipped"] += 1
                continue
            existing = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
            if existing is None:
                counts["not_found"] += 1
                continue
            changed = False
            for field in ("name", "catalog", "subcatalog"):
                new_value = (row.get(field) or "").strip()
                if new_value and new_value != (existing[field] or ""):
                    edit_app_field(db, app_id, field, new_value)
                    changed = True
            if changed:
                counts["updated"] += 1
            else:
                counts["skipped"] += 1
    return counts
    
    
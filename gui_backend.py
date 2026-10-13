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
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout,
)

import app_manifest
import app_curation
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
        # List of (catalog, subcatalog|None) pairs. Empty = no filter.
        # subcatalog=None means "all subcatalogs of that catalog".
        self.catalog_filter_spec: list[tuple[Optional[str], Optional[str]]] = []
        self.status_filter: str | None = None
        self.scrape_status_filter: str | None = None
        # what the search text is matched against:
        #   "all"   app name AND the variants' file names
        #   "name"  app name only
        #   "tags"  catalog / subcatalog / tags
        #   "file"  variants' file names only
        #   "path"  variants' folder paths only
        self.search_scope: str = "all"
        self.app_id_filter = None     # optional set of app ids: only these apps are listed (e.g. from the Location check)

    def _search_clause(self) -> tuple:
        """SQL + params for the search box. Words are ANDed: every word must match
        somewhere in the chosen scope ("windows iso" = a name/file containing windows
        AND one containing iso). % and _ typed by the user are matched literally."""
        words = (self.search_text or "").split()
        if not words:
            return "", []
        scope = self.search_scope if self.search_scope in ("all", "name", "tags", "file", "path") else "all"
        esc = "ESCAPE '\\'"
        parts, params = [], []
        for w in words:
            like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            ors = []
            if scope in ("all", "name"):
                ors.append(f"a.name LIKE ? {esc}")
                params.append(like)
            if scope == "tags":
                # category + subcategory + tags (the filtering vocabulary)
                ors += [f"a.catalog LIKE ? {esc}", f"a.subcatalog LIKE ? {esc}",
                        "EXISTS (SELECT 1 FROM app_tags xat JOIN tags xt ON xt.id = xat.tag_id "
                        f"WHERE xat.app_id = a.id AND xt.name LIKE ? {esc})"]
                params += [like, like, like]
            if scope in ("all", "file"):
                ors.append(f"EXISTS (SELECT 1 FROM variants vs WHERE vs.app_id = a.id AND vs.file_name LIKE ? {esc})")
                params.append(like)
            if scope == "path":
                ors.append(f"EXISTS (SELECT 1 FROM variants vs WHERE vs.app_id = a.id AND vs.source_path LIKE ? {esc})")
                params.append(like)
            parts.append("(" + " OR ".join(ors) + ")")
        return " AND " + " AND ".join(parts), params

    def refresh(self):
        self.beginResetModel()
        rows = self._query_rows(use_search=True)
        self.fuzzy_used = False
        words = (self.search_text or "").split()
        if not rows and words and self.search_scope in ("all", "name"):
            # Nothing matched literally -> tolerate typos ("utilti" -> Utilities)
            # by fuzzy-matching every word against the app names, still honouring
            # the category / status filters.
            from rapidfuzz import fuzz

            def _ok(name: str) -> bool:
                n = (name or "").lower()
                return all(fuzz.partial_ratio(w.lower(), n) >= 80 for w in words)
            rows = [r for r in self._query_rows(use_search=False) if _ok(r["name"])]
            self.fuzzy_used = bool(rows)
        self._rows = [dict(r) for r in rows]
        self.endResetModel()

    def _query_rows(self, use_search: bool = True):
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
        clause, clause_params = self._search_clause() if use_search else ("", [])
        if clause:
            query += clause
            params += clause_params
        if self.catalog_filter_spec:
            clauses = []
            for cat, sub in self.catalog_filter_spec:
                if sub is not None:
                    clauses.append("(a.catalog = ? AND a.subcatalog = ?)")
                    params.extend([cat, sub])
                elif cat is not None:
                    clauses.append("a.catalog = ?")
                    params.append(cat)
            if clauses:
                query += " AND (" + " OR ".join(clauses) + ")"
        if self.status_filter:
            query += " AND a.status = ?"
            params.append(self.status_filter)
        if self.scrape_status_filter:
            if self.scrape_status_filter == "not_scraped":
                query += " AND (a.scrape_status IS NULL OR a.scrape_status = 'not_scraped')"
            else:
                query += " AND a.scrape_status = ?"
                params.append(self.scrape_status_filter)
        # While searching, apps whose NAME matches every word come first; the rest
        # (matched only through category / file name) follow, each group in the
        # usual catalog order.
        words = (self.search_text or "").split() if use_search else []
        rank = ""
        if words and self.search_scope in ("all", "name"):
            esc = "ESCAPE '\\'"
            rank = "CASE WHEN " + " AND ".join(f"a.name LIKE ? {esc}" for _ in words) + " THEN 0 ELSE 1 END, "
            params += ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                       for w in words]
        query += f" GROUP BY a.id ORDER BY {rank}a.catalog, a.subcatalog, a.name"
        rows = conn.execute(query, params).fetchall()
        if self.app_id_filter is not None:
            rows = [r for r in rows if r["id"] in self.app_id_filter]
        return rows

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


class AppsTableDelegate(QStyledItemDelegate):
    """
    Ensures a SELECTED row is always visually distinguishable from an
    unselected one, even when the model supplies a custom BackgroundRole
    for status coloring (needs_review = amber, scrape_failed = rose,
    not_yet_enriched = blue, ignored = gray). Without this, the custom
    background can visually mask Qt's own selection highlight, leaving
    the user unable to tell which row is currently selected.

    When the row is selected we force the standard highlight background
    and the standard highlighted-text color, overriding whatever the
    model returned for BackgroundRole/ForegroundRole. Unselected rows
    keep their status colors untouched.
    """
    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        if option.state & QStyle.State_Selected:
            # Force a solid selection background...
            option.backgroundBrush = option.palette.highlight()
            # ...and force the highlighted-text color so the model's
            # dark status foreground can't make the selected row's
            # text unreadable against the highlight.
            hl_text = option.palette.highlightedText().color()
            option.palette.setColor(QPalette.Text, hl_text)
            option.palette.setColor(QPalette.WindowText, hl_text)
            option.palette.setColor(QPalette.HighlightedText, hl_text)


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
            # throttled snapshot (at most one per 15 min) before a rescan changes anything
            app_curation.create_backup(self.db_path, "before-scan", keep=6, min_interval_s=900)
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
            # Safety net: snapshot the catalog first. "Resolve all" re-derives
            # data for every app, so keep a copy to roll back to.
            app_curation.create_backup(self.db_path, "before-resolve", keep=10)
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


class ManifestWorker(QThread):
    """Manual 'Create / update manifests' job (all apps, or a given set of
    variant ids). Always writes -- the auto-mode 'only valuable' filter does
    not apply to an explicit request."""
    progress = Signal(int, int)
    finished_ok = Signal(object)          # app_manifest.BatchResult
    failed = Signal(str)

    def __init__(self, db_path: str, variant_ids: Optional[list] = None, parent=None,
                 rebuild: bool = False):
        super().__init__(parent)
        self.db_path = db_path
        self.variant_ids = variant_ids
        self.rebuild = rebuild               # forced: delete existing metadata files first
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        db = None
        try:
            db = Database(self.db_path)
            db.init_schema()

            def _p(i, total, _path):
                if i % 10 == 0 or i == total:
                    self.progress.emit(i, total)

            result = app_manifest.write_manifests(
                db, self.variant_ids, progress=_p, cancel=lambda: self._cancelled,
                rebuild=self.rebuild)
            self.finished_ok.emit(result)
        except Exception as e:
            log.error("Manifest job failed:\n%s", traceback.format_exc())
            self.failed.emit(str(e))
        finally:
            if db is not None:
                db.close()


class ManifestFlushWorker(QThread):
    """Auto mode: writes the manifests of variants whose data changed.
    Runs on its own DB connection so the GUI never waits on file I/O."""
    finished_ok = Signal(object)

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path

    def run(self):
        db = None
        try:
            db = Database(self.db_path)
            db.connect()
            self.finished_ok.emit(app_manifest.flush_dirty(db))
        except Exception:
            log.error("Manifest auto-flush failed:\n%s", traceback.format_exc())
            self.finished_ok.emit(None)
        finally:
            if db is not None:
                db.close()


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
# CSV export/import now lives in catalog_csv.py (Qt-free); re-exported here so
# existing `from gui_backend import export_apps_csv, import_apps_csv` keeps working.
# =============================================================
from catalog_csv import (  # noqa: E402,F401
    export_apps_csv, import_apps_csv, format_import_report, count_apps_missing_data,
    SHORT_FIELDS, FILL_FIELDS,
)

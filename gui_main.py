"""
gui_main.py – All UI components: main window, detail panel, dialogs.
"""

import json
import os
import platform
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional
import re
import app_manifest
import app_curation
from curation_dialogs import AddAppDialog, RepairEmptyAppsDialog
from PySide6.QtCore import Qt, Signal, QItemSelectionModel, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTableView,
    QLineEdit, QLabel, QPushButton, QToolBar,
    QFileDialog, QProgressBar, QStatusBar, QComboBox, QMessageBox,
    QAbstractItemView, QDialog, QFormLayout, QDoubleSpinBox, QSpinBox,
    QCheckBox, QPlainTextEdit, QTabWidget, QScrollArea, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit, QInputDialog,
    QMenu, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QStackedWidget,
    QToolButton, QWidgetAction,
)
from PySide6.QtWidgets import QGridLayout, QSizePolicy 
from database import Database
from resolver import (
    propose_reresolve_app, apply_reresolve_app, set_app_status,
    move_variant_to_app, split_variant_to_new_app,
    set_variant_ignored, delete_variant,
)
from scraper import apply_choco_candidate, apply_manifest_candidate, ScrapeProgress, ScrapeResult
from app_organizer import OrganizeDialog

from monitor import MonitorJob
from app_manager import (
    validate_archive_password,
    execute_clean_library, update_scan_root_path, delete_scan_root,
    apply_layout_change,
)
from scanner import resolve_scan_root_layout, list_top_level_folders, list_child_folders

from gui_backend import (
    AppsTableModel, AppsTableDelegate, AppPickerDialog, COLUMNS,
    export_apps_csv, import_apps_csv,
    ScanWorker, ScanAndResolveWorker, ResolveWorker, ScrapeWorker,
    ChocoSearchWorker, WingetSearchWorker, CleanLibraryScanWorker,
    ManifestWorker, ManifestFlushWorker,
)


# =============================================================
# Module-scope helpers
# =============================================================

def _set_app_tags(db: Database, app_id: int, tag_names: list) -> None:
    """
    Replace an app's tag list wholesale. Creates any tag row that
    doesn't exist yet. Used by the detail panel's editable Tags field
    (single and multi selection).
    """
    conn = db.connect()
    conn.execute("DELETE FROM app_tags WHERE app_id = ?", (app_id,))
    seen = set()
    for name in tag_names:
        name = (name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            tag_id = row["id"]
        else:
            tag_id = conn.execute(
                "INSERT INTO tags (name) VALUES (?)", (name,)
            ).lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)",
            (app_id, tag_id),
        )
    conn.commit()


# =============================================================
# Detail panel
# =============================================================

VARIANT_COLUMNS = [
    ("version", "Version"),
    ("file_name", "File Name"),
    ("edition", "Edition"),
    ("type", "Type"),
    ("name_source", "Name Source"),
    ("scanned_at", "Scanned"),
    ("path", "Path"),
]

# Variant columns the user may edit in place. Editing sets
# variants.version_locked = 1 so a later Re-resolve won't overwrite
# the manual edit (same lock convention as app name/catalog/subcatalog).
EDITABLE_VARIANT_COLUMNS = {"version", "edition"}


class DetailPanel(QWidget):
    app_changed = Signal()
    scrape_requested = Signal(list)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_app_id: int | None = None
        # ---- Multi-selection state ----
        # _multi_mode is True only while load_apps() has populated the
        # panel for a multi-row selection; load_app() / clear() reset it.
        self._multi_mode = False
        self._multi_app_ids: list[int] = []
        # Snapshot of editable field values, so the editingFinished
        # handlers can tell a real edit from a mere focus-out (which
        # also fires editingFinished and would otherwise cause spurious
        # DB writes every time the user tabs through a field).
        self._baseline: dict = {}
        self._loading_variants = False
        self._build_ui()
        self.clear()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ---- App group ----
        fields_box = QGroupBox("App")
        form = QFormLayout(fields_box)
        form.setSpacing(8)                                    # ← increased spacing
        form.setContentsMargins(6, 6, 6, 6)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        # Row 1: Name (edit + lock)
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        self.name_edit = QLineEdit()
        # Increase font size for the app name
        font = self.name_edit.font()
        font.setPointSize(12)                                 # ← larger font
        self.name_edit.setFont(font)
        self.name_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.name_edit.editingFinished.connect(lambda: self._commit_field("name", self.name_edit))
        name_row.addWidget(self.name_edit, stretch=1)

        self.name_lock_label = QLabel()
        self.name_lock_label.setFixedWidth(20)
        name_row.addWidget(self.name_lock_label)
        form.addRow("Name", name_row)

        # Row 2: Alternative name buttons (below name)
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(4)

        self.btn_original_name = QPushButton("Original")
        self.btn_original_name.setFixedHeight(24)
        self.btn_original_name.clicked.connect(lambda: self.apply_alt_name('original'))
        buttons_row.addWidget(self.btn_original_name)

        self.btn_alt_name = QPushButton("Alternative")
        self.btn_alt_name.setFixedHeight(24)
        self.btn_alt_name.clicked.connect(lambda: self.apply_alt_name('alternative'))
        buttons_row.addWidget(self.btn_alt_name)

        self.btn_manifest_name = QPushButton("Winget")
        self.btn_manifest_name.setFixedHeight(24)
        self.btn_manifest_name.clicked.connect(lambda: self.apply_alt_name('winget'))
        buttons_row.addWidget(self.btn_manifest_name)

        self.btn_choco_name = QPushButton("Choco")
        self.btn_choco_name.setFixedHeight(24)
        self.btn_choco_name.clicked.connect(lambda: self.apply_alt_name('choco'))
        buttons_row.addWidget(self.btn_choco_name)

        buttons_row.addStretch()
        form.addRow("", buttons_row)

        # Row 3: Catalog | Subcatalog
        cat_sub_row = QHBoxLayout()
        cat_sub_row.setSpacing(10)

        cat_sub_row.addWidget(QLabel("Catalog:"))
        self.catalog_edit = QComboBox()
        self.catalog_edit.setEditable(True)
        self.catalog_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Commit only when the user confirms an edit -- either Enter /
        # focus-out on the combo's line edit, or picking an item from the
        # dropdown. currentTextChanged fires on every keystroke typed
        # (which would write one UPDATE + one audit_log row per character)
        # and on every programmatic setCurrentText() call.
        self.catalog_edit.lineEdit().editingFinished.connect(
            lambda: self._on_catalog_changed(self.catalog_edit.currentText())
        )
        self.catalog_edit.activated.connect(
            lambda _idx: self._on_catalog_changed(self.catalog_edit.currentText())
        )
        cat_sub_row.addWidget(self.catalog_edit, stretch=1)

        cat_sub_row.addWidget(QLabel("Subcatalog:"))
        self.subcatalog_edit = QComboBox()
        self.subcatalog_edit.setEditable(True)
        self.subcatalog_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.subcatalog_edit.lineEdit().editingFinished.connect(
            lambda: self._on_subcatalog_changed(self.subcatalog_edit.currentText())
        )
        self.subcatalog_edit.activated.connect(
            lambda _idx: self._on_subcatalog_changed(self.subcatalog_edit.currentText())
        )
        cat_sub_row.addWidget(self.subcatalog_edit, stretch=1)

        form.addRow("", cat_sub_row)

        # Row 4: Status + Scrape status
        status_row = QHBoxLayout()
        status_row.setSpacing(6)

        self.status_label = QLabel()
        self.status_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)
        status_row.addWidget(self.status_label)

        separator = QLabel("|")
        separator.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)
        status_row.addWidget(separator)

        self.scrape_source_label = QLabel("not scraped yet")
        self.scrape_source_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)
        status_row.addWidget(self.scrape_source_label)

        status_row.addStretch()
        form.addRow("Status", status_row)

        # Row 5: Description
        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText("(not scraped yet)")
        self.description_edit.setMaximumHeight(100)
        self.description_edit.setReadOnly(True)
        self.description_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Description", self.description_edit)

        layout.addWidget(fields_box)

        # ---- Scraper metadata ----
        scrape_box = QGroupBox("Scraper metadata")
        scrape_layout = QGridLayout(scrape_box)
        scrape_layout.setSpacing(8)                           # ← increased spacing
        scrape_layout.setContentsMargins(6, 6, 6, 6)

        # Row 0: Publisher (now EDITABLE) | Latest version (read-only)
        self.publisher_edit = QLineEdit()
        self.publisher_edit.setPlaceholderText("(not scraped yet)")
        self.publisher_edit.editingFinished.connect(
            lambda: self._commit_scraper_field("publisher", self.publisher_edit)
        )
        scrape_layout.addWidget(QLabel("Publisher:"), 0, 0)
        scrape_layout.addWidget(self.publisher_edit, 0, 1)

        self.latest_version_edit = QLineEdit()
        self.latest_version_edit.setReadOnly(True)
        self.latest_version_edit.setPlaceholderText("(not scraped yet)")
        scrape_layout.addWidget(QLabel("Latest version:"), 0, 2)
        scrape_layout.addWidget(self.latest_version_edit, 0, 3)

        # Row 1: Homepage (now EDITABLE) – spans columns 1 to 3
        self.homepage_edit = QLineEdit()
        self.homepage_edit.setPlaceholderText("(not scraped yet)")
        self.homepage_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.homepage_edit.editingFinished.connect(
            lambda: self._commit_scraper_field("homepage_url", self.homepage_edit)
        )
        scrape_layout.addWidget(QLabel("Homepage:"), 1, 0)
        scrape_layout.addWidget(self.homepage_edit, 1, 1, 1, 3)   # ← spans 3 columns

        # Row 2: Winget ID | Choco ID (read-only -- single-app only)
        self.winget_id_edit = QLineEdit()
        self.winget_id_edit.setReadOnly(True)
        self.winget_id_edit.setPlaceholderText("(no Winget match yet)")
        scrape_layout.addWidget(QLabel("Winget ID:"), 2, 0)
        scrape_layout.addWidget(self.winget_id_edit, 2, 1)

        self.choco_id_edit = QLineEdit()
        self.choco_id_edit.setReadOnly(True)
        self.choco_id_edit.setPlaceholderText("(no Chocolatey match yet)")
        scrape_layout.addWidget(QLabel("Chocolatey ID:"), 2, 2)
        scrape_layout.addWidget(self.choco_id_edit, 2, 3)

        # Row 3: Tags -- now an EDITABLE line edit (comma-separated)
        self.scraped_tags_edit = QLineEdit()
        self.scraped_tags_edit.setPlaceholderText("(no tags)")
        self.scraped_tags_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.scraped_tags_edit.editingFinished.connect(self._commit_tags)
        scrape_layout.addWidget(QLabel("Tags:"), 3, 0)
        scrape_layout.addWidget(self.scraped_tags_edit, 3, 1, 1, 3)

        # Column stretches – column 1 and 3 take extra space
        scrape_layout.setColumnStretch(1, 1)
        scrape_layout.setColumnStretch(3, 1)

        layout.addWidget(scrape_box)

        # Action buttons
        actions_row = QHBoxLayout()
        self.verify_btn = QPushButton("Mark verified")
        self.verify_btn.clicked.connect(self._mark_verified)
        self.reresolve_btn = QPushButton("Re-resolve this app")
        self.reresolve_btn.clicked.connect(self._reresolve)
        actions_row.addWidget(self.verify_btn)
        actions_row.addWidget(self.reresolve_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        # Variants table
        variants_box = QGroupBox("Variants found on disk")
        vbox = QVBoxLayout(variants_box)
        self.variants_table = QTableWidget(0, len(VARIANT_COLUMNS))
        self.variants_table.setHorizontalHeaderLabels([label for _, label in VARIANT_COLUMNS])
        self.variants_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.variants_table.horizontalHeader().setSectionsMovable(True)
        self.variants_table.horizontalHeader().setStretchLastSection(False)
        self.variants_table.setSortingEnabled(True)
        self.variants_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.variants_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.variants_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.variants_table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed
        )
        self.variants_table.itemChanged.connect(self._on_variant_item_changed)
        self.variants_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.variants_table.customContextMenuRequested.connect(self._show_variant_context_menu)
        vbox.addWidget(self.variants_table)

        variant_actions = QHBoxLayout()
        self.ignore_btn = QPushButton("Ignore selected")
        self.ignore_btn.clicked.connect(self._ignore_selected_variant)
        self.move_btn = QPushButton("Move to different app…")
        self.move_btn.clicked.connect(self._move_selected_variant)
        self.split_btn = QPushButton("Split into new app…")
        self.split_btn.clicked.connect(self._split_selected_variant)
        variant_actions.addWidget(self.ignore_btn)
        variant_actions.addWidget(self.move_btn)
        variant_actions.addWidget(self.split_btn)
        variant_actions.addStretch()
        vbox.addLayout(variant_actions)

        # Remember the whole variants group so multi-mode can hide it in
        # one step.
        self.variants_box = variants_box

        layout.addWidget(variants_box)

    def clear(self):
        # Reset multi-selection state and restore anything multi-mode hid.
        self._multi_mode = False
        self._multi_app_ids = []
        self._baseline = {}
        self.current_app_id = None

        self.name_edit.setEnabled(True)
        self.description_edit.setVisible(True)
        self.description_edit.setPlaceholderText("(not scraped yet)")
        self.variants_box.setVisible(True)
        self.btn_original_name.setVisible(True)
        self.verify_btn.setText("Mark verified")
        self.reresolve_btn.setText("Re-resolve this app")
        self.publisher_edit.setPlaceholderText("(not scraped yet)")
        self.homepage_edit.setPlaceholderText("(not scraped yet)")
        self.scraped_tags_edit.setPlaceholderText("(no tags)")

        self.name_edit.setText("")
        self.catalog_edit.clear()
        self.catalog_edit.addItem("")   # optional placeholder
        self.subcatalog_edit.clear()
        self.subcatalog_edit.addItem("")
        self.status_label.setText("")
        self.name_lock_label.setText("")
        self.description_edit.setPlainText("")
        self.publisher_edit.setText("")
        self.homepage_edit.setText("")

        self.latest_version_edit.setText("")
        self.winget_id_edit.setText("")
        self.choco_id_edit.setText("")

        self.scraped_tags_edit.setText("")
        self.scrape_source_label.setText("")
        self.variants_table.setRowCount(0)
        self.setEnabled(False)

    def _populate_catalog_combo(self):
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT DISTINCT catalog FROM apps WHERE catalog IS NOT NULL AND catalog != '' ORDER BY catalog"
        ).fetchall()
        items = [r["catalog"] for r in rows]
        self.catalog_edit.blockSignals(True)
        self.catalog_edit.clear()
        self.catalog_edit.addItems(items)
        self.catalog_edit.blockSignals(False)

    def _populate_subcatalog_combo(self):
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT DISTINCT subcatalog FROM apps WHERE subcatalog IS NOT NULL AND subcatalog != '' ORDER BY subcatalog"
        ).fetchall()
        items = [r["subcatalog"] for r in rows]
        self.subcatalog_edit.blockSignals(True)
        self.subcatalog_edit.clear()
        self.subcatalog_edit.addItems(items)
        self.subcatalog_edit.blockSignals(False)

    def load_app(self, app_id: int):
        # Reset any lingering multi-selection state and restore anything
        # multi-mode hid.
        self._multi_mode = False
        self._multi_app_ids = []
        self.name_edit.setEnabled(True)
        self.description_edit.setVisible(True)
        self.description_edit.setPlaceholderText("(not scraped yet)")
        self.variants_box.setVisible(True)
        self.verify_btn.setText("Mark verified")
        self.reresolve_btn.setText("Re-resolve this app")
        self.publisher_edit.setPlaceholderText("(not scraped yet)")
        self.homepage_edit.setPlaceholderText("(not scraped yet)")
        self.scraped_tags_edit.setPlaceholderText("(no tags)")

        self.setEnabled(True)
        self.current_app_id = app_id
        conn = self.db.connect()
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            self.clear()
            return
        app = dict(row)

        self.name_edit.setText(app.get("name") or "")

        # --- Populate combo boxes and set current values (with signals blocked) ---
        self._populate_catalog_combo()
        self._populate_subcatalog_combo()

        self.catalog_edit.blockSignals(True)
        self.catalog_edit.setCurrentText(app.get("catalog") or "")
        self.catalog_edit.blockSignals(False)

        self.subcatalog_edit.blockSignals(True)
        self.subcatalog_edit.setCurrentText(app.get("subcatalog") or "")
        self.subcatalog_edit.blockSignals(False)

        conf = app.get("confidence")
        self.status_label.setText(
            f"{app.get('status')}  (confidence {conf:.2f})" if conf is not None else app.get("status", "")
        )
        self.name_lock_label.setText("🔒" if app.get("name_locked") else "")
        self.description_edit.setPlainText(app.get("description") or "")

        # Block signals while populating so the editingFinished handlers
        # can't fire spuriously from a programmatic setText.
        self.publisher_edit.blockSignals(True)
        self.publisher_edit.setText(app.get("publisher") or "")
        self.publisher_edit.blockSignals(False)

        self.homepage_edit.blockSignals(True)
        self.homepage_edit.setText(app.get("homepage_url") or "")
        self.homepage_edit.blockSignals(False)

        self.latest_version_edit.setText(app.get("latest_version") or "")
        self.winget_id_edit.setText(app.get("winget_id") or "")
        self.choco_id_edit.setText(app.get("choco_id") or "")

        # Name buttons
        self.btn_original_name.setText(app.get("name") or "Original")
        self.btn_original_name.setVisible(True)

        alt = app.get("alt_name_candidate")
        if alt:
            self.btn_alt_name.setText(alt)
            self.btn_alt_name.setVisible(True)
        else:
            self.btn_alt_name.setVisible(False)

        man = app.get("manifest_name")
        if man:
            self.btn_manifest_name.setText(man)
            self.btn_manifest_name.setVisible(True)
        else:
            self.btn_manifest_name.setVisible(False)

        choco = app.get("choco_id")
        if choco:
            self.btn_choco_name.setText(choco)
            self.btn_choco_name.setVisible(True)
        else:
            self.btn_choco_name.setVisible(False)

        # Tags -- now a QLineEdit, populate as comma-separated text.
        tag_rows = conn.execute(
            """SELECT t.name FROM tags t JOIN app_tags at ON at.tag_id = t.id
               WHERE at.app_id = ? ORDER BY t.name""",
            (app_id,),
        ).fetchall()
        tags_text = ", ".join(r["name"] for r in tag_rows)
        self.scraped_tags_edit.blockSignals(True)
        self.scraped_tags_edit.setText(tags_text)
        self.scraped_tags_edit.blockSignals(False)

        if app.get("last_scraped"):
            self.scrape_source_label.setText(
                f"{app.get('scrape_status') or 'scraped'} — last scraped {app['last_scraped']}"
            )
        else:
            self.scrape_source_label.setText("not scraped yet")

        # Variants
        variants = conn.execute(
            """SELECT v.*, r.folder_path AS raw_path, r.first_seen_at AS scanned_at
               FROM variants v
               LEFT JOIN raw_candidates r ON v.raw_candidate_id = r.id
               WHERE v.app_id = ? ORDER BY v.version""",
            (app_id,),
        ).fetchall()
        self._loading_variants = True
        try:
            self.variants_table.setSortingEnabled(False)
            self.variants_table.setRowCount(len(variants))
            for i, v in enumerate(variants):
                scanned = (v["scanned_at"] or "")[:10]
                forced = bool(v["file_locked"]) if "file_locked" in v.keys() else False
                file_display = v["file_name"] or ""
                if forced:
                    file_display = f"🔒 {file_display}"
                values = [v["version"], file_display, v["edition"], v["file_type"],
                          v["name_source"], scanned, v["source_path"]]
                for j, val in enumerate(values):
                    item = QTableWidgetItem(val or "")
                    item.setData(Qt.UserRole, v["id"])
                    col_key = VARIANT_COLUMNS[j][0]
                    if col_key in EDITABLE_VARIANT_COLUMNS:
                        item.setFlags(item.flags() | Qt.ItemIsEditable)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if col_key == "scanned_at" and v["scanned_at"]:
                        item.setToolTip(v["scanned_at"])
                    if col_key == "file_name" and forced:
                        item.setToolTip(
                            f"Installer file manually locked (survives re-scans)\n"
                            f"Locked file: {v['file_name'] or '(none)'}\n"
                            f"Right-click → Clear installer file override to go back to auto."
                        )
                    if v["is_ignored"]:
                        item.setForeground(Qt.gray)
                    self.variants_table.setItem(i, j, item)
            self.variants_table.setSortingEnabled(True)
            self.variants_table.resizeColumnsToContents()
        finally:
            self._loading_variants = False

        # Snapshot the editable-field values now that everything's
        # populated, so _commit_* knows the "no change" baseline.
        self._baseline = {
            "publisher":    self.publisher_edit.text(),
            "homepage_url": self.homepage_edit.text(),
            "tags":         self.scraped_tags_edit.text(),
        }

    # ------------------------------------------------------------------
    # Multi-app loading (bulk edit mode)
    # ------------------------------------------------------------------
    def load_apps(self, app_ids: list) -> None:
        """
        Multi-app selection mode. Shows ONLY the fields that make sense
        to apply to several apps at once:
            Catalog, Subcatalog, Publisher, Homepage, Tags
        plus the two bulk action buttons.

        Name / Description / Winget ID / Choco ID / the variants table
        and the alt-name buttons are disabled or hidden, since they only
        make sense for one app at a time.

        A field whose selected-apps value is not unanimous shows as
        "(mixed)" (for combos) or blank with a hint placeholder (for
        line edits). Typing a new value and confirming applies it to
        every selected app.
        """
        if not app_ids:
            self.clear()
            return

        self._multi_mode = True
        self._multi_app_ids = list(app_ids)
        self.current_app_id = None
        self.setEnabled(True)

        conn = self.db.connect()
        placeholders = ",".join("?" * len(app_ids))
        rows = conn.execute(
            f"SELECT * FROM apps WHERE id IN ({placeholders})", app_ids
        ).fetchall()
        apps = [dict(r) for r in rows]
        n = len(apps)

        # --- Name: display-only summary; not editable ---
        self.name_edit.blockSignals(True)
        self.name_edit.setText(f"({n} apps selected)")
        self.name_edit.blockSignals(False)
        self.name_edit.setEnabled(False)
        self.name_lock_label.setText("")

        # --- Hide single-app-only buttons ---
        self.btn_original_name.setVisible(False)
        self.btn_alt_name.setVisible(False)
        self.btn_manifest_name.setVisible(False)
        self.btn_choco_name.setVisible(False)

        # --- Catalog / Subcatalog ---
        self._populate_catalog_combo()
        self._populate_subcatalog_combo()

        cat_values = {(a.get("catalog") or "") for a in apps}
        sub_values = {(a.get("subcatalog") or "") for a in apps}
        cat_unanimous = (len(cat_values) == 1)
        sub_unanimous = (len(sub_values) == 1)

        self.catalog_edit.blockSignals(True)
        self.catalog_edit.setCurrentText(
            next(iter(cat_values)) if cat_unanimous else "(mixed)"
        )
        self.catalog_edit.blockSignals(False)

        self.subcatalog_edit.blockSignals(True)
        self.subcatalog_edit.setCurrentText(
            next(iter(sub_values)) if sub_unanimous else "(mixed)"
        )
        self.subcatalog_edit.blockSignals(False)

        # --- Status line ---
        statuses = sorted({(a.get("status") or "?") for a in apps})
        self.status_label.setText(
            f"{n} apps selected — status(es): " + ", ".join(statuses)
        )

        # --- Description: single-app-only, hidden ---
        self.description_edit.setPlainText("")
        self.description_edit.setVisible(False)

        # --- Scraper metadata ---
        pub_values = {(a.get("publisher") or "") for a in apps}
        home_values = {(a.get("homepage_url") or "") for a in apps}
        pub_unanimous = (len(pub_values) == 1)
        home_unanimous = (len(home_values) == 1)

        self.publisher_edit.blockSignals(True)
        self.publisher_edit.setText(next(iter(pub_values)) if pub_unanimous else "")
        self.publisher_edit.setPlaceholderText(
            f"({n} apps — same value)" if pub_unanimous and next(iter(pub_values))
            else "(mixed — type to set for all selected)"
        )
        self.publisher_edit.blockSignals(False)

        self.homepage_edit.blockSignals(True)
        self.homepage_edit.setText(next(iter(home_values)) if home_unanimous else "")
        self.homepage_edit.setPlaceholderText(
            f"({n} apps — same value)" if home_unanimous and next(iter(home_values))
            else "(mixed — type to set for all selected)"
        )
        self.homepage_edit.blockSignals(False)

        # Single-app-only read-onlys: clear
        self.latest_version_edit.setText("")
        self.winget_id_edit.setText("")
        self.choco_id_edit.setText("")

        # --- Tags: union across all selected apps ---
        union = set()
        for a in apps:
            tag_rows = conn.execute(
                "SELECT t.name FROM tags t JOIN app_tags at ON at.tag_id = t.id "
                "WHERE at.app_id = ?",
                (a["id"],),
            ).fetchall()
            for r in tag_rows:
                union.add(r["name"])
        self.scraped_tags_edit.blockSignals(True)
        self.scraped_tags_edit.setText(", ".join(sorted(union)))
        self.scraped_tags_edit.setPlaceholderText(
            f"(union of {n} apps' tags — type to replace for all)"
        )
        self.scraped_tags_edit.blockSignals(False)

        self.scrape_source_label.setText(f"{n} apps selected")

        # --- Variants table hidden in multi mode ---
        self.variants_table.setRowCount(0)
        self.variants_box.setVisible(False)

        # --- Bulk action buttons ---
        self.verify_btn.setText(f"Mark all {n} verified")
        self.reresolve_btn.setText(f"Re-resolve all {n}")

        # --- Baseline snapshot ---
        self._baseline = {
            "publisher":    self.publisher_edit.text(),
            "homepage_url": self.homepage_edit.text(),
            "tags":         self.scraped_tags_edit.text(),
        }

    # ------------------------------------------------------------------
    # Field commit handlers
    # ------------------------------------------------------------------
    def _commit_field(self, field: str, widget):
        """
        Commit catalog/subcatalog/name. In multi mode, applies the new
        value to every selected app.
        """
        if isinstance(widget, QComboBox):
            value = widget.currentText().strip()
        else:
            value = widget.text().strip()

        # "(mixed)" is a display-only marker; ignore it as a value.
        if value == "(mixed)":
            return

        # Name is not allowed to be cleared (a nameless app is unusable in
        # the table and every picker dialog). Catalog/subcatalog CAN be
        # cleared -- an empty catalog is a valid state (the app just isn't
        # filed under any category yet) and the user needs a way to get
        # back to it if they mis-typed one.
        if field == "name" and not value:
            return

        from resolver import edit_app_field

        if self._multi_mode:
            for app_id in self._multi_app_ids:
                edit_app_field(self.db, app_id, field, value)
            self.app_changed.emit()
            return

        if self.current_app_id is None:
            return
        edit_app_field(self.db, self.current_app_id, field, value)
        if field == "name":
            self.name_lock_label.setText("🔒")
        self.app_changed.emit()

    def _commit_scraper_field(self, field: str, widget: QLineEdit) -> None:
        """
        Commit publisher / homepage_url. Skips the write if the text
        hasn't actually changed, because editingFinished also fires on
        focus-out and we must not clobber a scraped value just because
        the user tabbed through the field.
        """
        new_value = widget.text().strip()
        if new_value == self._baseline.get(field, ""):
            return

        if self._multi_mode:
            targets = list(self._multi_app_ids)
        elif self.current_app_id is not None:
            targets = [self.current_app_id]
        else:
            return

        conn = self.db.connect()
        for app_id in targets:
            conn.execute(
                f"UPDATE apps SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
                (new_value, app_id),
            )
        conn.commit()
        self._baseline[field] = new_value
        self.app_changed.emit()

    def _commit_tags(self) -> None:
        text = self.scraped_tags_edit.text().strip()
        if text == self._baseline.get("tags", ""):
            return
        tags = [t.strip() for t in text.split(",") if t.strip()]

        if self._multi_mode:
            targets = list(self._multi_app_ids)
        elif self.current_app_id is not None:
            targets = [self.current_app_id]
        else:
            return

        for app_id in targets:
            _set_app_tags(self.db, app_id, tags)
        self._baseline["tags"] = text
        self.app_changed.emit()

    def _on_catalog_changed(self, text):
        if self._multi_mode or self.current_app_id is not None:
            self._commit_field("catalog", self.catalog_edit)

    def _on_subcatalog_changed(self, text):
        if self._multi_mode or self.current_app_id is not None:
            self._commit_field("subcatalog", self.subcatalog_edit)

    def _mark_verified(self):
        if self._multi_mode:
            for app_id in self._multi_app_ids:
                set_app_status(self.db, app_id, "verified")
            self.app_changed.emit()
            self.load_apps(self._multi_app_ids)
            return
        if self.current_app_id is None:
            return
        set_app_status(self.db, self.current_app_id, "verified")
        self.load_app(self.current_app_id)
        self.app_changed.emit()

    def _selected_variant_id(self) -> int | None:
        items = self.variants_table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _selected_variant_row(self) -> dict | None:
        vid = self._selected_variant_id()
        if vid is None:
            return None
        conn = self.db.connect()
        row = conn.execute("SELECT * FROM variants WHERE id = ?", (vid,)).fetchone()
        return dict(row) if row else None

    def _ignore_selected_variant(self):
        vid = self._selected_variant_id()
        if vid is None:
            QMessageBox.information(self, "No selection", "Select a variant row first.")
            return
        set_variant_ignored(self.db, vid, ignored=True)
        self.load_app(self.current_app_id)
        self.app_changed.emit()

    def _on_variant_item_changed(self, item):
        """Commit an in-place edit of a variant's version or edition.
        Sets version_locked so a later Re-resolve won't clobber it."""
        if self._loading_variants:
            return
        col = item.column()
        if not (0 <= col < len(VARIANT_COLUMNS)):
            return
        key = VARIANT_COLUMNS[col][0]
        if key not in EDITABLE_VARIANT_COLUMNS:
            return
        vid = item.data(Qt.UserRole)
        if vid is None:
            return
        new_value = item.text().strip()
        conn = self.db.connect()
        row = conn.execute("SELECT * FROM variants WHERE id = ?", (vid,)).fetchone()
        if row is None:
            return
        old_value = (row[key] or "")
        if new_value == old_value:
            return
        conn.execute(
            f"UPDATE variants SET {key} = ?, version_locked = 1, "
            f"updated_at = datetime('now') WHERE id = ?",
            (new_value or None, vid),
        )
        conn.commit()
        # Defer the refresh so we don't tear down the table mid-edit.
        QTimer.singleShot(0, self.app_changed.emit)

    def _delete_selected_variants(self):
        vids = {item.data(Qt.UserRole)
                for item in self.variants_table.selectedItems()}
        vids = [v for v in vids if v is not None]
        if not vids:
            QMessageBox.information(self, "No selection", "Select a variant row first.")
            return
        confirm = QMessageBox.question(
            self, "Delete variant(s)",
            f"Delete {len(vids)} variant(s) from this app?\n\n"
            "This removes the catalog record only — no files are touched on disk. "
            "A future re-scan can re-add any file that is still present.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        for vid in vids:
            delete_variant(self.db, vid)
        if self.current_app_id is not None:
            self.load_app(self.current_app_id)
        self.app_changed.emit()

    def _move_selected_variant(self):
        vid = self._selected_variant_id()
        if vid is None:
            QMessageBox.information(self, "No selection", "Select a variant row first.")
            return
        # Shared, searchable app picker (gui_backend.AppPickerDialog).
        # Excludes the current parent so a variant can't be "moved" to
        # the app it's already under.
        dialog = AppPickerDialog(self.db, exclude_app_id=self.current_app_id, parent=self)
        if dialog.exec():
            target_id = dialog.selected_app_id()
            if target_id:
                move_variant_to_app(self.db, vid, target_id)
                self.load_app(self.current_app_id)
                self.app_changed.emit()

    def _split_selected_variant(self):
        vid = self._selected_variant_id()
        if vid is None:
            QMessageBox.information(self, "No selection", "Select a variant row first.")
            return
        # The same dialog as "Add app", prefilled from the variant (installer file,
        # app folder, version, edition, ... all editable).
        conn = self.db.connect()
        v = conn.execute("SELECT * FROM variants WHERE id = ?", (vid,)).fetchone()
        a = conn.execute("SELECT * FROM apps WHERE id = ?", (v["app_id"],)).fetchone()
        dlg = AddAppDialog(self.db, parent=self, split_variant=dict(v), split_app=dict(a))
        if dlg.exec() != QDialog.Accepted or not dlg.result_info:
            return
        if conn.execute("SELECT 1 FROM apps WHERE id = ?", (self.current_app_id,)).fetchone():
            self.load_app(self.current_app_id)       # (the old app is deleted if this was its only variant)
        self.app_changed.emit()
        if dlg.scrape_after:
            self.scrape_requested.emit([dlg.result_info["app_id"]])

    def _selected_variant_rows(self) -> list[dict]:
        vids = {item.data(Qt.UserRole) for item in self.variants_table.selectedItems()}
        if not vids:
            return []
        conn = self.db.connect()
        rows = []
        for vid in vids:
            row = conn.execute("SELECT * FROM variants WHERE id = ?", (vid,)).fetchone()
            if row:
                rows.append(dict(row))
        return rows


    def _change_variant_installer_file(self, variant: dict):
        """
        Let the user pick a different installer file for this variant.
        Opens a normal file dialog rooted at the variant's source folder.
        The chosen file's path RELATIVE to source_path (e.g. "setup.exe"
        for a flat install unit, "Set/setup.exe" for a file inside a
        single_app subtree) is stored on variants.file_name, and
        variants.file_locked is set to 1, so the scanner keeps using this
        file instead of auto-picking on future re-scans.

        If the user picks a file OUTSIDE source_path, the scanner could never
        find it again (it only sees files under source_path). The user is told
        so and can OVERRIDE: the variant's folder is widened to the common
        parent folder of both, that folder becomes one single-app unit with
        the chosen file as its locked entry (see _offer_installer_override and
        app_curation.apply_installer_override).
        """
        source_path = variant["source_path"]
        if not os.path.isdir(source_path):
            QMessageBox.warning(
                self, "Folder not found",
                f"The variant's source folder no longer exists:\n\n{source_path}",
            )
            return

        current_full = self._variant_full_path(variant)
        start_dir = os.path.dirname(current_full) if os.path.isfile(current_full) else source_path
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose installer file", start_dir,
            "Installer files (*.exe *.msi *.msix *.msixbundle *.zip *.rar *.7z *.iso)"
            ";;All files (*)",
        )
        if not chosen:
            return

        # Compute the path of the chosen file RELATIVE to the variant's
        # source folder -- this is exactly what the scanner needs to match
        # it back on a re-scan (source_path + "/" + file_name must resolve
        # to the chosen file). Works identically for a flat install unit
        # (relpath is just the basename) and a single_app subtree (relpath
        # may include subfolders like "Set/setup.exe").
        try:
            rel = os.path.relpath(chosen, source_path)
        except ValueError:
            # Windows: different drive letters -- relpath raises. Treat as
            # "outside source_path" below.
            rel = ""

        if not rel or rel.startswith(".."):
            self._offer_installer_override(variant, chosen)
            return

        conn = self.db.connect()
        conn.execute(
            "UPDATE variants SET file_name = ?, file_locked = 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (rel, variant["id"]),
        )
        conn.commit()
        self.load_app(self.current_app_id)
        self.app_changed.emit()
        
        
    def _offer_installer_override(self, variant: dict, chosen: str):
        """The chosen installer is outside the variant's folder (e.g. one level
        up, in a sibling sub-folder): explain, and offer to use it anyway."""
        source_path = variant["source_path"]
        plan = app_curation.plan_installer_override(self.db, variant["id"], chosen)
        if not plan["ok"]:
            QMessageBox.warning(self, "Can't use that file", plan["reason"])
            return
        sw = plan["swallowed"]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Outside the app folder")
        box.setText("The file you picked is not inside this variant's app folder.")
        info = (
            f"App folder:\n  {source_path}\n\nChosen file:\n  {chosen}\n\n"
            "The scanner only looks inside a variant's own folder, so to use this file the "
            "variant would be widened to the folder that contains both:\n"
            f"  {plan['new_root']}\n\n"
            f"That whole folder ({plan['sub_folders']} sub-folders, {plan['installer_like_files']} "
            f"installer-like files) becomes ONE app/variant with “{plan['rel']}” as its installer. "
            "The installer choice is locked, so re-scans keep it.")
        if sw:
            listing = "\n".join(f"  • {s['app']}  —  {s['file'] or ''}" for s in sw[:8])
            more = f"\n  … and {len(sw) - 8} more" if len(sw) > 8 else ""
            info += (f"\n\n{len(sw)} variant(s) already catalogued inside that folder become part "
                     f"of this one and will be REMOVED from the catalog:\n{listing}{more}\n"
                     "(typically leftovers the scanner made from the unit's own sub-folders).")
        box.setInformativeText(info)
        use = box.addButton(
            f"Use it and remove the {len(sw)} other variant(s)" if sw else "Use it anyway",
            QMessageBox.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not use:
            return
        try:
            app_curation.create_backup(self.db.path, "before-override", keep=10)
            app_curation.apply_installer_override(
                self.db, variant["id"], chosen, remove_swallowed=True)
        except Exception as e:
            QMessageBox.warning(self, "Override failed", str(e))
            return
        QMessageBox.information(
            self, "Installer overridden",
            "Done. Re-scan this scan root once so the scanner picks up the widened folder.")
        self.load_app(self.current_app_id)
        self.app_changed.emit()

    def _clear_variant_installer_file(self, variant: dict):
        """Drop the override. Auto-pick takes over on the next re-scan;
        the stored file_name stays as-is until then."""
        conn = self.db.connect()
        conn.execute(
            "UPDATE variants SET file_locked = 0, "
            "updated_at = datetime('now') WHERE id = ?",
            (variant["id"],),
        )
        conn.commit()
        self.load_app(self.current_app_id)
        self.app_changed.emit()

    def _show_variant_context_menu(self, pos):
        rows = self._selected_variant_rows()
        if not rows:
            return
        menu = QMenu(self)
        open_location_action = menu.addAction("Open file location")
        run_action = menu.addAction("Run / open file")
        menu.addSeparator()
        change_file_action = menu.addAction("Change installer file…")
        clear_file_action = menu.addAction("Clear installer file override")
        menu.addSeparator()
        manifest_action = menu.addAction("Create/update manifest")
        menu.addSeparator()
        reeval_action = menu.addAction("Re-evaluate selected")
        scrape_action = menu.addAction("Scrape app metadata (Winget)")
        choco_action = menu.addAction("Search & match…")
        menu.addSeparator()
        ignore_action = menu.addAction("Ignore selected")
        delete_variants_action = menu.addAction("Delete selected variant(s)")

        single = rows[0] if len(rows) == 1 else None
        open_location_action.setEnabled(single is not None)
        run_action.setEnabled(single is not None)
        change_file_action.setEnabled(single is not None)
        has_override = bool(single and single.get("file_locked"))
        clear_file_action.setEnabled(has_override)
        if single is not None and single.get("raw_candidate_id") is None:
            # Monitor-created variants have no scan-root folder to key an
            # override against -- the scan wouldn't know where to look.
            change_file_action.setEnabled(False)
            change_file_action.setToolTip(
                "Not available for monitored files (no scan-root folder to attach to)"
            )

        chosen = menu.exec(self.variants_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == open_location_action and single:
            self._open_file_location(self._variant_full_path(single))
        elif chosen == run_action and single:
            self._run_file(self._variant_full_path(single))
        elif chosen == change_file_action and single:
            self._change_variant_installer_file(single)
        elif chosen == clear_file_action and single:
            self._clear_variant_installer_file(single)
        elif chosen == manifest_action:
            res = app_manifest.write_manifests(self.db, [r["id"] for r in rows])
            QMessageBox.information(self, "Manifest", f"{res.summary()}.")
        elif chosen == reeval_action:
            self._reresolve()
        elif chosen == scrape_action:
            if self.current_app_id is not None:
                self.scrape_requested.emit([self.current_app_id])
        elif chosen == choco_action:
            if self.current_app_id is not None:
                self._open_search_match()
        elif chosen == ignore_action:
            self._ignore_selected_variant()
        elif chosen == delete_variants_action:
            self._delete_selected_variants()
            
    def _open_search_match(self):
        settings = self.db.get_all_settings()
        dialog = SearchMatchDialog(
            self.db, self.current_app_id, self.name_edit.text() or "", settings, parent=self
        )
        if dialog.exec():
            self.load_app(self.current_app_id)
            self.app_changed.emit()
            
    def _variant_full_path(self, variant: dict) -> str:
        if variant.get("file_name"):
            return os.path.normpath(
                os.path.join(variant["source_path"], variant["file_name"])
            )
        return variant["source_path"]

    def _open_file_location(self, full_path: str):
        full_path = os.path.normpath(full_path)
        print(f"[open-location] {full_path}")

        folder = full_path if os.path.isdir(full_path) else os.path.dirname(full_path)
        if not os.path.exists(full_path):
            if os.path.isdir(folder):
                reply = QMessageBox.question(
                    self, "File not found",
                    f"This exact file no longer exists:\n\n{full_path}\n\n"
                    f"The containing folder does exist -- open it instead?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if reply != QMessageBox.Yes:
                    return
            else:
                QMessageBox.warning(
                    self, "Location not found",
                    f"Neither the file nor its folder exist anymore at the recorded path:\n\n{full_path}\n\n"
                    "It may have been moved, renamed, or the drive isn't currently connected.",
                )
                return
        try:
            system = platform.system()
            if system == "Windows":
                if os.path.isfile(full_path):
                    subprocess.run(f'explorer /select,"{full_path}"', shell=True)
                else:
                    os.startfile(folder)
            elif system == "Darwin":
                subprocess.run(["open", "-R", full_path] if os.path.isfile(full_path) else ["open", folder])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception as e:
            QMessageBox.warning(self, "Could not open location", str(e))
            
            
    def _run_file(self, full_path: str):
        if not os.path.exists(full_path):
            QMessageBox.warning(self, "File not found", f"{full_path}\n\ndoes not exist on disk.")
            return
        confirm = QMessageBox.question(
            self, "Run file?",
            f"This will run:\n\n{full_path}\n\n"
            "This may launch an installer or make changes to your system. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(full_path)
            elif system == "Darwin":
                subprocess.run(["open", full_path])
            else:
                subprocess.run(["xdg-open", full_path])
        except Exception as e:
            QMessageBox.warning(self, "Could not run file", str(e))

    def _reresolve(self):
        if self._multi_mode:
            n = len(self._multi_app_ids)
            confirm = QMessageBox.question(
                self, "Re-resolve all?",
                f"Re-resolve {n} selected apps against the current settings?\n\n"
                "Every unlocked name/catalog/subcatalog field and every unlocked "
                "variant will be re-derived. Locked fields are never touched.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            for app_id in list(self._multi_app_ids):
                proposal = propose_reresolve_app(self.db, app_id)
                accepted_app_fields = {
                    f for f in ("name", "catalog", "subcatalog")
                    if not proposal[f"{f}_locked"]
                }
                accepted_variant_ids = {
                    vp["variant_id"] for vp in proposal["variants"]
                    if not vp["version_locked"]
                }
                apply_reresolve_app(
                    self.db, proposal, accepted_app_fields, accepted_variant_ids
                )
            self.app_changed.emit()
            self.load_apps(self._multi_app_ids)
            return

        if self.current_app_id is None:
            return
        proposal = propose_reresolve_app(self.db, self.current_app_id)
        dialog = ReresolveDialog(proposal, parent=self)
        if dialog.exec():
            accepted_app_fields, accepted_variant_ids = dialog.accepted_selection()
            apply_reresolve_app(self.db, proposal, accepted_app_fields, accepted_variant_ids)
            self.load_app(self.current_app_id)
            self.app_changed.emit()

    def apply_alt_name(self, source: str):
        if self.current_app_id is None:
            return
        conn = self.db.connect()
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (self.current_app_id,)).fetchone()
        if row is None:
            return
        app = dict(row)
        if source == 'original':
            name = app.get('name') or ''
        elif source == 'alternative':
            name = app.get('alt_name_candidate') or ''
        elif source == 'winget':
            name = app.get('manifest_name') or ''
        elif source == 'choco':
            name = app.get('choco_id') or ''
        else:
            return
        if name:
            self.name_edit.setText(name)


# =============================================================
# Dialogs
# =============================================================


class SearchMatchDialog(QDialog):
    SOURCE_WINGET = "Winget (local manifest, instant)"
    SOURCE_CHOCO = "Chocolatey (live search)"

    def __init__(self, db: Database, app_id: int, app_name: str, settings: dict, parent=None):
        super().__init__(parent)
        self.db = db
        self.app_id = app_id
        self.settings = settings
        self.results: list[dict] = []
        self._worker = None
        self.setWindowTitle(f"Search & match — {app_name}")
        self.resize(760, 460)
        layout = QVBoxLayout(self)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItems([self.SOURCE_WINGET, self.SOURCE_CHOCO])
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        source_row.addWidget(self.source_combo)
        source_row.addStretch()
        layout.addLayout(source_row)

        search_row = QHBoxLayout()
        self.search_box = QLineEdit(app_name)
        self.search_box.returnPressed.connect(self._start_search)
        search_row.addWidget(self.search_box, stretch=1)
        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(1, 20)
        self.max_results_spin.setValue(int(settings.get("scraper_choco_default_max_results", 5)))
        search_row.addWidget(QLabel("Max results:"))
        search_row.addWidget(self.max_results_spin)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._start_search)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(["Name", "Version", "Publisher", "Match %", "Id"])
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.results_table)

        self.description_preview = QLabel("")
        self.description_preview.setWordWrap(True)
        self.description_preview.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.description_preview)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QPushButton("Apply selected")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply_selected)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.apply_btn)
        layout.addLayout(btn_row)

        self.results_table.itemSelectionChanged.connect(self._on_selection_changed)

    def _is_winget(self) -> bool:
        return self.source_combo.currentText() == self.SOURCE_WINGET

    def _on_source_changed(self):
        self.results = []
        self.results_table.setRowCount(0)
        self.description_preview.setText("")
        self.apply_btn.setEnabled(False)
        self.status_label.setText("")

    def _start_search(self):
        term = self.search_box.text().strip()
        if not term:
            return
        self.search_btn.setEnabled(False)
        self.status_label.setText("Searching…")
        self.results_table.setRowCount(0)
        self.description_preview.setText("")
        self.apply_btn.setEnabled(False)
        if self._is_winget():
            worker = WingetSearchWorker(self.db.path, term, self.max_results_spin.value(), parent=self)
        else:
            worker = ChocoSearchWorker(term, self.max_results_spin.value(), parent=self)
        worker.finished_ok.connect(self._on_search_finished)
        worker.failed.connect(self._on_search_failed)
        self._worker = worker
        worker.start()

    def _on_search_finished(self, results: list):
        self.search_btn.setEnabled(True)
        self.results = results
        self.status_label.setText(f"{len(results)} result(s) found" if results else "No results found")
        self.results_table.setRowCount(len(results))
        for i, pkg in enumerate(results):
            id_val = pkg.get("winget_id") if self._is_winget() else pkg.get("choco_id", pkg.get("id", ""))
            values = [
                pkg.get("name", ""), pkg.get("version", ""), pkg.get("company", ""),
                str(pkg.get("match_percent", "")), id_val,
            ]
            for j, val in enumerate(values):
                self.results_table.setItem(i, j, QTableWidgetItem(str(val)))
        self.results_table.resizeColumnsToContents()

    def _on_search_failed(self, error_message: str):
        self.search_btn.setEnabled(True)
        self.status_label.setText("Search failed.")
        QMessageBox.warning(self, f"{self.source_combo.currentText()} search failed", error_message)

    def _on_selection_changed(self):
        row = self.results_table.currentRow()
        has_selection = 0 <= row < len(self.results)
        self.apply_btn.setEnabled(has_selection)
        self.description_preview.setText(self.results[row].get("description", "") if has_selection else "")

    def _apply_selected(self):
        row = self.results_table.currentRow()
        if row < 0 or row >= len(self.results):
            return
        candidate = self.results[row]
        choose_name = QMessageBox.question(
            self, "Rename app?",
            f'Also rename this app to "{candidate.get("name","")}"?\n\n'
            "Choosing No keeps the current name and only updates description/publisher/"
            "URL/version/license/tags.",
        ) == QMessageBox.Yes
        try:
            if self._is_winget():
                apply_manifest_candidate(self.db, self.app_id, candidate,
                                          choose_name=choose_name, settings=self.settings)
            else:
                apply_choco_candidate(self.db, self.app_id, candidate, choose_name=choose_name)
        except Exception as e:
            QMessageBox.critical(self, "Apply failed", str(e))
            return
        self.accept()


class FolderLayoutDialog(QDialog):
    """
    Per-scan-root folder layout editor (Feature B, redesigned checkpoint
    28). Every folder in the tree gets one explicit, self-describing
    ROLE from the same dropdown regardless of depth: Catalog /
    Subcatalog / App / Single App/Variant / Skip. An unconfigured folder's default cascades
    from its parent's role (Catalog's children default to Subcatalog,
    Subcatalog's children default to App, App's children stay App --
    they're just internal/version folders at that point) -- a top-level
    folder with no parent in the tree defaults to this root's own
    `unconfigured_toplevel_role` ("catalog" normally, "skip" for a root
    created via the single-catalog "-1 level" promotion in
    `ScanRootsDialog._add_new_root()`). See scanner.resolve_scan_root_
    layout() for how these roles translate into catalog/subcatalog/depth.

    The first two folder levels are populated immediately (no clicking
    needed) since that's the common "maybe one folder is mixed" case;
    anything deeper is resolved lazily the moment a level-2 row is
    expanded, so opening the dialog never has to walk more of the disk
    than a handful of os.scandir() calls.
    """

    ROLE_ITEMS = [("Catalog", "catalog"), ("Subcatalog", "subcatalog"),
                  ("App", "app"), ("Single App/Variant", "single_app"), ("Skip", "skip")]
    ROLE_CASCADE = {"catalog": "subcatalog", "subcatalog": "app", "app": "app",
                     "single_app": "single_app", "skip": "skip"}

    def __init__(self, db: Database, scan_root_row: dict, parent=None,
                 only_new_folders: Optional[list] = None):
        super().__init__(parent)
        self.db = db
        self.scan_root_row = scan_root_row
        self.root_path = scan_root_row["path"]
        # Informational only -- which top-level folders are newly found on
        # this re-scan (per the user's choice: always show the FULL
        # dialog, with new rows simply pre-filled at default, rather than
        # hiding already-configured rows).
        self.new_folder_names = (
            {n.lower() for n in only_new_folders} if only_new_folders else set()
        )
        try:
            self._saved_layout = json.loads(scan_root_row.get("folder_layouts_json") or "{}")
        except (TypeError, ValueError):
            self._saved_layout = {}
        # Working copy the dialog edits live; only written back on OK.
        self._working_layout = dict(self._saved_layout)
        self._rows = {}  # rel_key -> {"item", "combo", "rename", "preview", "rel_parts"}
        # Populated by _on_ok() with the LayoutChangeResult from
        # apply_layout_change() -- read by ScanRootsDialog to build the
        # post-save status-bar message. None if the dialog was cancelled
        # or nothing needed propagating.
        self.layout_change_result = None

        title = "Folder layout — " + self.root_path
        if self.new_folder_names:
            title += "  (new folders found)"
        self.setWindowTitle(title)
        self.resize(920, 540)
        outer = QVBoxLayout(self)

        intro_text = (
            "Declare what each folder IS: a Catalog, a Subcatalog, the App itself, or "
            "Skip it entirely. An unconfigured folder defaults to whatever makes sense "
            "below its parent (a Catalog's children default to Subcatalog, a "
            "Subcatalog's children default to App) -- expand a row (▸) and change any "
            "individual folder that doesn't fit the pattern, at any depth. The Preview "
            "column shows what that folder actually resolves to."
        )
        if self.new_folder_names:
            intro_text = (
                f"{len(self.new_folder_names)} new top-level folder(s) were found since this "
                "root was last configured (marked \"NEW\" below) -- everything else keeps its "
                "saved setting. " + intro_text
            )
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # -- top strip: default for a top-level folder with no entry -----
        top_strip = QHBoxLayout()
        top_strip.addWidget(QLabel("New top-level folders here default to:"))
        self.default_toplevel_combo = QComboBox()
        self.default_toplevel_combo.addItem("Catalog (normal root)", "catalog")
        self.default_toplevel_combo.addItem("Skip (only explicitly-added folders are scanned)", "skip")
        current_default = scan_root_row.get("unconfigured_toplevel_role") or "catalog"
        idx = self.default_toplevel_combo.findData(current_default)
        self.default_toplevel_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.default_toplevel_combo.currentIndexChanged.connect(self._on_default_toplevel_changed)
        top_strip.addWidget(self.default_toplevel_combo)
        top_strip.addStretch()
        outer.addLayout(top_strip)

        # -- main tree ----------------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Folder", "Role", "Rename (optional)", "Preview"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 180)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        outer.addWidget(self.tree)

        self._populate_top_level()

        # -- footer ---------------------------------------------------------
        footer = QHBoxLayout()
        default_all_btn = QPushButton("Use default for all")
        default_all_btn.setToolTip("Resets every visible row to its cascaded default role.")
        default_all_btn.clicked.connect(self._use_default_for_all)
        footer.addWidget(default_all_btn)
        expand_all_btn = QPushButton("Expand all")
        expand_all_btn.clicked.connect(self.tree.expandAll)
        footer.addWidget(expand_all_btn)
        collapse_all_btn = QPushButton("Collapse all")
        collapse_all_btn.clicked.connect(self.tree.collapseAll)
        footer.addWidget(collapse_all_btn)
        footer.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setToolTip("Aborts the scan job entirely -- no candidates are written.")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._on_ok)
        footer.addWidget(ok_btn)
        outer.addLayout(footer)

    # ------------------------------------------------------------------
    # role/default helpers
    # ------------------------------------------------------------------
    def _explicit_role_and_name(self, rel_key: str):
        entry = self._working_layout.get(rel_key)
        if isinstance(entry, dict):
            return entry.get("role"), entry.get("name")
        if entry in ("catalog", "subcatalog", "app", "single_app", "skip"):
            return entry, None
        return None, None

    def _default_role_for(self, rel_parts: tuple) -> str:
        """Cascades down from the nearest ANCESTOR's role (checking the
        working layout directly, not the tree widget, so this stays
        correct even for a not-yet-populated row) -- or this root's
        top-level default when there's no parent at all."""
        if len(rel_parts) == 1:
            return self.default_toplevel_combo.currentData()
        parent_key = "/".join(p.lower() for p in rel_parts[:-1])
        parent_role, _ = self._explicit_role_and_name(parent_key)
        if parent_role is None:
            parent_role = self._default_role_for(rel_parts[:-1])
        return self.ROLE_CASCADE.get(parent_role, "app")

    # ------------------------------------------------------------------
    # tree population
    # ------------------------------------------------------------------
    def _populate_top_level(self):
        names = list_top_level_folders(self.root_path)
        for name in names:
            label = f"{name}   (NEW)" if name.lower() in self.new_folder_names else name
            item = QTreeWidgetItem(self.tree, [label])
            self.tree.addTopLevelItem(item)
            self._add_row(item, (name,))
            # Level 2 is populated immediately (not lazy) -- this is the
            # "mixed catalog" case the user actually has, so it shouldn't
            # require an extra click to discover.
            self._populate_children(item, (name,))

    def _populate_children(self, parent_item: QTreeWidgetItem, rel_parts: tuple):
        child_names = list_child_folders(self.root_path, os.path.join(*rel_parts))
        for name in child_names:
            child_parts = rel_parts + (name,)
            child_item = QTreeWidgetItem(parent_item, [name])
            self._add_row(child_item, child_parts)
            # Lazy placeholder: a dummy child gives this row an expand
            # arrow without touching the disk again until the user
            # actually clicks it (any depth beyond level 2).
            QTreeWidgetItem(child_item, ["…loading…"])

    def _on_item_expanded(self, item: QTreeWidgetItem):
        # Real children already populated (level <=2, or already expanded
        # once) -- nothing to do.
        if item.childCount() != 1 or item.child(0).text(0) != "…loading…":
            return
        item.takeChildren()
        rel_parts = item.data(0, Qt.UserRole + 1)
        self._populate_children(item, rel_parts)

    def _add_row(self, item: QTreeWidgetItem, rel_parts: tuple):
        rel_key = "/".join(p.lower() for p in rel_parts)
        item.setData(0, Qt.UserRole, rel_key)
        item.setData(0, Qt.UserRole + 1, rel_parts)

        existing_role, existing_name = self._explicit_role_and_name(rel_key)
        default_role = self._default_role_for(rel_parts)

        combo = QComboBox()
        for label, value in self.ROLE_ITEMS:
            combo.addItem(label, value)
        idx = combo.findData(existing_role if existing_role is not None else default_role)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.currentIndexChanged.connect(lambda _i, k=rel_key: self._on_row_changed(k))
        self.tree.setItemWidget(item, 1, combo)

        rename_edit = QLineEdit(existing_name or "")
        rename_edit.setPlaceholderText("(use folder name)")
        rename_edit.textChanged.connect(lambda _t, k=rel_key: self._on_row_changed(k))
        combo.currentIndexChanged.connect(lambda _i, k=rel_key: self._update_rename_placeholder(k))
        self.tree.setItemWidget(item, 2, rename_edit)

        preview_label = QLabel("")
        self.tree.setItemWidget(item, 3, preview_label)

        self._rows[rel_key] = {
            "item": item, "combo": combo, "rename": rename_edit,
            "preview": preview_label, "rel_parts": rel_parts, "default_role": default_role,
        }
        self._update_row_layout_entry(rel_key)
        self._update_preview(rel_key)
        self._update_rename_placeholder(rel_key)

    def _update_rename_placeholder(self, rel_key: str):
        # The rename box means "category label" for Catalog/Subcatalog,
        # nothing in particular for App -- but for Single App/Variant it's
        # the app/variant NAME override, which is a different enough
        # meaning that it's worth flagging right on the field itself
        # rather than only in the role dropdown text.
        row = self._rows.get(rel_key)
        if not row:
            return
        role = row["combo"].currentData()
        if role == "single_app":
            row["rename"].setPlaceholderText("(app/variant name — leave blank to auto-name)")
        else:
            row["rename"].setPlaceholderText("(use folder name)")

    # ------------------------------------------------------------------
    # live editing
    # ------------------------------------------------------------------
    def _on_row_changed(self, rel_key: str):
        self._update_row_layout_entry(rel_key)
        self._refresh_all_previews()

    def _on_default_toplevel_changed(self):
        # Changes what an UNCONFIGURED top-level row's default is -- only
        # affects rows that don't already have an explicit entry, and only
        # visually until OK is pressed (recomputing each such row's combo
        # selection + preview, without touching rows the user has already
        # set explicitly).
        for rel_key, row in self._rows.items():
            if len(row["rel_parts"]) != 1:
                continue
            existing_role, _ = self._explicit_role_and_name(rel_key)
            if existing_role is not None:
                continue
            new_default = self.default_toplevel_combo.currentData()
            row["default_role"] = new_default
            idx = row["combo"].findData(new_default)
            row["combo"].blockSignals(True)
            row["combo"].setCurrentIndex(idx if idx >= 0 else 0)
            row["combo"].blockSignals(False)
        self._refresh_all_previews()

    def _update_row_layout_entry(self, rel_key: str):
        row = self._rows.get(rel_key)
        if not row:
            return
        role = row["combo"].currentData()
        name = row["rename"].text().strip()
        if role == row["default_role"] and not name:
            # Matches the cascaded default with no rename -- no entry
            # needed (keeps the saved JSON small).
            self._working_layout.pop(rel_key, None)
        elif name:
            self._working_layout[rel_key] = {"role": role, "name": name}
        else:
            self._working_layout[rel_key] = role

    def _refresh_all_previews(self):
        for rel_key in self._rows:
            self._update_preview(rel_key)

    def _update_preview(self, rel_key: str):
        row = self._rows.get(rel_key)
        if not row:
            return
        rel_parts = row["rel_parts"]
        role = row["combo"].currentData()
        if role == "skip":
            row["preview"].setText("(not imported — skipped)")
            return
        # Every role is now self-describing (Catalog/Subcatalog/App all
        # mean "this is what I am", never "this is what my children are")
        # so previewing is just resolving THIS folder's own path directly
        # -- no synthetic child needed, unlike the old mode system.
        sample_path = os.path.join(self.root_path, *rel_parts)
        catalog, subcatalog, depth, skip, is_single_app, forced_name = resolve_scan_root_layout(
            self.root_path, sample_path, self._working_layout,
            unconfigured_toplevel_role=self.default_toplevel_combo.currentData(),
        )
        if skip:
            row["preview"].setText("(not imported — skipped)")
        elif role == "catalog":
            row["preview"].setText(f"Catalog: {catalog}")
        elif role == "subcatalog":
            row["preview"].setText(f"{catalog or '—'} / {subcatalog or '—'}  (subcategory)")
        elif role == "single_app":
            # Whole subtree becomes ONE app/variant candidate -- no
            # separate row per exe/msi component inside it. Name shown
            # here mirrors resolver behavior: the manual rename verbatim
            # if set, else "(auto-named)" since the real cascade only
            # runs at resolve time (needs the picked installer's PE data).
            label = forced_name or "(auto-named)"
            row["preview"].setText(
                f"{catalog or '—'} / {subcatalog or '—'} / {label}  (single app/variant — subfolders merged)"
            )
        else:  # app
            row["preview"].setText(f"{catalog or '—'} / {subcatalog or '—'} / {rel_parts[-1]}")

    def _use_default_for_all(self):
        for rel_key, row in self._rows.items():
            row["rename"].clear()
            idx = row["combo"].findData(row["default_role"])
            row["combo"].setCurrentIndex(idx if idx >= 0 else 0)

    # ------------------------------------------------------------------
    # save
    # ------------------------------------------------------------------
    def _on_ok(self):
        # Top-level rows are always written explicitly (even at the
        # default), so a future re-scan doesn't treat an already-seen,
        # unchanged folder as "new" again.
        for rel_key, row in self._rows.items():
            if len(row["rel_parts"]) == 1 and rel_key not in self._working_layout:
                self._working_layout[rel_key] = row["combo"].currentData()

        # Prune keys whose folder no longer exists on disk (e.g. renamed/
        # deleted since the config was last saved) -- low priority but
        # cheap to do on every save.
        pruned = _prune_orphaned_layout_keys(self.root_path, self._working_layout)

        # Capture the pre-save top-level default so we can diff old vs
        # new meaningfully below; save_folder_layout() writes the new
        # one, so we must read the old value first.
        old_toplevel_role = (
            self.scan_root_row.get("unconfigured_toplevel_role") or "catalog"
        )
        new_toplevel_role = self.default_toplevel_combo.currentData()

        # Save the layout first (that's what "save" means); then bring
        # the catalog into agreement with it. Role changes delete the
        # affected subtree's catalog entries so the next re-scan can
        # rebuild them cleanly; label-only changes just update
        # raw_candidates.catalog/subcatalog in place. See
        # app_manager.apply_layout_change() and the Layout-Change
        # Propagation plan for the full rationale.
        self.db.save_folder_layout(
            self.scan_root_row["id"], pruned,
            unconfigured_toplevel_role=new_toplevel_role,
        )
        self.layout_change_result = apply_layout_change(
            self.db, self.scan_root_row["id"],
            self._saved_layout, pruned,
            old_unconfigured_toplevel_role=old_toplevel_role,
            new_unconfigured_toplevel_role=new_toplevel_role,
        )
        self.accept()


def _folder_exists_case_insensitive(root_path: str, key_parts: list) -> bool:
    """
    Layout keys are stored lowercased, but the real folder names on disk
    keep their original casing -- os.path.isdir(root/lowercased/parts)
    would wrongly report "missing" on any case-sensitive filesystem (and,
    just as importantly, must never be relied on to be case-preserving on
    Windows either). Walks down one segment at a time, matching each
    segment case-insensitively via os.scandir.
    """
    current = root_path
    for part in key_parts:
        try:
            with os.scandir(current) as it:
                match = next((e.name for e in it if e.is_dir(follow_symlinks=False)
                              and e.name.lower() == part), None)
        except OSError:
            return False
        if match is None:
            return False
        current = os.path.join(current, match)
    return True


def _prune_orphaned_layout_keys(root_path: str, layout: dict) -> dict:
    """
    Drops layout keys whose folder no longer exists under root_path (e.g.
    the on-disk folder was renamed or deleted after the config was saved).
    Cheap (a handful of os.scandir calls per key) at the scale this app
    runs at.
    """
    pruned = {}
    for key, value in layout.items():
        if _folder_exists_case_insensitive(root_path, key.split("/")):
            pruned[key] = value
    return pruned


class ScanRootsDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Scan roots")
        self.resize(720, 320)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Re-scanning skips folders that haven't changed (matched by content "
            "fingerprint) and only processes new/changed ones. \"Clean library\" does "
            "the opposite -- it never looks for new apps, it only confirms the apps "
            "already in the catalog from that folder still exist on disk."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Path", "Last scan started", "Last scan finished", "Status", "", "", "", ""]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add new scan root…")
        add_btn.clicked.connect(self._add_new_root)
        btn_row.addWidget(add_btn)
        delete_btn = QPushButton("Delete selected root…")
        delete_btn.setToolTip(
            "Forgets the selected scan root and its staged scan data. Does NOT remove "
            "apps already in the catalog from it -- use this for a root added by "
            "mistake, a duplicate, or one that's permanently gone."
        )
        delete_btn.clicked.connect(self._delete_selected_root)
        btn_row.addWidget(delete_btn)
        btn_row.addSpacing(20)
        rescan_all_btn = QPushButton("Re-scan all roots")
        rescan_all_btn.setToolTip(
            "Re-scans every scan root above, one at a time (never in parallel). Each "
            "root still skips anything unchanged, same as its own \"Re-scan now\"."
        )
        rescan_all_btn.clicked.connect(self._rescan_all)
        btn_row.addWidget(rescan_all_btn)
        clean_all_btn = QPushButton("Clean library (all roots)")
        clean_all_btn.setToolTip(
            "Checks EVERY app in the catalog, from every scan root, for whether its "
            "file still exists on disk -- removes catalog records for anything that "
            "doesn't. Never looks for new apps."
        )
        clean_all_btn.clicked.connect(self._clean_library_all)
        btn_row.addWidget(clean_all_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._load_rows()

    def _load_rows(self):
        conn = self.db.connect()
        rows = conn.execute("SELECT * FROM scan_roots ORDER BY path").fetchall()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = [r["path"], r["last_scan_started_at"] or "", r["last_scan_finished_at"] or "",
                      r["last_scan_status"] or ""]
            for j, val in enumerate(values):
                item = QTableWidgetItem(val)
                if j == 0:
                    item.setData(Qt.UserRole, r["id"])
                self.table.setItem(i, j, item)
            rescan_btn = QPushButton("Re-scan now")
            rescan_btn.clicked.connect(lambda _checked, p=r["path"]: self._rescan(p))
            self.table.setCellWidget(i, 4, rescan_btn)
            clean_btn = QPushButton("Clean library")
            clean_btn.setToolTip(
                "Checks whether every app already in the catalog from this folder still "
                "exists on disk (does NOT look for new apps) -- for apps that were deleted, "
                "moved, or replaced by hand outside this app."
            )
            clean_btn.clicked.connect(lambda _checked, p=r["path"]: self._clean_library(p))
            self.table.setCellWidget(i, 5, clean_btn)
            repath_btn = QPushButton("Change path…")
            repath_btn.setToolTip(
                "Drive letter or mount point changed? Point this scan root at its new "
                "location -- every path already in the catalog from here gets rewritten "
                "to match. No re-scan needed."
            )
            repath_btn.clicked.connect(lambda _checked, rid=r["id"]: self._change_path(rid))
            self.table.setCellWidget(i, 6, repath_btn)
            layout_btn = QPushButton("Edit folder layout…")
            layout_btn.setToolTip(
                "Declare which top-level folders under this root have subcategories, "
                "have none, or should be skipped entirely."
            )
            layout_btn.clicked.connect(lambda _checked, rid=r["id"]: self._edit_layout(rid))
            self.table.setCellWidget(i, 7, layout_btn)
        self.table.resizeColumnsToContents()

    def _change_path(self, scan_root_id: int):
        row = self.db.get_scan_root_by_id(scan_root_id)
        if row is None:
            return
        new_path, ok = QInputDialog.getText(
            self, "Change scan root path",
            "New path for this scan root (rewrites every stored path under it):",
            text=row["path"],
        )
        if not ok or not new_path.strip():
            return
        new_path = new_path.strip()
        if not os.path.isdir(new_path):
            proceed = QMessageBox.question(
                self, "Path not found",
                f"'{new_path}' doesn't exist or isn't reachable right now (the drive "
                "may not be connected). The catalog update is still valid either way -- "
                "continue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if proceed != QMessageBox.Yes:
                return
        try:
            counts = update_scan_root_path(self.db, scan_root_id, new_path)
        except Exception as e:
            QMessageBox.critical(self, "Repath failed", f"Could not update the catalog:\n{e}")
            return
        QMessageBox.information(
            self, "Path updated",
            "Scan root path updated. Rows rewritten:\n"
            f"  raw_candidates: {counts['raw_candidates']}\n"
            f"  variants: {counts['variants']}\n"
            f"  scan_errors: {counts['scan_errors']}",
        )
        self._load_rows()
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "refresh_all"):
            main_window.refresh_all()

    def _edit_layout(self, scan_root_id: int):
        row = self.db.get_scan_root_by_id(scan_root_id)
        if row is None:
            return
        dialog = FolderLayoutDialog(self.db, row, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._load_rows()

        # No modal "saved" dialog: save is already a deliberate action,
        # and the post-save status-bar message is the one-second signal
        # to inspect before re-scanning. See the Layout-Change
        # Propagation plan section 3.5.
        result = getattr(dialog, "layout_change_result", None)
        mw = self.parent()
        if result is None or mw is None or not hasattr(mw, "status_label"):
            return
        if (result.raw_candidates_deleted or result.variants_deleted
                or result.apps_deleted or result.label_updates):
            mw.status_label.setText(
                f"Layout saved. Removed {result.apps_deleted} app(s) / "
                f"{result.variants_deleted} variant(s) "
                f"({result.folders_affected} folder(s) affected). "
                f"Re-scan to rebuild."
            )
        else:
            mw.status_label.setText("Layout saved. No catalog changes needed.")

    def _rescan(self, path: str):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_scan_and_resolve"):
            main_window._run_scan_and_resolve(path)
        self.accept()

    def _rescan_all(self):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_rescan_all_roots"):
            main_window._run_rescan_all_roots()
        self.accept()

    def _clean_library(self, path: str):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_clean_library"):
            main_window._run_clean_library(path)
        self.accept()

    def _clean_library_all(self):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_clean_library"):
            main_window._run_clean_library(None)
        self.accept()

    def _add_new_root(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to scan")
        if not folder:
            return
        folder = os.path.normpath(folder)

        is_single_catalog = QMessageBox.question(
            self, "Single catalog?",
            f"Is '{os.path.basename(folder)}' itself a single catalog, rather than a "
            "folder that CONTAINS multiple catalog subfolders?\n\n"
            "Choosing Yes stores the scan root as its PARENT folder instead, with this "
            "folder added as one explicit Catalog entry -- so adding several "
            "single-catalog folders that happen to share the same parent all land on one "
            "shared scan root instead of a separate one each.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if is_single_catalog != QMessageBox.Yes:
            self._rescan(folder)
            return

        default_name = os.path.basename(folder)
        catalog_name, ok = QInputDialog.getText(
            self, "Catalog name", "Display name for this catalog:", text=default_name,
        )
        if not ok:
            return
        catalog_name = catalog_name.strip() or default_name

        promoted_path = os.path.dirname(folder)
        scan_root_id = self.db.ensure_scan_root(promoted_path)
        layout = self.db.get_folder_layout(scan_root_id)
        key = default_name.lower()
        layout[key] = (
            {"role": "catalog", "name": catalog_name} if catalog_name != default_name else "catalog"
        )
        # Every sibling under the promoted parent that isn't explicitly
        # added this same way stays Skip by default -- adding ONE
        # single-catalog folder shouldn't silently start scanning
        # whatever else happens to live next to it.
        self.db.save_folder_layout(scan_root_id, layout, unconfigured_toplevel_role="skip")
        self._rescan(promoted_path)

    def _delete_selected_root(self):
        row_idx = self.table.currentRow()
        if row_idx < 0:
            QMessageBox.information(self, "No selection", "Select a scan root row first.")
            return
        path_item = self.table.item(row_idx, 0)
        scan_root_id = path_item.data(Qt.UserRole)
        path = path_item.text()

        confirm = QMessageBox.question(
            self, "Delete scan root",
            f"Forget this scan root?\n\n{path}\n\n"
            "This removes its staged scan data (raw candidates, scan errors). Apps "
            "already resolved into the catalog from it are NOT deleted -- they just "
            "lose their link back to this root. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            counts = delete_scan_root(self.db, scan_root_id)
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", f"Could not delete the scan root:\n{e}")
            return
        QMessageBox.information(
            self, "Scan root deleted",
            "Scan root removed. Rows affected:\n"
            f"  raw_candidates: {counts['raw_candidates']}\n"
            f"  scan_errors: {counts['scan_errors']}\n"
            f"  variants unlinked (apps kept): {counts['variants_unlinked']}",
        )
        self._load_rows()


class CleanLibraryReviewDialog(QDialog):
    """
    Review step for "Clean library": lists every variant whose source no
    longer exists on disk (already confirmed by CleanLibraryScanWorker --
    this dialog does no filesystem I/O itself), lets the user uncheck
    any it doesn't want removed, then calls execute_clean_library() on
    confirm. Same dry-run-then-confirm shape as Reorganize/Monitor.
    """

    def __init__(self, db: Database, db_path: str, missing: list, parent=None):
        super().__init__(parent)
        self.db = db
        self.db_path = db_path
        self.missing = missing
        self.setWindowTitle("Clean library — review")
        self.resize(760, 420)
        layout = QVBoxLayout(self)

        intro = QLabel(
            f"<b>{len(missing)} item(s)</b> are no longer valid: either the file itself is gone "
            "(deleted, moved, or replaced outside this app), its scan root's drive/folder isn't "
            "reachable right now, or its scan root was removed from this app entirely. See the "
            "Reason column for which. Uncheck anything you don't want removed from the catalog -- "
            "this only removes the catalog record, it never touches the filesystem."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(missing), 5)
        self.table.setHorizontalHeaderLabels(
            ["Remove", "App", "Version", "Reason", "Source path (no longer valid)"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self._checks = []
        for i, item in enumerate(missing):
            checkbox = QCheckBox()
            checkbox.setChecked(True)
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.addWidget(checkbox)
            cell_layout.setAlignment(Qt.AlignCenter)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(i, 0, cell)
            self._checks.append(checkbox)
            self.table.setItem(i, 1, QTableWidgetItem(item.app_name))
            self.table.setItem(i, 2, QTableWidgetItem(item.version))
            self.table.setItem(i, 3, QTableWidgetItem(getattr(item, "reason", "file not found")))
            self.table.setItem(i, 4, QTableWidgetItem(item.source_path))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        btn_row.addWidget(select_all_btn)
        select_none_btn = QPushButton("Select none")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        btn_row.addWidget(select_none_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.remove_btn = QPushButton()
        self.remove_btn.clicked.connect(self._do_remove)
        btn_row.addWidget(self.remove_btn)
        layout.addLayout(btn_row)

        for cb in self._checks:
            cb.stateChanged.connect(self._update_remove_label)
        self._update_remove_label()

    def _set_all_checked(self, checked: bool):
        for cb in self._checks:
            cb.setChecked(checked)

    def _update_remove_label(self):
        n = sum(1 for cb in self._checks if cb.isChecked())
        self.remove_btn.setText(f"Remove {n} selected from catalog")
        self.remove_btn.setEnabled(n > 0)

    def _do_remove(self):
        selected = [item for item, cb in zip(self.missing, self._checks) if cb.isChecked()]
        if not selected:
            return
        result = execute_clean_library(self.db, selected)

        summary = (
            f"Removed: {result.removed_variants}\n"
            f"Apps fully removed (no variants left): {result.removed_apps}"
        )
        report_note = ""
        if result.html_report_path and os.path.exists(result.html_report_path):
            try:
                webbrowser.open(Path(result.html_report_path).as_uri())
                report_note = "\n\nA detailed report has been opened in your browser."
            except Exception as e:
                report_note = f"\n\nDetailed report: {result.html_report_path}\n(Could not auto-open it: {e})"
        QMessageBox.information(self, "Clean library complete", summary + report_note)
        self.accept()


class ReresolveDialog(QDialog):
    def __init__(self, proposal: dict, parent=None):
        super().__init__(parent)
        self.proposal = proposal
        self.setWindowTitle("Re-resolve — review changes")
        self.resize(600, 500)
        self._app_checks: dict[str, QCheckBox] = {}
        self._variant_checks: dict[int, QCheckBox] = {}
        layout = QVBoxLayout(self)

        app_box = QGroupBox("App fields")
        form = QFormLayout(app_box)
        for field in ("name", "catalog", "subcatalog"):
            current = proposal[f"current_{field}"]
            proposed = proposal[f"proposed_{field}"]
            locked = proposal[f"{field}_locked"]
            if current == proposed:
                continue
            cb = QCheckBox(f'"{current or "(empty)"}"  →  "{proposed or "(empty)"}"')
            cb.setChecked(not locked)
            cb.setEnabled(not locked)
            if locked:
                cb.setToolTip("Locked by a manual edit — clear the lock first to change this")
            self._app_checks[field] = cb
            form.addRow(field.capitalize(), cb)
        if not self._app_checks:
            form.addRow(QLabel("No app-level field changes proposed."))
        layout.addWidget(app_box)

        variants_box = QGroupBox("Variant fields")
        vlayout = QVBoxLayout(variants_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        any_variant_changes = False
        for vp in proposal["variants"]:
            changes = []
            for f in ("version", "edition", "architecture", "language"):
                cur, prop = vp[f"current_{f}"], vp[f"proposed_{f}"]
                if cur != prop:
                    changes.append(f'{f}: "{cur or "(empty)"}" → "{prop or "(empty)"}"')
            if not changes:
                continue
            any_variant_changes = True
            label = f"{vp['source_path']}\n    " + " | ".join(changes)
            cb = QCheckBox(label)
            cb.setChecked(not vp["version_locked"])
            cb.setEnabled(not vp["version_locked"])
            if vp["version_locked"]:
                cb.setToolTip("Version is locked by a manual edit")
            self._variant_checks[vp["variant_id"]] = cb
            inner_layout.addWidget(cb)
        if not any_variant_changes:
            inner_layout.addWidget(QLabel("No variant field changes proposed."))
        inner_layout.addStretch()
        scroll.setWidget(inner)
        vlayout.addWidget(scroll)
        layout.addWidget(variants_box)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply selected changes")
        apply_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def accepted_selection(self) -> tuple[set, set]:
        app_fields = {f for f, cb in self._app_checks.items() if cb.isChecked()}
        variant_ids = {vid for vid, cb in self._variant_checks.items() if cb.isChecked()}
        return app_fields, variant_ids


class SettingsDialog(QDialog):
    """
    Settings dialog reorganized by pipeline phase.

    Tabs (7):
      1. Variant Matching   -- resolver thresholds & fuzzy clustering
      2. Archive Handling   -- deep-inspection / archive extraction
      3. Scanning & Noise   -- what gets scanned, what gets skipped
      4. Naming & Renaming  -- name cleaning, categorization, relabeling
      5. Scraper            -- metadata enrichment sources
      6. Interface          -- UI scale & display
      7. Watch Folders      -- Monitor job configuration

    Every setting key and its backend reader is UNCHANGED. This
    reorganization is layout-only: no DEFAULT_SETTINGS entry was added,
    renamed, or removed; every set_setting() call writes the same key it
    always did.
    """

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Settings")
        self.resize(920, 780)
        self.setMaximumWidth(1200)

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        settings = db.get_all_settings()

        self._build_variant_matching(settings)
        self._build_archive_handling(settings)
        self._build_scanning(settings)
        self._build_naming(settings)
        self._build_scraper(settings)
        self._build_interface(settings)
        self._build_watch_folders(settings)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        outer.addLayout(btn_row)

    # =====================================================================
    # Layout helpers
    # =====================================================================

    def _desc(self, text: str) -> QLabel:
        """Small italic description label shown under a field."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: palette(mid); font-style: italic;")
        return lbl

    def _scroll_tab(self, title: str) -> QVBoxLayout:
        """Create a scrollable tab; return its inner vertical layout."""
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(14)
        layout.setContentsMargins(12, 12, 12, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        self.tabs.addTab(scroll, title)
        return layout

    def _group(self, title: str) -> QGroupBox:
        """A titled group box with a vertical layout inside."""
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        v.setSpacing(8)
        box._inner_layout = v
        return box

    def _labeled(self, label: str, widget, description: str = None) -> QWidget:
        """Package label + widget + optional description into one widget."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        v.addWidget(QLabel(f"<b>{label}</b>"))
        v.addWidget(widget)
        if description:
            v.addWidget(self._desc(description))
        return w

    def _two_col(self, parent_layout, left_widgets, right_widgets=None):
        """Add two columns of widgets side by side inside parent_layout."""
        row = QHBoxLayout()
        row.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(8)
        for w in left_widgets:
            left.addWidget(w)
        row.addLayout(left, 1)
        if right_widgets is not None:
            right = QVBoxLayout()
            right.setSpacing(8)
            for w in right_widgets:
                right.addWidget(w)
            row.addLayout(right, 1)
        parent_layout.addLayout(row)

    # =====================================================================
    # Tab 1 -- Variant Matching
    # =====================================================================

    def _build_variant_matching(self, settings):
        layout = self._scroll_tab("Variant Matching")

        box = self._group("How variants are matched and merged into apps")
        v = box._inner_layout

        self.auto_accept = QDoubleSpinBox()
        self.auto_accept.setRange(0, 1)
        self.auto_accept.setSingleStep(0.05)
        self.auto_accept.setValue(settings.get("confidence_auto_accept", 0.85))

        self.needs_review = QDoubleSpinBox()
        self.needs_review.setRange(0, 1)
        self.needs_review.setSingleStep(0.05)
        self.needs_review.setValue(settings.get("confidence_needs_review", 0.60))

        self._two_col(
            v,
            [self._labeled(
                "Auto-accept threshold",
                self.auto_accept,
                "Variants scored at or above this value are accepted automatically.")],
            [self._labeled(
                "Needs-review threshold",
                self.needs_review,
                "Variants scored below this value are flagged for manual review.")],
        )

        self.fuzzy_threshold = QSpinBox()
        self.fuzzy_threshold.setRange(50, 100)
        self.fuzzy_threshold.setValue(settings.get("fuzzy_match_threshold", 88))

        self.prefer_pe = QCheckBox("Prefer .exe version metadata over parsed name")
        self.prefer_pe.setChecked(settings.get("prefer_pe_version_over_parsed", True))

        self._two_col(
            v,
            [self._labeled(
                "Fuzzy match threshold (0-100)",
                self.fuzzy_threshold,
                "Variants whose names are at least this similar are merged into "
                "one app. Higher = stricter merging; lower = more aggressive.")],
            [self.prefer_pe],
        )

        layout.addWidget(box)
        layout.addStretch()

    # =====================================================================
    # Tab 2 -- Archive Handling
    # =====================================================================

    def _build_archive_handling(self, settings):
        layout = self._scroll_tab("Archive Handling")

        box = self._group("Reading installer metadata and inspecting archives")
        v = box._inner_layout

        self.read_pe_metadata_toggle = QCheckBox(
            "Read .exe version metadata (ProductName, Version, etc.)")
        self.read_pe_metadata_toggle.setChecked(
            settings.get("read_exe_metadata_enabled", False))
        self.read_pe_metadata_toggle.setToolTip(
            "OFF (default): fast, name-based identification only.\n"
            "ON: opens each .exe to read its embedded version resource --\n"
            "more accurate, slower over a large collection.")

        self.inspect_archives_toggle = QCheckBox(
            "Inspect archive contents (.zip/.rar/.7z/.iso)")
        self.inspect_archives_toggle.setChecked(
            settings.get("inspect_archive_contents_enabled", False))
        self.inspect_archives_toggle.setToolTip(
            "OFF (default): archives are identified by filename only.\n"
            "ON: lists archive contents, and extracts when ambiguous, to find\n"
            "and identify the installer inside -- slower, some extraction risk.")

        v.addWidget(self.read_pe_metadata_toggle)
        v.addWidget(self.inspect_archives_toggle)
        v.addWidget(self._desc(
            "Applies to archive files (.zip/.rar/.7z/.iso). When enabled, the "
            "installer found inside is used as the naming source instead of "
            "the archive's own filename."))
        layout.addWidget(box)

        # -- ambiguity / extraction block --
        box2 = self._group("Handling unclear or large archives")
        v2 = box2._inner_layout

        self.escalate_ambiguous = QCheckBox(
            "Extract when the archive's contents are unclear")
        self.escalate_ambiguous.setChecked(
            settings.get("archive_ambiguity_escalates_to_extraction", True))

        self.max_extract_mb = QSpinBox()
        self.max_extract_mb.setRange(1, 100_000)
        self.max_extract_mb.setValue(settings.get("archive_max_full_extract_mb", 2048))
        self.max_extract_mb.setSuffix(" MB")

        self._two_col(
            v2,
            [self._labeled(
                "Extract when ambiguous",
                self.escalate_ambiguous,
                "When an archive contains multiple plausible installers, fully "
                "extract it to pick the right one. If off, the archive's own "
                "filename is used instead.")],
            [self._labeled(
                "Never extract archives larger than",
                self.max_extract_mb,
                "Safety limit. Archives larger than this are never fully "
                "extracted, even when ambiguous.")],
        )

        self.purge_after = QCheckBox("Delete extracted temp files when done")
        self.purge_after.setChecked(settings.get("archive_purge_scratch_after_use", True))

        # scratch dir: LineEdit + Browse button
        self.archive_scratch_dir_edit = QLineEdit(
            settings.get("archive_scratch_dir") or "")
        self.archive_scratch_dir_edit.setPlaceholderText(
            "(empty = use the system temp folder)")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._pick_archive_scratch_dir)
        scratch_row = QWidget()
        sr = QHBoxLayout(scratch_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(self.archive_scratch_dir_edit, 1)
        sr.addWidget(browse_btn)

        self._two_col(
            v2,
            [self.purge_after],
            [self._labeled(
                "Temporary folder for extractions",
                scratch_row,
                "Where archive contents are written while being inspected. "
                "Leave empty to use the system temp folder.")],
        )

        layout.addWidget(box2)

        # -- password protection for archives this app CREATES --
        box3 = self._group("Creating archives -- password protection")
        v3 = box3._inner_layout

        self.archive_pw_enabled = QCheckBox(
            "Password-protect archives created by Reorganize and Monitor")
        self.archive_pw_enabled.setChecked(
            bool(settings.get("archive_password_enabled", False)))

        self.archive_pw_edit = QLineEdit(settings.get("archive_password") or "")
        self.archive_pw_edit.setPlaceholderText("password")
        self.archive_pw_edit.setEnabled(self.archive_pw_enabled.isChecked())
        self.archive_pw_enabled.toggled.connect(self.archive_pw_edit.setEnabled)

        v3.addWidget(self.archive_pw_enabled)
        v3.addWidget(self._labeled(
            "Password",
            self.archive_pw_edit,
            "The password is also written into each archive's filename in "
            "brackets, e.g. Setup.exe becomes Setup(password).7z -- so it is "
            "never lost, but it is NOT secret. Avoid \\ / : * ? \" < > | ( ) "
            "since they can't appear in a filename. This is the default; "
            "the Reorganize tab and the Monitor start dialog can override it "
            "for a single run."))
        v3.addWidget(self._desc(
            "Encryption backends: 7z needs py7zr (or a 7z binary for the "
            "monitor); zip needs pyzipper or a 7z binary on PATH; RAR needs "
            "rar/WinRAR on PATH. If the password can't be applied, the file is "
            "left uncompressed -- it is never archived unprotected."))
        layout.addWidget(box3)
        layout.addStretch()

    def _pick_archive_scratch_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose scratch folder for archive extractions")
        if folder:
            self.archive_scratch_dir_edit.setText(folder)

    # =====================================================================
    # Tab 3 -- Scanning & Noise
    # =====================================================================

    def _build_scanning(self, settings):
        layout = self._scroll_tab("Scanning & Noise")

        intro = QLabel(
            "<b>Three different things can happen to a folder:</b>"
            "<ul>"
            "<li><b>Skip</b> -- the folder is excluded from the scan entirely "
            "(no app, no variant, no record). Controlled by <i>Folders to skip</i>.</li>"
            "<li><b>Component of another app</b> -- the folder is marked as an "
            "internal part of a bigger install, not a standalone app. Controlled "
            "by <i>Component folder names</i>.</li>"
            "<li><b>Scanned, but name not used</b> -- the folder still produces "
            "apps; only its name is discarded as a naming source. Controlled by "
            "<i>Folder names not used as app names</i>.</li>"
            "</ul>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # -- Skip block --
        box_skip = self._group("Folders to skip")
        v = box_skip._inner_layout

        self.noise_keywords = QPlainTextEdit(
            "\n".join(settings.get("noise_folder_keywords", [])))
        self.noise_keywords.setMaximumHeight(120)
        v.addWidget(self._labeled(
            "Folders to skip (name matches any keyword)",
            self.noise_keywords,
            "A folder whose name matches any word here is excluded from the scan "
            "entirely -- no app, no variant, no record is created from it. "
            "Applies to the folder name only, not to file names."))

        self.noise_short_only_keywords = QPlainTextEdit(
            "\n".join(settings.get("noise_short_only_keywords", [])))
        self.noise_short_only_keywords.setMaximumHeight(100)

        self.noise_short_only_max_len = QSpinBox()
        self.noise_short_only_max_len.setRange(1, 200)
        self.noise_short_only_max_len.setValue(
            int(settings.get("noise_short_only_max_len", 25)))

        self._two_col(
            v,
            [self._labeled(
                "Skip these words only when the folder name is short",
                self.noise_short_only_keywords,
                "A subset of the list above. For these words, the skip only fires "
                "when the folder name is short enough to basically be the word "
                "itself -- e.g. a folder named just 'Crack' is skipped, but a "
                "long release name that mentions 'Keygen' among many other words "
                "is still scanned as a normal folder.")],
            [self._labeled(
                "What counts as a short folder name",
                self.noise_short_only_max_len,
                "Folder names up to this many characters long count as 'short'.")],
        )
        layout.addWidget(box_skip)

        # -- Container block --
        box_cont = self._group("Component folder names (parts of another app)")
        v2 = box_cont._inner_layout
        self.container_keywords = QPlainTextEdit(
            "\n".join(settings.get("container_folder_keywords", [])))
        self.container_keywords.setMaximumHeight(120)
        v2.addWidget(self._labeled(
            "Component folder names",
            self.container_keywords,
            "Folder names that indicate an internal component of a bigger app "
            "-- e.g. 'payloads', 'redist', 'resources'. A folder matching one "
            "of these is not treated as a standalone app."))
        layout.addWidget(box_cont)

        # -- Preferred installer file name block --
        box_pref = self._group("Preferred installer file names")
        v2b = box_pref._inner_layout
        pref_intro = self._desc(
            "When a folder (or a Single App/Variant subtree) has several .exe/.msi "
            "files, this decides which one IS the installer versus a component "
            "sitting alongside it -- e.g. picking setup.exe over vcredist_x64.exe. "
            "Checked in order: exact name match, then starts-with, then "
            "contains-anywhere; the first tier that matches wins.")
        v2b.addWidget(pref_intro)

        self.preferred_installer_exact_names = QPlainTextEdit(
            "\n".join(settings.get("preferred_installer_exact_names", [])))
        self.preferred_installer_exact_names.setMaximumHeight(90)

        self.preferred_installer_prefixes = QPlainTextEdit(
            "\n".join(settings.get("preferred_installer_prefixes", [])))
        self.preferred_installer_prefixes.setMaximumHeight(70)

        self.preferred_installer_contains = QPlainTextEdit(
            "\n".join(settings.get("preferred_installer_contains", [])))
        self.preferred_installer_contains.setMaximumHeight(70)

        v2b.addWidget(self._labeled(
            "Tier 1 -- exact file name (strongest match)",
            self.preferred_installer_exact_names,
            "A file whose full name matches one of these exactly always wins, "
            "regardless of where it sits or what else is in the folder."))
        self._two_col(
            v2b,
            [self._labeled(
                "Tier 2 -- file name starts with",
                self.preferred_installer_prefixes,
                "e.g. 'setup_v2.3.exe' or 'installshield.exe'. Wins over any "
                "file that only matches tier 3 or nothing at all.")],
            [self._labeled(
                "Tier 3 -- file name contains anywhere",
                self.preferred_installer_contains,
                "Weakest signal -- still beats an unrelated component like "
                "'dotnetfx.exe' that matches none of these.")],
        )
        layout.addWidget(box_pref)

        # -- Name-not-used block --
        box_name = self._group("Folder and file names not used as app names")
        v3 = box_name._inner_layout

        self.ignore_folder_names = QPlainTextEdit(
            "\n".join(settings.get("ignore_folder_names", [])))
        self.ignore_folder_names.setMaximumHeight(100)

        self.ignore_folder_name_patterns = QPlainTextEdit(
            "\n".join(settings.get("ignore_folder_name_patterns", [])))
        self.ignore_folder_name_patterns.setMaximumHeight(80)

        self.ignore_filename_patterns = QPlainTextEdit(
            "\n".join(settings.get("ignore_filename_patterns", [])))
        self.ignore_filename_patterns.setMaximumHeight(80)

        self._two_col(
            v3,
            [self._labeled(
                "Folder names not used as app names",
                self.ignore_folder_names,
                "The folder is still scanned -- this only stops the resolver "
                "from using the folder's own name as the app name. Example: a "
                "folder called 'bin' or '32' isn't an app name; the resolver "
                "uses the file name or a parent folder's name instead.")],
            [self._labeled(
                "Folder name patterns not used as app names",
                self.ignore_folder_name_patterns,
                "Same idea as above, for patterns rather than exact names. "
                "Regex -- one pattern per line. Example: a folder named just a "
                "version number ('2.0') or a bitness label ('32-bit').")],
        )
        v3.addWidget(self._labeled(
            "File name patterns not used as app names",
            self.ignore_filename_patterns,
            "Same idea, for file names. A file matching one of these isn't used "
            "as a naming source -- e.g. a file literally named 'setup.exe' has "
            "no useful product name; the resolver falls back to the folder name. "
            "Regex -- one pattern per line."))
        layout.addWidget(box_name)

        # -- Scan behavior --
        box_behav = self._group("Scan behavior")
        v4 = box_behav._inner_layout
        self.incremental = QCheckBox("Skip folders that haven't changed since last scan")
        self.incremental.setChecked(settings.get("incremental_scan_by_default", True))
        self.follow_symlinks = QCheckBox("Follow shortcuts into other folders")
        self.follow_symlinks.setChecked(settings.get("scan_follow_symlinks", False))
        self._two_col(v4, [self.incremental], [self.follow_symlinks])
        layout.addWidget(box_behav)

        # -- Variant manifests --
        box_mf = self._group("Variant manifests (<app> <version>.appcatalog.json)")
        vm = box_mf._inner_layout
        vm.addWidget(self._desc(
            "A small file written next to each variant: app name, the setup file and its "
            "dependent files, scraped details (description, Winget / Choco ids). Rescanning "
            "a re-organized library — or a brand-new database — recognises your manual work "
            "from these. Category / subcategory are deliberately NOT stored."))
        self.manifest_auto = QCheckBox(
            "Keep manifests up to date automatically (when a variant is edited, scraped, "
            "re-organized or monitored)")
        self.manifest_auto.setChecked(settings.get("manifest_auto_enabled", True))
        self.manifest_only_valuable = QCheckBox(
            "…but only for variants with manual or scraped work "
            "(untick to also write untouched auto-resolved ones)")
        self.manifest_only_valuable.setChecked(settings.get("manifest_auto_only_valuable", True))
        vm.addWidget(self.manifest_auto)
        vm.addWidget(self.manifest_only_valuable)
        try:
            st = app_manifest.manifest_stats(self.db)
            vm.addWidget(self._desc(
                f"{st['with_manifest']} of {st['variants']} variants have a manifest · "
                f"{st['pending']} pending · {st['errors']} with a write error. "
                "Use the toolbar's “Write manifests…” (or right-click apps) to create them "
                "manually, e.g. for your existing catalog."))
        except Exception:
            pass
        layout.addWidget(box_mf)

        layout.addStretch()

    # =====================================================================
    # Tab 4 -- Naming & Renaming
    # =====================================================================

    def _build_naming(self, settings):
        layout = self._scroll_tab("Naming & Renaming")

        intro = QLabel(
            "These settings control how app names and category labels are "
            "derived. They run in the order shown: first the pipeline cleans "
            "the raw text, then rules pick the catalog and subcatalog, then "
            "aliases relabel the finished result. "
            "<b>Per-scan-root folder roles</b> (which folders are Catalog / "
            "Subcatalog / App / Skip) are set in <i>Scan roots → Edit folder "
            "layout…</i> and apply <i>on top of</i> these global rules."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # -- The naming pipeline --
        box_pipe = self._group("Name cleaning pipeline (runs top to bottom on each candidate)")
        v = box_pipe._inner_layout

        self.bracket_content_patterns = QPlainTextEdit(
            "\n".join(settings.get("bracket_content_patterns", [])))
        self.bracket_content_patterns.setMaximumHeight(70)

        self.website_patterns = QPlainTextEdit(
            "\n".join(settings.get("website_tag_patterns", [])))
        self.website_patterns.setMaximumHeight(70)

        self._two_col(
            v,
            [self._labeled(
                "1. Bracket-content patterns (regex)",
                self.bracket_content_patterns,
                "Removes text inside (), [], or {} -- the brackets and their "
                "contents both. Runs first. Applies to file names, folder "
                "names, and parent folder names. Regex -- one pattern per line.")],
            [self._labeled(
                "2. Website/domain tag patterns (regex)",
                self.website_patterns,
                "Removes website names and uploader handles stamped into names "
                "-- e.g. 'www.example.com-', 'HaxPC.net-'. Applies to file "
                "names, folder names, and parent folder names. Regex -- one "
                "pattern per line.")],
        )

        self.release_patterns = QPlainTextEdit(
            "\n".join(settings.get("release_tag_patterns", [])))
        self.release_patterns.setMaximumHeight(90)

        self.ignore_filename_words = QPlainTextEdit(
            "\n".join(settings.get("ignore_filename_words", [])))
        self.ignore_filename_words.setMaximumHeight(90)

        self._two_col(
            v,
            [self._labeled(
                "3. Release-tag / scene-flag patterns (regex)",
                self.release_patterns,
                "Keyword stripper for scene-release flags and uploader marks "
                "-- e.g. '-ViRiLiTY', 'Repack', 'Keygen.Only'. Each match is "
                "removed wherever it appears in the text. Applies to file "
                "names, folder names, and parent folder names. Regex -- one "
                "pattern per line.")],
            [self._labeled(
                "4. Ignore words in FILE names (whole word, one per line)",
                self.ignore_filename_words,
                "Whole words stripped out of a filename before it's considered "
                "as an app name -- e.g. 'setup', 'patch', 'trial'. Applied to "
                "file names only.")],
        )

        self.edition_keywords = QPlainTextEdit(
            "\n".join(settings.get("edition_keywords", [])))
        self.edition_keywords.setMaximumHeight(90)

        lang = settings.get("language_keywords", {})
        self.language_keywords = QPlainTextEdit(
            "\n".join(f"{k}={v}" for k, v in lang.items()))
        self.language_keywords.setMaximumHeight(90)

        self._two_col(
            v,
            [self._labeled(
                "7. Edition keywords (whole word, one per line)",
                self.edition_keywords,
                "Edition words like 'Pro', 'Ultimate', 'Home'. Pulled out of "
                "the name into a separate edition field, so 'Able2Extract' and "
                "'Able2Extract Professional' cluster as one app with two "
                "editions instead of two separate apps. Whole-word match only "
                "-- 'Lite' won't match inside 'K-Lite'.")],
            [self._labeled(
                "8. Language keywords (word=Label, one per line)",
                self.language_keywords,
                "Language tags like 'English', 'Multilanguage'. Extracted into "
                "a separate language field. Whole-word match only.")],
        )

        # architecture_keywords: dict of arch -> list of words, serialized
        # as "arch: word1, word2, ..." per line
        arch = settings.get("architecture_keywords", {})
        arch_text = "\n".join(
            f"{k}: {', '.join(v)}" for k, v in arch.items())
        self.architecture_keywords = QPlainTextEdit(arch_text)
        self.architecture_keywords.setMaximumHeight(80)

        self.build_number_pattern = QLineEdit(
            settings.get("build_number_pattern", ""))

        self._two_col(
            v,
            [self._labeled(
                "Architecture keywords (arch: word1, word2, ...)",
                self.architecture_keywords,
                "Words that mark a build's architecture -- 'x64', '32bit', "
                "'arm64', etc. Extracted into a separate architecture field.")],
            [self._labeled(
                "9. Build-number pattern (regex, one capture group)",
                self.build_number_pattern,
                "Recognizes build numbers appended to a version -- e.g. "
                "'Build 212' in 'ACDSee 6.2 Build 212'. The matched text moves "
                "from the name into the version. Regex -- must contain exactly "
                "one capture group.")],
        )

        self.bare_number_check = QCheckBox(
            "Treat a trailing bare number as the version")
        self.bare_number_check.setChecked(
            settings.get("allow_bare_trailing_number_as_version", True))

        # App synonyms and portable words side by side
        synonyms = settings.get("app_name_synonyms", [])
        syn_text = "\n".join(
            f"{item['pattern']}={item['replacement']}"
            for item in synonyms if "pattern" in item)
        self.synonyms_edit = QPlainTextEdit(syn_text)
        self.synonyms_edit.setMaximumHeight(80)

        self.portable_words_edit = QPlainTextEdit(
            "\n".join(settings.get("portable_indicator_words", [])))
        self.portable_words_edit.setMaximumHeight(80)

        self._two_col(
            v,
            [self._labeled(
                "App name synonyms (pattern=replacement)",
                self.synonyms_edit,
                "Explicit corrections for known misspellings or rebrands -- "
                "e.g. '^(demon|deamon)\\s+tools=DAEMON Tools'. Checked after "
                "all other cleaning, before clustering. Regex -- one "
                "pattern=replacement per line.")],
            [self._labeled(
                "Portable indicator words (one per line)",
                self.portable_words_edit,
                "Words that mark a build as portable -- e.g. 'portable', "
                "'paf'. A portable build gets a '(Portable)' suffix and a "
                "'Portable' tag.")],
        )

        v.addWidget(self.bare_number_check)
        v.addWidget(self._desc(
            "If the cleaned name ends in a bare number with no other version "
            "context -- e.g. 'Able2Extract Professional 10' -- treat that "
            "number as the version. Only applies to the LAST number in the name."))

        layout.addWidget(box_pipe)

        # -- Categorization & relabeling --
        box_cat = self._group("Categorization and relabeling")
        v2 = box_cat._inner_layout

        cat_rules = settings.get("category_rules", [])
        cat_text = "\n".join(
            f"{r['pattern']}={r['value']}"
            for r in cat_rules if "pattern" in r)
        self.category_rules_edit = QPlainTextEdit(cat_text)
        self.category_rules_edit.setMaximumHeight(80)

        subcat_rules = settings.get("subcategory_rules", [])
        subcat_text = "\n".join(
            f"{r['pattern']}={r['value']}"
            for r in subcat_rules if "pattern" in r)
        self.subcategory_rules_edit = QPlainTextEdit(subcat_text)
        self.subcategory_rules_edit.setMaximumHeight(80)

        self._two_col(
            v2,
            [self._labeled(
                "Force a catalog based on folder path (pattern=value)",
                self.category_rules_edit,
                "Regex matched against the folder's full relative path. First "
                "matching rule wins. Runs at scan time, before the default "
                "first-segment-is-catalog fallback. Applies to the raw folder "
                "path.")],
            [self._labeled(
                "Force a subcategory based on folder path (pattern=value)",
                self.subcategory_rules_edit,
                "Same as above, for subcatalog. Checked after category rules.")],
        )

        aliases = settings.get("folder_name_aliases", {})
        self.folder_aliases = QPlainTextEdit(
            "\n".join(f"{k}={v}" for k, v in aliases.items()))
        self.folder_aliases.setMaximumHeight(80)
        v2.addWidget(self._labeled(
            "Rename catalogs and subcatalogs (alias=display name)",
            self.folder_aliases,
            "Relabels the finished catalog or subcatalog string without "
            "touching anything on disk. Exact case-insensitive match -- not "
            "regex. Runs at resolve time, after the catalog/subcatalog string "
            "has already been decided. Example: 'burners=CD/DVD Burner'."))

        self.fallback_catalog_name = QLineEdit(
            settings.get("fallback_catalog_name", "MISC"))
        self.fallback_subcatalog_name = QLineEdit(
            settings.get("fallback_subcatalog_name", "MISC"))

        self._two_col(
            v2,
            [self._labeled(
                "Catalog name when nothing else specifies one",
                self.fallback_catalog_name,
                "Used when a folder structure doesn't yield a clear catalog "
                "-- e.g. a loose installer with no category folder around it.")],
            [self._labeled(
                "Subcatalog name when nothing else specifies one",
                self.fallback_subcatalog_name,
                "Same as above, for subcatalog.")],
        )

        layout.addWidget(box_cat)
        layout.addStretch()

    # =====================================================================
    # Tab 5 -- Scraper
    # =====================================================================

    def _build_scraper(self, settings):
        layout = self._scroll_tab("Scraper")

        # -- Sources --
        box_src = self._group("Metadata sources")
        v = box_src._inner_layout

        self.scraper_manifest_url = QLineEdit(
            settings.get("scraper_winget_manifest_url", ""))
        v.addWidget(self._labeled(
            "Winget manifest URL",
            self.scraper_manifest_url,
            "Source of the large offline Winget app list used for background "
            "enrichment. Re-downloaded when the local cache is older than the "
            "'refresh after N hours' setting below."))

        self.winutil_url_edit = QLineEdit(
            settings.get("scraper_winutil_apps_url", ""))
        v.addWidget(self._labeled(
            "Winutil apps URL",
            self.winutil_url_edit,
            "Optional secondary source with richer per-app metadata "
            "(description, category, homepage) than the Winget manifest."))

        layout.addWidget(box_src)

        # -- Refresh / search limits --
        box_lim = self._group("Refresh and search limits")
        v2 = box_lim._inner_layout

        self.scraper_staleness_hours = QSpinBox()
        self.scraper_staleness_hours.setRange(1, 24 * 30)
        self.scraper_staleness_hours.setValue(
            int(settings.get("scraper_manifest_staleness_hours", 4)))
        self.scraper_staleness_hours.setSuffix(" hours")

        self.scraper_choco_max_results = QSpinBox()
        self.scraper_choco_max_results.setRange(1, 20)
        self.scraper_choco_max_results.setValue(
            int(settings.get("scraper_choco_default_max_results", 5)))

        self._two_col(
            v2,
            [self._labeled(
                "Refresh the manifest after",
                self.scraper_staleness_hours,
                "The manifest is re-downloaded the next time a scrape runs "
                "once it's older than this.")],
            [self._labeled(
                "Results per Chocolatey search",
                self.scraper_choco_max_results,
                "How many candidates the manual Chocolatey search returns.")],
        )
        layout.addWidget(box_lim)

        # -- Behaviour --
        box_beh = self._group("Scrape behavior")
        v3 = box_beh._inner_layout

        self.scraper_auto_rename = QCheckBox(
            "Rename apps from the manifest (may override resolver names)")
        self.scraper_auto_rename.setChecked(
            bool(settings.get("scraper_auto_rename", False)))
        v3.addWidget(self.scraper_auto_rename)
        v3.addWidget(self._desc(
            "Off by default -- a manifest match still fills in publisher/"
            "description/tags/version, it just won't overwrite an "
            "already-resolved app name. Locked names (name_locked) are never "
            "renamed either way."))

        self.scraper_auto_enrich_statuses = QPlainTextEdit(
            "\n".join(settings.get("scraper_auto_enrich_statuses",
                                    ["resolved", "verified"])))
        self.scraper_auto_enrich_statuses.setMaximumHeight(60)

        self.scraper_winget_show_for_auto_scrape = QCheckBox(
            "Fetch full details via winget show (slow)")
        self.scraper_winget_show_for_auto_scrape.setChecked(
            bool(settings.get("scraper_winget_show_for_auto_scrape", False)))

        self.scraper_winget_show_timeout = QSpinBox()
        self.scraper_winget_show_timeout.setRange(1, 60)
        self.scraper_winget_show_timeout.setValue(
            int(settings.get("scraper_winget_show_timeout_seconds", 10)))
        self.scraper_winget_show_timeout.setSuffix(" seconds")

        self._two_col(
            v3,
            [self._labeled(
                "Which apps 'Scrape all' touches (one status per line)",
                self.scraper_auto_enrich_statuses,
                "Only these app statuses are attempted during a batch scrape. "
                "Manual search/match on a single app isn't restricted by this.")],
            [self._labeled(
                "winget show details",
                self.scraper_winget_show_for_auto_scrape,
                "Adds a per-app `winget show` call for extra details. Off by "
                "default -- a few hundred ms per app adds up over a large "
                "batch. The manual 'Search & match…' dialog always uses it for "
                "the single app you pick, regardless of this setting.")],
        )

        self._two_col(
            v3,
            [self._labeled(
                "winget show timeout",
                self.scraper_winget_show_timeout,
                "How long to wait for one `winget show` call before giving up.")],
            [],
        )

        layout.addWidget(box_beh)
        layout.addStretch()

    # =====================================================================
    # Tab 6 -- Interface
    # =====================================================================

    def _build_interface(self, settings):
        layout = self._scroll_tab("Interface")

        box = self._group("Display")
        v = box._inner_layout

        self.ui_scale = QDoubleSpinBox()
        self.ui_scale.setRange(0.5, 3.0)
        self.ui_scale.setSingleStep(0.1)
        self.ui_scale.setValue(settings.get("ui_scale_multiplier", 1.0))
        v.addWidget(self._labeled(
            "Interface scale multiplier",
            self.ui_scale,
            "Scales all fonts and widget sizes. Takes effect after restarting "
            "the app (Qt reads this before the window is created)."))

        layout.addWidget(box)

        # Read-only info
        info = self._group("Catalog info")
        vi = info._inner_layout
        conn = self.db.connect()
        app_count = conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"]
        variant_count = conn.execute("SELECT COUNT(*) AS n FROM variants").fetchone()["n"]
        vi.addWidget(QLabel(f"<b>Database:</b> {self.db.path}"))
        vi.addWidget(QLabel(f"<b>Apps:</b> {app_count}"))
        vi.addWidget(QLabel(f"<b>Variants:</b> {variant_count}"))
        layout.addWidget(info)

        layout.addStretch()

    # =====================================================================
    # Tab 7 -- Watch Folders (Monitor)
    # =====================================================================

    def _build_watch_folders(self, settings):
        layout = self._scroll_tab("Watch Folders")

        intro = QLabel(
            "<b>Monitor job</b> — a manually-triggered pass over the folders "
            "below. New .exe / .msi / archive files are identified, matched to "
            "an existing app (or flagged for creation of a new one), and moved "
            "into the organized structure. Already-compressed inputs are moved "
            "as-is; everything else is archived first. Nothing on disk changes "
            "until you review the dry-run plan and click <b>Execute</b>."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        box_folders = self._group("Folders to watch")
        v = box_folders._inner_layout

        self.monitor_folders_table = QTableWidget(0, 1)
        self.monitor_folders_table.setHorizontalHeaderLabels(["Folder"])
        self.monitor_folders_table.horizontalHeader().setStretchLastSection(True)
        self.monitor_folders_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.monitor_folders_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.monitor_folders_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.monitor_folders_table.setMinimumHeight(120)
        for f in settings.get("monitor_folders", []):
            self._append_monitor_folder_row(f)
        v.addWidget(self.monitor_folders_table)

        row = QHBoxLayout()
        add_btn = QPushButton("Add folder…")
        add_btn.clicked.connect(self._add_monitor_folder)
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self._remove_monitor_folders)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        row.addStretch()
        v.addLayout(row)
        layout.addWidget(box_folders)

        # -- Extensions & filters --
        box_ext = self._group("File filters")
        v2 = box_ext._inner_layout

        self.monitor_extensions = QPlainTextEdit(
            " ".join(settings.get("monitor_extensions", [])))
        self.monitor_extensions.setMaximumHeight(40)

        self.monitor_already_compressed = QPlainTextEdit(
            " ".join(settings.get("monitor_already_compressed_extensions", [])))
        self.monitor_already_compressed.setMaximumHeight(40)

        self._two_col(
            v2,
            [self._labeled(
                "Extensions to watch",
                self.monitor_extensions,
                "Space-separated, leading dot required.")],
            [self._labeled(
                "Already-compressed extensions",
                self.monitor_already_compressed,
                "Files with these extensions are moved as-is, not re-compressed.")],
        )

        self.monitor_skip_partial = QPlainTextEdit(
            " ".join(settings.get("monitor_skip_partial_extensions", [])))
        self.monitor_skip_partial.setMaximumHeight(40)

        self.monitor_min_size = QSpinBox()
        self.monitor_min_size.setRange(0, 100_000)
        self.monitor_min_size.setValue(int(settings.get("monitor_min_size_mb", 1)))
        self.monitor_min_size.setSuffix(" MB")

        self._two_col(
            v2,
            [self._labeled(
                "Skip partial-download suffixes",
                self.monitor_skip_partial,
                "Files ending in these are ignored entirely (mid-download markers).")],
            [self._labeled(
                "Minimum file size",
                self.monitor_min_size,
                "Smaller files are ignored.")],
        )
        layout.addWidget(box_ext)

        # -- Settle / archive / mode --
        box_act = self._group("Settling and archive options")
        v3 = box_act._inner_layout

        self.monitor_settle = QSpinBox()
        self.monitor_settle.setRange(0, 300)
        self.monitor_settle.setValue(int(settings.get("monitor_settle_seconds", 3)))
        self.monitor_settle.setSuffix(" seconds")

        self.monitor_format = QComboBox()
        self.monitor_format.addItems(["7z", "zip", "rar"])
        fmt = settings.get("monitor_archive_format", "7z")
        idx = self.monitor_format.findText(fmt)
        if idx >= 0:
            self.monitor_format.setCurrentIndex(idx)

        self._two_col(
            v3,
            [self._labeled(
                "Settle delay",
                self.monitor_settle,
                "A file must be unchanged for this long before it's processed.")],
            [self._labeled(
                "Default archive format",
                self.monitor_format,
                "Format used when compressing non-archive installers.")],
        )

        self.monitor_move_mode = QComboBox()
        self.monitor_move_mode.addItems(["move", "copy"])
        self.monitor_move_mode.setCurrentText(
            settings.get("monitor_move_mode", "move"))

        self.monitor_auto_scrape = QCheckBox(
            "Refresh Winget metadata for an app right after a file is attached")
        self.monitor_auto_scrape.setChecked(
            bool(settings.get("monitor_auto_scrape_on_attach", True)))

        self._two_col(
            v3,
            [self._labeled(
                "Default mode",
                self.monitor_move_mode,
                "Move = the original file is removed after being placed. "
                "Copy = the original stays where it was.")],
            [self.monitor_auto_scrape],
        )
        layout.addWidget(box_act)
        layout.addStretch()

    # ---------------------------------------------------------------------
    # Watch Folders -- table helpers (unchanged from previous implementation)
    # ---------------------------------------------------------------------

    def _append_monitor_folder_row(self, folder: str):
        row = self.monitor_folders_table.rowCount()
        self.monitor_folders_table.insertRow(row)
        self.monitor_folders_table.setItem(row, 0, QTableWidgetItem(folder))

    def _current_monitor_folders(self) -> list:
        out = []
        for r in range(self.monitor_folders_table.rowCount()):
            item = self.monitor_folders_table.item(r, 0)
            if item and item.text().strip():
                out.append(item.text().strip())
        return out

    def _add_monitor_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to monitor")
        if not folder:
            return
        if folder in self._current_monitor_folders():
            return
        self._append_monitor_folder_row(folder)

    def _remove_monitor_folders(self):
        rows = sorted(
            {idx.row() for idx in self.monitor_folders_table.selectedIndexes()},
            reverse=True,
        )
        for r in rows:
            self.monitor_folders_table.removeRow(r)

    # =====================================================================
    # Save
    # =====================================================================

    def _save(self):
        # -- Archive password: validate first so a bad one never gets saved --
        if self.archive_pw_enabled.isChecked():
            pw_err = validate_archive_password(self.archive_pw_edit.text())
            if pw_err:
                QMessageBox.warning(self, "Archive password", pw_err)
                return
        # -- Variant Matching --
        self.db.set_setting("confidence_auto_accept", self.auto_accept.value(),
                            bump_version=True, note="settings dialog save")
        self.db.set_setting("confidence_needs_review", self.needs_review.value(),
                            bump_version=False)
        self.db.set_setting("fuzzy_match_threshold", self.fuzzy_threshold.value(),
                            bump_version=False)
        self.db.set_setting("prefer_pe_version_over_parsed",
                            self.prefer_pe.isChecked(), bump_version=False)

        # -- Archive Handling --
        self.db.set_setting("read_exe_metadata_enabled",
                            self.read_pe_metadata_toggle.isChecked(),
                            bump_version=False)
        self.db.set_setting("inspect_archive_contents_enabled",
                            self.inspect_archives_toggle.isChecked(),
                            bump_version=False)
        self.db.set_setting("archive_ambiguity_escalates_to_extraction",
                            self.escalate_ambiguous.isChecked(),
                            bump_version=False)
        self.db.set_setting("archive_max_full_extract_mb",
                            self.max_extract_mb.value(), bump_version=False)
        self.db.set_setting("archive_purge_scratch_after_use",
                            self.purge_after.isChecked(), bump_version=False)
        scratch = self.archive_scratch_dir_edit.text().strip() or None
        self.db.set_setting("archive_scratch_dir", scratch, bump_version=False)

        # -- Scanning & Noise --
        self.db.set_setting("noise_folder_keywords",
                            [l.strip() for l in
                             self.noise_keywords.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("noise_short_only_keywords",
                            [l.strip() for l in
                             self.noise_short_only_keywords.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("noise_short_only_max_len",
                            self.noise_short_only_max_len.value(),
                            bump_version=False)
        self.db.set_setting("container_folder_keywords",
                            [l.strip() for l in
                             self.container_keywords.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("preferred_installer_exact_names",
                            [l.strip() for l in
                             self.preferred_installer_exact_names.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("preferred_installer_prefixes",
                            [l.strip() for l in
                             self.preferred_installer_prefixes.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("preferred_installer_contains",
                            [l.strip() for l in
                             self.preferred_installer_contains.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("ignore_folder_names",
                            [l.strip() for l in
                             self.ignore_folder_names.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("ignore_folder_name_patterns",
                            [l.strip() for l in
                             self.ignore_folder_name_patterns.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("ignore_filename_patterns",
                            [l.strip() for l in
                             self.ignore_filename_patterns.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("incremental_scan_by_default",
                            self.incremental.isChecked(), bump_version=False)
        self.db.set_setting("scan_follow_symlinks",
                            self.follow_symlinks.isChecked(), bump_version=False)
        self.db.set_setting("manifest_auto_enabled",
                            self.manifest_auto.isChecked(), bump_version=False)
        self.db.set_setting("manifest_auto_only_valuable",
                            self.manifest_only_valuable.isChecked(), bump_version=False)

        # -- Naming & Renaming --
        self.db.set_setting("bracket_content_patterns",
                            [l.strip() for l in
                             self.bracket_content_patterns.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("website_tag_patterns",
                            [l.strip() for l in
                             self.website_patterns.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("release_tag_patterns",
                            [l.strip() for l in
                             self.release_patterns.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("ignore_filename_words",
                            [l.strip() for l in
                             self.ignore_filename_words.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("edition_keywords",
                            [l.strip() for l in
                             self.edition_keywords.toPlainText().splitlines()
                             if l.strip()], bump_version=False)

        lang_dict = {}
        for line in self.language_keywords.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    lang_dict[k.strip().lower()] = v.strip()
        self.db.set_setting("language_keywords", lang_dict, bump_version=False)

        arch_dict = {}
        for line in self.architecture_keywords.toPlainText().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                words = [w.strip() for w in v.split(",") if w.strip()]
                if k.strip():
                    arch_dict[k.strip()] = words
        self.db.set_setting("architecture_keywords", arch_dict, bump_version=False)

        self.db.set_setting("build_number_pattern",
                            self.build_number_pattern.text().strip(),
                            bump_version=False)
        self.db.set_setting("allow_bare_trailing_number_as_version",
                            self.bare_number_check.isChecked(),
                            bump_version=False)

        synonyms = []
        for line in self.synonyms_edit.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    synonyms.append({"pattern": k.strip(), "replacement": v.strip()})
        self.db.set_setting("app_name_synonyms", synonyms, bump_version=False)

        self.db.set_setting("portable_indicator_words",
                            [l.strip() for l in
                             self.portable_words_edit.toPlainText().splitlines()
                             if l.strip()], bump_version=False)

        cat_rules = []
        for line in self.category_rules_edit.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    cat_rules.append({"pattern": k.strip(), "value": v.strip()})
        self.db.set_setting("category_rules", cat_rules, bump_version=False)

        subcat_rules = []
        for line in self.subcategory_rules_edit.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    subcat_rules.append({"pattern": k.strip(), "value": v.strip()})
        self.db.set_setting("subcategory_rules", subcat_rules, bump_version=False)

        alias_dict = {}
        for line in self.folder_aliases.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    alias_dict[k.strip().lower()] = v.strip()
        self.db.set_setting("folder_name_aliases", alias_dict, bump_version=False)

        self.db.set_setting("fallback_catalog_name",
                            self.fallback_catalog_name.text().strip(),
                            bump_version=False)
        self.db.set_setting("fallback_subcatalog_name",
                            self.fallback_subcatalog_name.text().strip(),
                            bump_version=False)

        # -- Scraper --
        self.db.set_setting("scraper_winget_manifest_url",
                            self.scraper_manifest_url.text().strip(),
                            bump_version=False)
        self.db.set_setting("scraper_winutil_apps_url",
                            self.winutil_url_edit.text().strip(),
                            bump_version=False)
        self.db.set_setting("scraper_manifest_staleness_hours",
                            self.scraper_staleness_hours.value(),
                            bump_version=False)
        self.db.set_setting("scraper_auto_rename",
                            self.scraper_auto_rename.isChecked(),
                            bump_version=False)
        self.db.set_setting("scraper_auto_enrich_statuses",
                            [l.strip() for l in
                             self.scraper_auto_enrich_statuses.toPlainText().splitlines()
                             if l.strip()], bump_version=False)
        self.db.set_setting("scraper_choco_default_max_results",
                            self.scraper_choco_max_results.value(),
                            bump_version=False)
        self.db.set_setting("scraper_winget_show_for_auto_scrape",
                            self.scraper_winget_show_for_auto_scrape.isChecked(),
                            bump_version=False)
        self.db.set_setting("scraper_winget_show_timeout_seconds",
                            self.scraper_winget_show_timeout.value(),
                            bump_version=False)

        # -- Interface --
        self.db.set_setting("ui_scale_multiplier", self.ui_scale.value(),
                            bump_version=False)

        # -- Watch Folders --
        self.db.set_setting("monitor_folders",
                            self._current_monitor_folders(), bump_version=False)
        self.db.set_setting("monitor_extensions",
                            self.monitor_extensions.toPlainText().split(),
                            bump_version=False)
        self.db.set_setting("monitor_already_compressed_extensions",
                            self.monitor_already_compressed.toPlainText().split(),
                            bump_version=False)
        self.db.set_setting("monitor_skip_partial_extensions",
                            self.monitor_skip_partial.toPlainText().split(),
                            bump_version=False)
        self.db.set_setting("monitor_min_size_mb",
                            self.monitor_min_size.value(), bump_version=False)
        self.db.set_setting("monitor_settle_seconds",
                            self.monitor_settle.value(), bump_version=False)
        self.db.set_setting("monitor_archive_format",
                            self.monitor_format.currentText(), bump_version=False)
        self.db.set_setting("monitor_move_mode",
                            self.monitor_move_mode.currentText(), bump_version=False)
        self.db.set_setting("monitor_auto_scrape_on_attach",
                            self.monitor_auto_scrape.isChecked(), bump_version=False)

        # -- Archive password protection --
        self.db.set_setting("archive_password_enabled",
                            self.archive_pw_enabled.isChecked(), bump_version=False)
        self.db.set_setting("archive_password", self.archive_pw_edit.text(),
                            bump_version=False)

        self.accept()

class CsvExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export to CSV")
        self.resize(380, 160)
        self._output_dir = None
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Exports one row per app (name, catalog, subcatalog, versions,\n"
            "confidence, alt-name candidate) for review or bulk correction."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.batch_toggle = QCheckBox("Split into small batches (for easy chat upload)")
        self.batch_toggle.setChecked(True)
        self.batch_toggle.toggled.connect(self._toggle_batch_size)
        form.addRow(self.batch_toggle)
        self.batch_size = QSpinBox()
        self.batch_size.setRange(10, 5000)
        self.batch_size.setValue(200)
        self.batch_size.setSuffix(" apps per file")
        form.addRow("Batch size", self.batch_size)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        choose_btn = QPushButton("Choose folder && export…")
        choose_btn.clicked.connect(self._choose_and_accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(choose_btn)
        layout.addLayout(btn_row)

    def _toggle_batch_size(self, checked: bool):
        self.batch_size.setEnabled(checked)

    def _choose_and_accept(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose export folder")
        if folder:
            self._output_dir = folder
            self.accept()

    def output_dir(self) -> str | None:
        return self._output_dir

    def batch_size_value(self) -> int:
        return self.batch_size.value() if self.batch_toggle.isChecked() else 0

class RefineNamesDialog(QDialog):
    """
    One-time name refinement for a selection of apps. The user points at a
    token ("Ultimate", "Lite", "Repack") and says what it is. By default
    the rules apply ONLY to this run, only to the selected apps -- the
    settings DB is never written to, so the change can't leak into other
    apps or into future scans/re-resolves. Ticking "Also save permanently"
    writes the rules into the corresponding settings lists.

    Release-tag rules are always one-time (they need a regex under the
    hood, and there's no user-facing regex helper yet) -- the permanent
    checkbox is disabled when any such rule is present.
    """

    KIND_OPTIONS = [
        ("edition",     "Edition — moves the token to the Edition field"),
        ("language",    "Language — moves the token to the Language field"),
        ("arch_x64",    "Architecture: x64"),
        ("arch_x86",    "Architecture: x86"),
        ("arch_arm64",  "Architecture: arm64"),
        ("release",     "Release tag — removes the token entirely (one-time only)"),
        ("ignore_word", "Ignore word — removes the token from file names"),
    ]

    def __init__(self, db: Database, app_ids: list, parent=None):
        super().__init__(parent)
        self.db = db
        self.app_ids = list(app_ids)
        self.applied_count = 0
        self.saved_permanently = False

        self.setWindowTitle(f"Refine names — {len(self.app_ids)} app(s)")
        self.resize(780, 640)
        outer = QVBoxLayout(self)

        intro = QLabel(
            "Add a rule per token you want the resolver to treat differently. "
            "By default each rule applies <b>only to this run</b>, only to the "
            "selected apps — nothing is saved and future scans/re-resolves are "
            "unaffected. Tick the box at the bottom to also save the rules "
            "permanently."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        outer.addWidget(QLabel("<b>Rules</b>"))
        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["Token", "Treat as", ""])
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        outer.addWidget(self.rules_table)

        add_row = QHBoxLayout()
        add_btn = QPushButton("+ Add rule")
        add_btn.clicked.connect(self._add_rule_row)
        add_row.addWidget(add_btn)
        add_row.addStretch()
        outer.addLayout(add_row)

        outer.addWidget(QLabel("<b>Preview</b> — apps whose name will change"))
        self.preview_table = QTableWidget(0, 3)
        self.preview_table.setHorizontalHeaderLabels(
            ["Current name", "New name", "Rule effect"]
        )
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        outer.addWidget(self.preview_table)

        self.preview_note = QLabel("")
        self.preview_note.setWordWrap(True)
        outer.addWidget(self.preview_note)

        self.save_permanent_check = QCheckBox(
            "Also save these rules permanently "
            "(future scans/re-resolves will apply them everywhere)"
        )
        outer.addWidget(self.save_permanent_check)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self._apply)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.apply_btn)
        outer.addLayout(btn_row)

        # Debounced live preview -- rules change faster than we want to
        # run re-resolve proposals, so wait 250ms after the last edit.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._refresh_preview)

        self._add_rule_row()   # start with one empty row
        self._refresh_preview()

    # -- rules table ---------------------------------------------------

    def _add_rule_row(self):
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)

        token_edit = QLineEdit()
        token_edit.setPlaceholderText("e.g. Lite")
        token_edit.textChanged.connect(self._on_rules_changed)
        self.rules_table.setCellWidget(row, 0, token_edit)

        kind_combo = QComboBox()
        for value, label in self.KIND_OPTIONS:
            kind_combo.addItem(label, value)
        kind_combo.currentIndexChanged.connect(self._on_rules_changed)
        self.rules_table.setCellWidget(row, 1, kind_combo)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(30)
        remove_btn.clicked.connect(
            lambda _checked=False, b=remove_btn: self._remove_rule_row(b)
        )
        self.rules_table.setCellWidget(row, 2, remove_btn)

    def _remove_rule_row(self, button):
        for row in range(self.rules_table.rowCount()):
            if self.rules_table.cellWidget(row, 2) is button:
                self.rules_table.removeRow(row)
                break
        self._on_rules_changed()

    def _collect_rules(self) -> list:
        rules = []
        for row in range(self.rules_table.rowCount()):
            token_edit = self.rules_table.cellWidget(row, 0)
            kind_combo = self.rules_table.cellWidget(row, 1)
            if token_edit is None or kind_combo is None:
                continue
            token = token_edit.text().strip()
            if token:
                rules.append((token, kind_combo.currentData()))
        return rules

    def _on_rules_changed(self):
        self._update_permanent_availability()
        self._preview_timer.start()

    def _update_permanent_availability(self):
        has_release = any(kind == "release" for _, kind in self._collect_rules())
        self.save_permanent_check.setEnabled(not has_release)
        if has_release:
            self.save_permanent_check.setChecked(False)
            self.save_permanent_check.setToolTip(
                "Release-tag rules are regex-based. Saving them permanently "
                "needs a regex helper (coming later) — this run still applies "
                "them one-time."
            )
        else:
            self.save_permanent_check.setToolTip("")

    # -- override / preview --------------------------------------------

    def _build_override(self, rules: list, settings: dict) -> dict:
        """Build a settings dict that supersedes the DB's settings for one run.
        Multiple rules of the same kind accumulate (list entries are appended,
        dict entries merged) rather than the later rule overwriting the earlier."""
        override = {}
        for token, kind in rules:
            if kind == "edition":
                key = "edition_keywords"
                current = override.get(key)
                if current is None:
                    current = list(settings.get(key, []))
                if token.lower() not in {w.lower() for w in current}:
                    current.append(token)
                override[key] = current

            elif kind == "language":
                key = "language_keywords"
                current = override.get(key)
                if current is None:
                    current = dict(settings.get(key, {}))
                current[token.lower()] = token.title()
                override[key] = current

            elif kind in ("arch_x64", "arch_x86", "arch_arm64"):
                key = "architecture_keywords"
                arch = kind.split("_", 1)[1]
                current = override.get(key)
                if current is None:
                    current = {k: list(v) for k, v in settings.get(key, {}).items()}
                current.setdefault(arch, [])
                if token not in current[arch]:
                    current[arch].append(token)
                override[key] = current

            elif kind == "release":
                key = "release_tag_patterns"
                current = override.get(key)
                if current is None:
                    current = list(settings.get(key, []))
                current.append(r"\b" + re.escape(token) + r"\b")
                override[key] = current

            elif kind == "ignore_word":
                key = "ignore_filename_words"
                current = override.get(key)
                if current is None:
                    current = list(settings.get(key, []))
                if token.lower() not in {w.lower() for w in current}:
                    current.append(token)
                override[key] = current

        return override
    
    def _refresh_preview(self):
        rules = self._collect_rules()
        self.preview_table.setRowCount(0)

        settings = self.db.get_all_settings()
        override = self._build_override(rules, settings)
        conn = self.db.connect()

        n_changed = 0
        for app_id in self.app_ids:
            row = conn.execute(
                "SELECT name FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if row is None:
                continue
            current_name = row["name"] or ""
            try:
                proposal = propose_reresolve_app(
                    self.db, app_id, settings_override=override
                )
                proposed_name = proposal.get("proposed_name") or current_name
            except Exception as exc:
                print(f"[refine-names] proposal failed for app {app_id}: {exc!r}")
                proposed_name = current_name

            effects = self._describe_effects(rules, current_name, proposed_name)

            i = self.preview_table.rowCount()
            self.preview_table.insertRow(i)
            self.preview_table.setItem(i, 0, QTableWidgetItem(current_name))
            self.preview_table.setItem(i, 1, QTableWidgetItem(proposed_name))
            self.preview_table.setItem(i, 2, QTableWidgetItem(effects))
            if proposed_name != current_name:
                n_changed += 1

        n_total = len(self.app_ids)
        if not rules:
            self.preview_note.setText(
                f"Add at least one rule above to see what changes. "
                f"Previewing {n_total} selected app(s)."
            )
            self.apply_btn.setEnabled(False)
        elif n_changed == 0:
            self.preview_note.setText(
                f"None of the {n_total} selected app(s) would change with these rules."
            )
            self.apply_btn.setEnabled(False)
        else:
            self.preview_note.setText(
                f"{n_changed} of {n_total} selected app(s) would change name."
            )
            self.apply_btn.setEnabled(True)

    def _describe_effects(self, rules, current_name, proposed_name) -> str:
        """
        For each rule, check whether its token was actually consumed by the
        resolver for THIS app -- i.e. it appeared in the current name and no
        longer appears in the proposed name. If so, describe where it went:
          - Edition / Language / Architecture -> moved into that metadata field
          - Release tag / Ignore word         -> stripped entirely, no field
        Uses a whole-word boundary match, mirroring how the resolver itself
        matches edition/language tokens (so 'en' matches the standalone "EN"
        in "Smart Pack1 5 EN" but not the "en" inside "Extended").
        """
        if not rules:
            return ""
        curr = current_name.lower()
        prop = proposed_name.lower()
        parts = []
        for token, kind in rules:
            pattern = r"\b" + re.escape(token.lower()) + r"\b"
            if re.search(pattern, curr) and not re.search(pattern, prop):
                label = {
                    "edition":     "moved to Edition",
                    "language":    "moved to Language",
                    "arch_x64":    "moved to Architecture (x64)",
                    "arch_x86":    "moved to Architecture (x86)",
                    "arch_arm64":  "moved to Architecture (arm64)",
                    "release":     "stripped (release tag)",
                    "ignore_word": "stripped (filename word)",
                }.get(kind, f"applied as {kind}")
                parts.append(f"'{token}' \u2192 {label}")
        return "; ".join(parts)


    # -- apply ---------------------------------------------------------

    def _apply(self):
        rules = self._collect_rules()
        if not rules:
            return
        settings = self.db.get_all_settings()
        override = self._build_override(rules, settings)

        if self.save_permanent_check.isChecked():
            self._save_rules_permanently(rules, settings)
            self.saved_permanently = True

        applied = 0
        for app_id in self.app_ids:
            try:
                proposal = propose_reresolve_app(
                    self.db, app_id, settings_override=override
                )
                accepted_app_fields = {
                    f for f in ("name", "catalog", "subcatalog")
                    if not proposal[f"{f}_locked"]
                }
                accepted_variant_ids = {
                    vp["variant_id"] for vp in proposal["variants"]
                    if not vp["version_locked"]
                }
                apply_reresolve_app(
                    self.db, proposal, accepted_app_fields, accepted_variant_ids
                )
                applied += 1
            except Exception:
                pass
        self.applied_count = applied
        self.accept()

    def _save_rules_permanently(self, rules, settings):
        # Work on a mutable copy that accumulates across rules, so two
        # "edition" rules don't clobber each other on the second write.
        working = {
            k: (list(v) if isinstance(v, list)
                else dict(v) if isinstance(v, dict) else v)
            for k, v in settings.items()
        }
        dirty = set()
        for token, kind in rules:
            if kind == "edition":
                key = "edition_keywords"
                if token.lower() not in {w.lower() for w in working.get(key, [])}:
                    working.setdefault(key, []).append(token)
                    dirty.add(key)
            elif kind == "language":
                key = "language_keywords"
                d = working.setdefault(key, {})
                if token.lower() not in d:
                    d[token.lower()] = token.title()
                    dirty.add(key)
            elif kind in ("arch_x64", "arch_x86", "arch_arm64"):
                key = "architecture_keywords"
                arch = kind.split("_", 1)[1]
                d = working.setdefault(key, {})
                d.setdefault(arch, [])
                if token not in d[arch]:
                    d[arch].append(token)
                    dirty.add(key)
            elif kind == "ignore_word":
                key = "ignore_filename_words"
                if token.lower() not in {w.lower() for w in working.get(key, [])}:
                    working.setdefault(key, []).append(token)
                    dirty.add(key)
        for key in dirty:
            self.db.set_setting(
                key, working[key], bump_version=True,
                note=f"refine-names: added rules to {key}",
            )


# =============================================================
# Main window
# =============================================================

STATUS_OPTIONS = ["(all)", "resolved", "needs_review", "verified", "ignored"]


class MainWindow(QMainWindow):
    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.db = Database(db_path)
        self.db.init_schema()

        self.setWindowTitle(f"App Catalog — {os.path.basename(db_path)}")
        self.resize(1200, 750)

        self._active_worker = None
        self._batch_rescan_remaining = []
        self._batch_rescan_total = 0
        self._batch_rescan_failures = []
        self._batch_rescan_current_path = None

        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()
        self.refresh_all()

        # Variant manifests (appcatalog.json): auto mode flushes changed
        # variants in the background every few seconds (setting
        # "manifest_auto_enabled", on by default).
        self._manifest_flush_worker = None
        self._manifest_timer = QTimer(self)
        self._manifest_timer.setInterval(4000)
        self._manifest_timer.timeout.connect(self._manifest_tick)
        self._manifest_timer.start()

    def _release_worker(self):
        """
        Clears self._active_worker safely. The custom finished_ok/failed
        signals fire from inside the worker's run(), which is not
        necessarily the exact instant the underlying OS thread has fully
        wound down -- dropping the last Python reference to a QThread
        that Qt doesn't yet consider finished() prints "QThread:
        Destroyed while thread is still running" and can crash outright.
        wait() blocks until the thread has genuinely finished; if it
        already has (the overwhelmingly common case), it returns
        immediately, so this costs nothing in practice.
        """
        worker = self._active_worker
        if worker is not None:
            worker.wait()
        self._active_worker = None

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        scan_roots_action = QAction("Scan roots…", self)
        scan_roots_action.setToolTip(
            "Add, re-scan, repath, or delete scan roots (picks up new/changed files "
            "on re-scan, skips anything unchanged)"
        )
        scan_roots_action.triggered.connect(self._show_scan_roots)
        toolbar.addAction(scan_roots_action)

        add_app_action = QAction("Add app…", self)
        add_app_action.setToolTip(
            "Add an app (or another variant of an existing app) straight from its installer "
            "file -- no scan root needed.")
        add_app_action.triggered.connect(self._add_app_dialog)
        toolbar.addAction(add_app_action)

        resolve_action = QAction("Re-resolve all", self)
        resolve_action.setToolTip("Re-run resolution on all scanned data with current settings")
        resolve_action.triggered.connect(self._run_resolve_all)
        toolbar.addAction(resolve_action)

        scrape_all_action = QAction("Scrape all (Winget)", self)
        scrape_all_action.setToolTip(
            "Background-enrich every resolved/verified app from the cached Winget "
            "manifest (downloads/refreshes the manifest first if it's stale)"
        )
        scrape_all_action.triggered.connect(lambda: self._run_scrape(None))
        toolbar.addAction(scrape_all_action)

        toolbar.addSeparator()

        organize_action = QAction("Organize…", self)
        organize_action.setToolTip(
            "Find duplicate apps, rename/merge categories, generate a "
            "structural health report, or physically reorganize files on disk"
        )
        organize_action.triggered.connect(self._open_organize_dialog)
        toolbar.addAction(organize_action)

        manifests_action = QAction("Write manifests…", self)
        manifests_action.setToolTip(
            "Create / refresh a manifest (<app> <version>.appcatalog.json) next to every variant: app name, "
            "setup file + dependent files, scraped details. A fresh database (or a "
            "re-organized library) recognises your manual work again from these. "
            "Select apps first and use the right-click menu to do just those."
        )
        manifests_action.triggered.connect(lambda: self._write_manifests_for(None))
        toolbar.addAction(manifests_action)


        toolbar.addSeparator()

        monitor_action = QAction("Monitor…", self)
        monitor_action.setToolTip(
            "Watch configured folders for new installers: identify them, "
            "match to an existing app (or create a new one), review a "
            "dry-run plan, then archive + move them into the organized "
            "structure"
        )
        monitor_action.triggered.connect(self._run_monitor)
        toolbar.addAction(monitor_action)

        toolbar.addSeparator()

        export_action = QAction("Export CSV…", self)
        export_action.triggered.connect(self._export_csv)
        toolbar.addAction(export_action)

        import_action = QAction("Import CSV…", self)
        import_action.triggered.connect(self._import_csv)
        toolbar.addAction(import_action)

        toolbar.addSeparator()

        columns_action = QAction("Columns…", self)
        columns_action.setToolTip("Show/hide columns in the apps table")
        columns_action.triggered.connect(self._show_column_picker)
        toolbar.addAction(columns_action)

        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

    def _build_central_widget(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        filter_bar = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search app name / catalog / subcatalog…")
        self.search_box.textChanged.connect(self._apply_filters)
        filter_bar.addWidget(self.search_box, stretch=2)

        self.status_combo = QComboBox()
        self.status_combo.addItems(STATUS_OPTIONS)
        self.status_combo.currentTextChanged.connect(self._apply_filters)
        filter_bar.addWidget(QLabel("Status:"))
        filter_bar.addWidget(self.status_combo)

        self.scrape_status_combo = QComboBox()
        self.scrape_status_combo.addItems(["(all)", "not_scraped", "scraped", "pending", "failed"])
        self.scrape_status_combo.currentTextChanged.connect(self._apply_filters)
        filter_bar.addWidget(QLabel("Scraped:"))
        filter_bar.addWidget(self.scrape_status_combo)
        outer.addLayout(filter_bar)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        self.catalog_tree = QTreeWidget()
        self.catalog_tree.setHeaderHidden(True)
        self.catalog_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.catalog_tree.addTopLevelItem(self._make_catalog_node("(all catalogs)"))
        self.catalog_tree.setCurrentItem(self.catalog_tree.topLevelItem(0))
        self.catalog_tree.itemSelectionChanged.connect(lambda *_: self._apply_filters())
        self.catalog_tree.setMaximumWidth(240)
        splitter.addWidget(self.catalog_tree)

        self.model = AppsTableModel(self.db)
        self.table = QTableView()
        self.table.setModel(self.model)
        # Custom delegate makes a selected row visually distinct even when
        # the model returns a status BackgroundRole (amber/rose/blue/gray).
        # Without this, the custom background can mask Qt's own selection
        # highlight.
        self.table.setItemDelegate(AppsTableDelegate(self.table))
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        # selectionChanged (not currentRowChanged) -- we need to know when
        # the selection SIZE changes so the detail panel can switch into
        # multi-edit mode.
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_apps_context_menu)
        splitter.addWidget(self.table)

        self.detail_panel = DetailPanel(self.db)
        self.detail_panel.app_changed.connect(self.refresh_all)
        self.detail_panel.scrape_requested.connect(self._run_scrape)
        splitter.addWidget(self.detail_panel)

        splitter.setSizes([180, 650, 400])

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        self.status_label = QLabel("Ready.")
        self.status_bar.addWidget(self.status_label)

    def _capture_state(self) -> dict:
        """Capture current sort column/order and selected app id(s)."""
        return {
            'sort_col': self.table.horizontalHeader().sortIndicatorSection(),
            'sort_order': self.table.horizontalHeader().sortIndicatorOrder(),
            'app_id': self.detail_panel.current_app_id,
            'multi_app_ids': (
                list(self.detail_panel._multi_app_ids)
                if self.detail_panel._multi_mode else None
            ),
        }

    def _restore_state(self, state: dict):
        """Restore sort and selection (single or multi) from a captured state."""
        if state['sort_col'] >= 0:
            self.table.sortByColumn(state['sort_col'], state['sort_order'])

        sel_model = self.table.selectionModel()
        sel_model.blockSignals(True)
        try:
            multi_ids = state.get('multi_app_ids')
            if multi_ids:
                sel_model.clearSelection()

                # Pick the "current" row (used for keyboard focus / scroll
                # position) but do NOT go through QTableView.setCurrentIndex()
                # -- its default selection command is ClearAndSelect, which
                # would collapse our multi-selection down to a single row.
                first_idx = None
                for row in range(self.model.rowCount()):
                    if self.model.app_id_at(row) in multi_ids:
                        first_idx = self.model.index(row, 0)
                        break

                if first_idx is not None:
                    # NoUpdate = set current index without touching selection.
                    sel_model.setCurrentIndex(
                        first_idx, QItemSelectionModel.NoUpdate
                    )

                # Now select every row that belongs to the multi-selection.
                for row in range(self.model.rowCount()):
                    if self.model.app_id_at(row) in multi_ids:
                        idx = self.model.index(row, 0)
                        sel_model.select(
                            idx,
                            QItemSelectionModel.Select | QItemSelectionModel.Rows,
                        )

            elif state['app_id'] is not None:
                for row in range(self.model.rowCount()):
                    if self.model.app_id_at(row) == state['app_id']:
                        self.table.selectRow(row)   # ClearAndSelect -- fine for single
                        break
        finally:
            sel_model.blockSignals(False)

        # Manually re-trigger the panel update; blockSignals above
        # suppressed the normal selectionChanged path.
        self._on_selection_changed()

    def select_app_by_id(self, app_id: int) -> bool:
        """
        Clears any active table filter, selects the given app in the main
        table, loads it in the detail panel, and brings this window to the
        front. Used by report/duplicate views (opened from a modal dialog)
        so a finding is a jump-to-app link rather than a dead end.
        Returns False if the app id isn't in the model (e.g. it was
        deleted since the report was generated).
        """
        # A filter (search text / catalog / status) could be hiding the
        # row entirely -- clear it so "jump to app" always works, not just
        # when the current filter happens to include it.
        if getattr(self.model, "search_text", "") or self.model.catalog_filter_spec or \
           self.model.status_filter or getattr(self.model, "scrape_status_filter", None):
            self.search_box.clear()
            self.catalog_tree.clearSelection()
            all_item = self.catalog_tree.topLevelItem(0)
            if all_item is not None:
                all_item.setSelected(True)
                self.catalog_tree.setCurrentItem(all_item)
            if hasattr(self, "status_combo"):
                self.status_combo.setCurrentIndex(0)
            if hasattr(self, "scrape_status_combo"):
                self.scrape_status_combo.setCurrentIndex(0)
            self._apply_filters()
        for row in range(self.model.rowCount()):
            if self.model.app_id_at(row) == app_id:
                self.table.selectRow(row)
                self.table.scrollTo(self.model.index(row, 0))
                self.detail_panel.load_app(app_id)
                self.raise_()
                self.activateWindow()
                return True
        QMessageBox.information(self, "App not found",
                                 "That app no longer exists in the catalog (it may have been merged or deleted).")
        return False

    def _make_catalog_node(self, label: str, catalog: Optional[str] = None,
                            subcatalog: Optional[str] = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label])
        item.setData(0, Qt.UserRole, {"catalog": catalog, "subcatalog": subcatalog})
        return item

    def _current_catalog_selections(self) -> list[tuple[Optional[str], Optional[str]]]:
        """Every selected (catalog, subcatalog) pair. (None, None) means
        '(all catalogs)' is picked; if nothing is selected we default to it."""
        items = self.catalog_tree.selectedItems()
        if not items:
            return [(None, None)]
        seen = set()
        out: list[tuple[Optional[str], Optional[str]]] = []
        for item in items:
            data = item.data(0, Qt.UserRole) or {}
            pair = (data.get("catalog"), data.get("subcatalog"))
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
        return out

    def refresh_all(self, preserve_state: bool = True):
        """
        Refresh the entire UI (table, catalog list, column visibility).
        If preserve_state is True, restore sort order and selection
        (single or multi).
        """
        state = self._capture_state() if preserve_state else None

        # Update column visibility
        self._apply_column_visibility()

        # Update catalog/subcatalog tree, preserving every selected
        # (catalog, subcatalog) pair rather than raw text -- a subcatalog
        # name could otherwise collide with an unrelated catalog's name.
        prev_selections = set(self._current_catalog_selections())
        self.catalog_tree.blockSignals(True)
        self.catalog_tree.clear()
        all_item = self._make_catalog_node("(all catalogs)")
        self.catalog_tree.addTopLevelItem(all_item)
        if (None, None) in prev_selections:
            all_item.setSelected(True)
        for cat, subs in self.model.catalog_subcatalog_tree().items():
            cat_item = self._make_catalog_node(cat, catalog=cat)
            self.catalog_tree.addTopLevelItem(cat_item)
            if (cat, None) in prev_selections:
                cat_item.setSelected(True)
            for sub in sorted(set(subs)):
                sub_item = self._make_catalog_node(sub, catalog=cat, subcatalog=sub)
                cat_item.addChild(sub_item)
                if (cat, sub) in prev_selections:
                    sub_item.setSelected(True)
            cat_item.setExpanded(True)
        # If nothing ended up selected (e.g. the old selection disappeared),
        # fall back to "(all catalogs)".
        if not self.catalog_tree.selectedItems():
            all_item.setSelected(True)
            self.catalog_tree.setCurrentItem(all_item)
        self.catalog_tree.blockSignals(False)

        # Apply filters – this will refresh the model
        self._apply_filters()

        # Restore state if requested
        if state:
            self._restore_state(state)

    def _apply_filters(self):
        self.model.search_text = self.search_box.text().strip()
        selections = self._current_catalog_selections()
        # "(all catalogs)" anywhere in the selection wins — clear the filter.
        if (None, None) in selections:
            self.model.catalog_filter_spec = []
        else:
            self.model.catalog_filter_spec = [
                (c, s) for (c, s) in selections if c is not None
            ]
        status = self.status_combo.currentText()
        self.model.status_filter = None if status == "(all)" else status
        scrape_status = self.scrape_status_combo.currentText()
        self.model.scrape_status_filter = None if scrape_status == "(all)" else scrape_status
        self.model.refresh()

    def _on_selection_changed(self, *args):
        """
        Dispatch to the detail panel based on how many rows are selected:
          - 0 selected -> clear()
          - 1 selected -> load_app() (full single-app view)
          - N > 1      -> load_apps() (multi-edit view)
        """
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            self.detail_panel.clear()
            return
        app_ids = [self.model.app_id_at(r.row()) for r in selected_rows]
        app_ids = [i for i in app_ids if i is not None]
        if not app_ids:
            self.detail_panel.clear()
        elif len(app_ids) == 1:
            self.detail_panel.load_app(app_ids[0])
        else:
            self.detail_panel.load_apps(app_ids)

    def _selected_app_ids(self) -> list[int]:
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        ids = [self.model.app_id_at(r) for r in rows]
        return [i for i in ids if i is not None]

    def _show_apps_context_menu(self, pos):
        app_ids = self._selected_app_ids()
        if not app_ids:
            return
        menu = QMenu(self)
        scrape_action = menu.addAction(
            f"Auto-scrape selected ({len(app_ids)}) from Winget"
            if len(app_ids) > 1 else "Auto-scrape (Winget)"
        )
        search_action = menu.addAction("Manual-Scrape")
        search_action.setEnabled(len(app_ids) == 1)
        if len(app_ids) != 1:
            search_action.setToolTip("Select exactly one app to search/match it manually")

        menu.addSeparator()

        manifest_action = menu.addAction(
            f"Create/update manifests ({len(app_ids)} apps)"
            if len(app_ids) > 1 else "Create/update manifest")
        manifest_action.setToolTip(
            "Write the .appcatalog.json manifest next to each variant of the selected app(s)")
        repair_action = menu.addAction("Repair empty apps… (whole library)")
        repair_action.setToolTip(
            "Find apps with no variants (left behind by an old rescan bug that duplicated "
            "renamed/scraped apps) and merge the duplicates back / remove the leftovers.")

        menu.addSeparator()

        refine_action = menu.addAction(
            f"Refine names (one-time)… — {len(app_ids)} apps"
            if len(app_ids) > 1 else "Refine names (one-time)…"
        )
        refine_action.setToolTip(
            "Point at a token ('Lite', 'Ultimate', 'Repack') and say what it is. "
            "Rules apply ONLY to this run and to the selected apps — nothing is "
            "saved unless you tick the box in the dialog."
        )

        menu.addSeparator()

        delete_action = menu.addAction("Delete selected")
        delete_action.triggered.connect(self.delete_selected_apps)
        open_loc_action = menu.addAction("Open file location")
        open_loc_action.triggered.connect(self.open_selected_location)

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == scrape_action:
            self._run_scrape(app_ids)
        elif chosen == search_action:
            app_row = next((r for r in self.model._rows if r["id"] == app_ids[0]), None)
            app_name = app_row["name"] if app_row else ""
            settings = self.db.get_all_settings()
            dialog = SearchMatchDialog(self.db, app_ids[0], app_name, settings, parent=self)
            if dialog.exec():
                self.refresh_all()
                if self.detail_panel.current_app_id == app_ids[0]:
                    self.detail_panel.load_app(app_ids[0])
        elif chosen == refine_action:
            self._open_refine_names_dialog(app_ids)
        elif chosen == manifest_action:
            self._write_manifests_for(app_ids)
        elif chosen == repair_action:
            self._repair_empty_apps()

    def _open_refine_names_dialog(self, app_ids):
        dialog = RefineNamesDialog(self.db, app_ids, parent=self)
        if not dialog.exec():
            return
        if dialog.saved_permanently:
            self.status_label.setText(
                f"Refined {dialog.applied_count} app(s). Rules saved permanently."
            )
        else:
            self.status_label.setText(
                f"Refined {dialog.applied_count} app(s) (one-time rules — not saved)."
            )
        self.refresh_all()

    def delete_selected_apps(self):
        app_ids = self._selected_app_ids()
        if not app_ids:
            return
        if QMessageBox.question(self, "Confirm Delete",
                                f"Delete {len(app_ids)} app(s) and their versions?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            for app_id in app_ids:
                self.db.delete_app(app_id)
            self.refresh_all()

    def open_selected_location(self):
        app_ids = self._selected_app_ids()
        if not app_ids:
            return
        app_id = app_ids[0]
        versions = self.db.get_versions_for_app(app_id)
        if versions:
            # Use source_path (the folder containing the variant)
            self.detail_panel._open_file_location(versions[0]['source_path'])
        else:
            QMessageBox.information(self, "No versions", "This app has no versions.")

    def _show_scan_roots(self):
        dialog = ScanRootsDialog(self.db, parent=self)
        dialog.exec()

    def _show_column_picker(self):
        """Non-closing column visibility picker. Uses a QMenu of
        QWidgetActions holding QCheckBox widgets — clicking a checkbox
        toggles the column without triggering the menu's close-on-pick
        behaviour, so the menu stays open until the user clicks outside."""
        visible = set(self.db.get_setting("visible_columns", [k for k, _ in COLUMNS]))
        menu = QMenu(self)
        for key, label in COLUMNS:
            action = QWidgetAction(menu)
            cb = QCheckBox(label)
            cb.setChecked(key in visible)
            if key == "name":
                cb.setEnabled(False)
                cb.setToolTip("App Name cannot be hidden")
            else:
                cb.toggled.connect(
                    lambda checked, k=key: self._toggle_column(k, checked)
                )
            action.setDefaultWidget(cb)
            menu.addAction(action)
        menu.exec(self.table.viewport().mapToGlobal(self.table.rect().topLeft()))

    def _toggle_column(self, key: str, checked: bool):
        visible = set(self.db.get_setting("visible_columns", [k for k, _ in COLUMNS]))
        if checked:
            visible.add(key)
        else:
            visible.discard(key)
        visible.add("name")   # App Name can't be hidden
        self.db.set_setting("visible_columns", sorted(visible), bump_version=False)
        self._apply_column_visibility()

    def _apply_column_visibility(self):
        visible = set(self.db.get_setting("visible_columns", [k for k, _ in COLUMNS]))
        for i, (key, _label) in enumerate(COLUMNS):
            self.table.setColumnHidden(i, key not in visible and key != "name")

    def _open_organize_dialog(self):
        dialog = OrganizeDialog(self.db, parent=self)
        dialog.exec()
        self.refresh_all()

    def _maybe_show_folder_layout_dialog(self, root_path: str) -> bool:
        """
        Runs before every scan (main thread, non-blocking -- just a
        top-level os.scandir): opens FolderLayoutDialog if this is the
        first-ever scan of the root, or if new top-level folders have
        appeared since it was last configured. Returns False if the user
        cancelled, in which case the scan must not proceed at all.
        """
        scan_root_id = self.db.ensure_scan_root(root_path)
        scan_root_row = self.db.get_scan_root_by_id(scan_root_id)
        layout = self.db.get_folder_layout(scan_root_id)

        top_level = list_top_level_folders(root_path)
        configured = {k.split("/")[0] for k in layout.keys()}
        new_folders = [n for n in top_level if n.lower() not in configured]

        if layout and not new_folders:
            return True  # already configured, nothing new -- proceed silently

        dialog = FolderLayoutDialog(
            self.db, scan_root_row, parent=self,
            only_new_folders=new_folders if layout else None,
        )
        return dialog.exec() == QDialog.Accepted

    def _run_scan_and_resolve(self, root_path: str):
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve job is already running.")
            return
        if not self._maybe_show_folder_layout_dialog(root_path):
            return
        worker = ScanAndResolveWorker(self.db_path, root_path)
        worker.scan_progress.connect(self._on_scan_progress)
        worker.resolve_started.connect(lambda: self.status_label.setText("Scan complete. Resolving…"))
        worker.finished_ok.connect(self._on_scan_resolve_finished)
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.status_label.setText(f"Scanning {root_path} …")
        worker.start()

    def _run_rescan_all_roots(self):
        """
        Re-scans every known scan root, one at a time -- never in
        parallel, since ScanAndResolveWorker/the DB connection aren't set
        up for concurrent jobs. Each root still goes through the normal
        single-root path (including FolderLayoutDialog if that root has
        new top-level folders); a root that fails or gets skipped doesn't
        stop the rest of the batch. Continuation is driven from
        _on_scan_resolve_finished/_on_job_failed (already connected
        before the worker starts) rather than a fresh connect() made
        after _run_scan_and_resolve returns -- connecting afterwards
        would race a worker that finishes before that connect() runs.
        """
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve/scrape job is already running.")
            return
        conn = self.db.connect()
        paths = [r["path"] for r in conn.execute("SELECT path FROM scan_roots ORDER BY path").fetchall()]
        if not paths:
            QMessageBox.information(self, "No scan roots", "There are no scan roots to re-scan yet.")
            return
        self._batch_rescan_remaining = paths
        self._batch_rescan_total = len(paths)
        self._batch_rescan_failures = []
        self._batch_rescan_current_path = None
        self._advance_batch_rescan()

    def _advance_batch_rescan(self):
        if not self._batch_rescan_remaining:
            total = self._batch_rescan_total
            failures = self._batch_rescan_failures
            self._batch_rescan_total = 0
            self._batch_rescan_current_path = None
            if total:  # guards against a stray call when no batch is active
                msg = f"Re-scanned all {total} scan root(s)."
                if failures:
                    msg += f"\n\n{len(failures)} had a problem:\n" + "\n".join(failures)
                self.status_label.setText("Ready.")
                QMessageBox.information(self, "Re-scan all roots", msg)
            return

        next_path = self._batch_rescan_remaining.pop(0)
        self._batch_rescan_current_path = next_path
        done_so_far = self._batch_rescan_total - len(self._batch_rescan_remaining)
        self.status_label.setText(
            f"Re-scanning root {done_so_far}/{self._batch_rescan_total}: {next_path} …"
        )
        worker_before = self._active_worker
        self._run_scan_and_resolve(next_path)
        if self._active_worker is worker_before:
            # _run_scan_and_resolve returned without starting a job (the
            # user cancelled this root's FolderLayoutDialog) -- skip it
            # and keep the batch moving rather than stalling on one root.
            self._batch_rescan_failures.append(f"{next_path} (skipped)")
            self._batch_rescan_current_path = None
            self._advance_batch_rescan()

    def _run_clean_library(self, root_path: Optional[str]):
        """Confirms every app already in the catalog -- from `root_path`,
        or the whole catalog when root_path is None -- still exists on
        disk. The opposite of a scan, see app_manager.scan_for_missing_
        sources()'s docstring. Never looks for new apps."""
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve/scrape job is already running.")
            return
        worker = CleanLibraryScanWorker(self.db_path, root_path=root_path)
        worker.progress.connect(self._on_clean_library_progress)
        worker.finished_ok.connect(lambda missing: self._on_clean_library_scanned(missing, root_path))
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        target = root_path or "the whole catalog"
        self.status_label.setText(f"Checking whether apps from {target} still exist…")
        worker.start()

    def _on_clean_library_progress(self, checked: int, total: int):
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(checked)
        self.status_label.setText(f"Checking… {checked}/{total}")

    def _on_clean_library_scanned(self, missing: list, root_path: Optional[str]):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        self.status_label.setText("Ready.")
        target = f"from:\n{root_path}" if root_path else "in the whole catalog"
        if not missing:
            QMessageBox.information(
                self, "Clean library",
                f"Everything already in the catalog {target}\n\n"
                "still exists on disk. Nothing to clean up.",
            )
            return
        dialog = CleanLibraryReviewDialog(self.db, self.db_path, missing, parent=self)
        if dialog.exec():
            self.refresh_all()

    def _run_resolve_all(self):
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve job is already running.")
            return
        n_apps = self.db.connect().execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Re-resolve ALL apps?")
        box.setText(f"Re-resolve all {n_apps} apps against the current settings?")
        box.setInformativeText(
            "This re-derives names, versions, editions, architectures and categories for every app "
            "that has no lock, using the current settings and the folder / file names.\n\n"
            "Protected from this: apps with a locked name / catalog / subcatalog, verified apps, and "
            "variants you moved, merged or split by hand. Edition / architecture / language you typed "
            "into a variant are NOT locked and will be recalculated.\n\n"
            "A backup copy of the catalog is saved first (backups folder next to the database).\n\n"
            "Usually a normal Scan roots → Rescan is what you want; this is only needed after "
            "changing naming / filter settings.")
        yes = box.addButton("Re-resolve all", QMessageBox.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)      # Enter / Space must never trigger the destructive action
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return
        worker = ResolveWorker(self.db_path)
        worker.finished_ok.connect(self._on_resolve_only_finished)
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.status_label.setText("Re-resolving all apps with current settings…")
        worker.start()

    # ---------------------------------------------------------------
    # Manual curation: add an app directly / repair empty apps (app_curation.py)
    # ---------------------------------------------------------------
    def _add_app_dialog(self):
        dlg = AddAppDialog(self.db, parent=self)
        if dlg.exec() != QDialog.Accepted or not dlg.result_info:
            return
        info = dlg.result_info
        self.refresh_all()
        self.select_app_by_id(info["app_id"])
        self.status_label.setText("App added." if info["created_app"] else "Variant added to the existing app.")
        if dlg.scrape_after:
            self._run_scrape([info["app_id"]])

    def _repair_empty_apps(self):
        items = app_curation.find_empty_app_repairs(self.db)
        if not items:
            QMessageBox.information(self, "Repair empty apps", "No empty apps found — nothing to repair.")
            return
        dlg = RepairEmptyAppsDialog(self.db, items, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.applied is not None:
            a = dlg.applied
            self.refresh_all()
            QMessageBox.information(
                self, "Repair empty apps",
                f"Merged {a['merged']}, deleted {a['deleted']}, skipped {a['skipped']}.")

    # ---------------------------------------------------------------
    # Variant manifests (appcatalog.json) -- see app_manifest.py
    # ---------------------------------------------------------------
    def _manifest_tick(self):
        """Auto mode: every few seconds, write manifests for variants whose
        data changed. Skipped while a scan/resolve/scrape/manual-manifest job
        runs (they flush themselves when done) or while auto mode is off."""
        try:
            if (self._active_worker is not None
                    or app_manifest.auto_flush_suspended()
                    or (self._manifest_flush_worker is not None
                        and self._manifest_flush_worker.isRunning())
                    or not self.db.get_setting("manifest_auto_enabled", True)
                    or not app_manifest.has_dirty(self.db)):
                return
            worker = ManifestFlushWorker(self.db_path)
            worker.finished_ok.connect(self._on_manifest_flush_done)
            self._manifest_flush_worker = worker
            worker.start()
        except Exception:
            pass     # background nicety -- must never disturb the UI

    def _on_manifest_flush_done(self, result):
        w, self._manifest_flush_worker = self._manifest_flush_worker, None
        if w is not None:
            w.wait()
        if result is not None and (result.created or result.updated):
            self.status_label.setText(
                f"Manifests: {result.created} created, {result.updated} updated.")
        if result is not None and result.failed:
            self.status_label.setText(
                f"Manifests: {result.failed} could not be written (see app.log).")

    def _write_manifests_for(self, app_ids: Optional[list]):
        """Manual: create/refresh manifests for the given apps (None = every app)."""
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "Another job is already running.")
            return
        conn = self.db.connect()
        if app_ids is None:
            variant_ids = None
            total = conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
            have = conn.execute(
                "SELECT COUNT(*) FROM variants WHERE manifest_hash IS NOT NULL").fetchone()[0]
            scope = "every variant in the catalog"
        else:
            variant_ids = app_manifest.variant_ids_for_apps(self.db, app_ids)
            total = len(variant_ids)
            have = 0
            for i in range(0, len(variant_ids), 500):
                chunk = variant_ids[i:i + 500]
                have += conn.execute(
                    "SELECT COUNT(*) FROM variants WHERE manifest_hash IS NOT NULL AND id IN (%s)"
                    % ",".join("?" * len(chunk)), chunk).fetchone()[0]
            scope = f"the {len(app_ids)} selected app(s)"
        if total == 0:
            QMessageBox.information(self, "Manifests", "Nothing to write — no variants found.")
            return
        answer = QMessageBox.question(
            self, "Create / update manifests",
            f"Write a .appcatalog.json manifest next to {scope}?\n\n"
            f"{total} variant(s), {have} already have one (those are refreshed only if "
            "something changed).\n\n"
            "Each file records the app name, the setup file and its dependent files, "
            "and any scraped details — so a new database or a re-organized library can "
            "recognise this work again. No installer is modified or moved.",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        worker = ManifestWorker(self.db_path, variant_ids=variant_ids)
        worker.progress.connect(self._on_manifest_progress)
        worker.finished_ok.connect(self._on_manifest_finished)
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, max(total, 1))
        self.status_label.setText(f"Writing manifests for {total} variant(s)…")
        worker.start()

    def _on_manifest_progress(self, done: int, total: int):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.status_label.setText(f"Writing manifests… {done}/{total}")

    def _on_manifest_finished(self, result):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        self.status_label.setText(f"Manifests: {result.summary()}.")
        problems = [r for r in result.items if r.status == "failed"]
        if problems:
            lines = "\n".join(f"• {r.message}" for r in problems[:8])
            more = f"\n…and {len(problems) - 8} more (see app.log)" if len(problems) > 8 else ""
            QMessageBox.warning(
                self, "Manifests",
                f"{result.summary()}.\n\nSome could not be written (read-only drive, "
                f"missing folder…):\n{lines}{more}")
        else:
            QMessageBox.information(self, "Manifests", f"Done — {result.summary()}.")

    def closeEvent(self, event):
        """Last chance to persist pending manifests before the app exits."""
        try:
            self._manifest_timer.stop()
            if self._manifest_flush_worker is not None:
                self._manifest_flush_worker.wait(5000)
            if self.db.get_setting("manifest_auto_enabled", True):
                app_manifest.flush_dirty(self.db)
        except Exception:
            pass
        super().closeEvent(event)

    def _run_scrape(self, app_ids: Optional[list] = None):
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve/scrape job is already running.")
            return
        worker = ScrapeWorker(self.db_path, app_ids=app_ids)
        worker.progress.connect(self._on_scrape_progress)
        worker.finished_ok.connect(self._on_scrape_finished)
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        label = "selected app" if app_ids and len(app_ids) == 1 else \
                (f"{len(app_ids)} selected apps" if app_ids else "all eligible apps")
        self.status_label.setText(f"Scraping {label} from the Winget manifest…")
        worker.start()

    def _on_scrape_progress(self, progress: ScrapeProgress):
        if progress.total:
            self.progress_bar.setRange(0, progress.total)
            self.progress_bar.setValue(progress.processed)
        self.status_label.setText(
            f"Scraping… {progress.processed}/{progress.total} "
            f"({progress.matched} matched, {progress.not_found} not found)"
            + (f" — {progress.current_name}" if progress.current_name else "")
        )

    def _on_scrape_finished(self, result: ScrapeResult):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        if result.status == "failed":
            self.status_label.setText("Scrape failed to run.")
            detail = f"\n\n({result.manifest_error})" if result.manifest_error else ""
            QMessageBox.warning(
                self, "Scrape failed",
                "Could not load the Winget manifest (no usable cache and the network "
                f"fetch failed).{detail}",
            )
        else:
            source_note = {
                "network": "freshly downloaded",
                "cache": "cached",
                "stale_cache_fallback": "stale cache — network refresh failed",
            }.get(result.manifest_source, result.manifest_source)
            self.status_label.setText(
                f"Scrape done ({source_note} manifest). "
                f"{result.matched} of {result.total} apps matched, "
                f"{result.not_found} not found."
            )
        self.refresh_all()
        if self.detail_panel.current_app_id is not None:
            self.detail_panel.load_app(self.detail_panel.current_app_id)

    # ---------------------------------------------------------------
    # Monitor job (checkpoint 18) -- all logic lives in monitor.py;
    # this is just the wiring that keeps it inside the shared
    # _active_worker lock.
    # ---------------------------------------------------------------
    def _run_monitor(self):
        if self._active_worker is not None:
            QMessageBox.information(
                self, "Busy",
                "A scan/resolve/scrape/monitor job is already running.")
            return
        job = MonitorJob(self, self.db, self.db_path)
        job.progress.connect(self.status_label.setText)
        job.job_finished.connect(self._on_monitor_finished)
        job.job_failed.connect(self._on_monitor_failed)
        if not job.start():
            # User cancelled the start dialog -- nothing was started, so
            # we never held the lock and there's nothing to release.
            job.deleteLater()
            return
        self._active_worker = job
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

    def _on_monitor_finished(self, result):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        if result is None:
            self.status_label.setText("Monitor: cancelled.")
            return
        self.status_label.setText(
            f"Monitor done. Moved/copied: {result.moved}, "
            f"skipped: {result.skipped}, failed: {result.failed}."
        )
        # Refresh so newly-attached variants appear in the table / detail
        # panel immediately.
        self.refresh_all()
        if self.detail_panel.current_app_id is not None:
            self.detail_panel.load_app(self.detail_panel.current_app_id)

    def _on_monitor_failed(self, err: str):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        self.status_label.setText("Monitor failed.")
        QMessageBox.critical(self, "Monitor failed", err)

    def _on_scan_progress(self, progress):
        self.status_label.setText(
            f"Scanning… {progress.folders_seen} folders seen, "
            f"{progress.install_units_found} install units found, "
            f"{progress.skipped_unchanged} skipped (unchanged)"
        )

    def _on_scan_resolve_finished(self, scan_result, resolve_result):
        self.progress_bar.setVisible(False)
        self._release_worker()
        if resolve_result is None:
            self.status_label.setText("Scan cancelled.")
        else:
            self.status_label.setText(
                f"Done. {scan_result.install_units_found} install units found, "
                f"{resolve_result.apps_created} new apps, {resolve_result.apps_updated} apps updated."
            )
        self.refresh_all()
        if self._batch_rescan_total and self._batch_rescan_current_path is not None:
            self._batch_rescan_current_path = None
            self._advance_batch_rescan()

    def _on_resolve_only_finished(self, resolve_result):
        self.progress_bar.setVisible(False)
        self._release_worker()
        self.status_label.setText(
            f"Re-resolve done. {resolve_result.apps_created} new, "
            f"{resolve_result.apps_updated} updated, "
            f"{resolve_result.apps_skipped_locked} had locked fields preserved, "
            f"{getattr(resolve_result, 'variants_pinned', 0)} variants kept in their protected app."
        )
        self.refresh_all()

    def _on_job_failed(self, error_message: str):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._release_worker()
        self.status_label.setText("Job failed.")
        QMessageBox.critical(self, "Job failed", error_message)
        if self._batch_rescan_total and self._batch_rescan_current_path is not None:
            self._batch_rescan_failures.append(f"{self._batch_rescan_current_path}: {error_message}")
            self._batch_rescan_current_path = None
            self._advance_batch_rescan()

    def _open_settings(self):
        dialog = SettingsDialog(self.db, parent=self)
        if dialog.exec():
            self.status_label.setText("Settings saved. Use 'Re-resolve all' to apply to existing data.")

    def _export_csv(self):
        dialog = CsvExportDialog(parent=self)
        if not dialog.exec():
            return
        output_dir = dialog.output_dir()
        if not output_dir:
            return
        try:
            paths = export_apps_csv(self.db, output_dir, batch_size=dialog.batch_size_value())
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self.status_label.setText(f"Exported {len(paths)} CSV file(s) to {output_dir}")
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {len(paths)} file(s):\n" + "\n".join(os.path.basename(p) for p in paths[:10])
            + ("\n…" if len(paths) > 10 else ""),
        )

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose CSV to import", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            counts = import_apps_csv(self.db, path)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return
        self.status_label.setText(
            f"Import done: {counts['updated']} updated, {counts['skipped']} unchanged, "
            f"{counts['not_found']} not found."
        )
        self.refresh_all()
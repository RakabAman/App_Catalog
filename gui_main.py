"""
gui_main.py – All UI components: main window, detail panel, dialogs.
"""

import os
import platform
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTableView,
    QLineEdit, QLabel, QPushButton, QToolBar,
    QFileDialog, QProgressBar, QStatusBar, QComboBox, QMessageBox,
    QAbstractItemView, QDialog, QFormLayout, QDoubleSpinBox, QSpinBox,
    QCheckBox, QPlainTextEdit, QTabWidget, QScrollArea, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit, QInputDialog,
    QMenu, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QStackedWidget,
    QToolButton,
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
from app_manager import execute_clean_library

from gui_backend import (
    AppsTableModel, AppPickerDialog, COLUMNS, export_apps_csv, import_apps_csv,
    ScanWorker, ScanAndResolveWorker, ResolveWorker, ScrapeWorker,
    ChocoSearchWorker, WingetSearchWorker, CleanLibraryScanWorker,
)


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


class DetailPanel(QWidget):
    app_changed = Signal()
    scrape_requested = Signal(list)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_app_id: int | None = None
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

        # Row 0: Publisher | Latest version
        self.publisher_edit = QLineEdit()
        self.publisher_edit.setReadOnly(True)
        self.publisher_edit.setPlaceholderText("(not scraped yet)")
        scrape_layout.addWidget(QLabel("Publisher:"), 0, 0)
        scrape_layout.addWidget(self.publisher_edit, 0, 1)

        self.latest_version_edit = QLineEdit()
        self.latest_version_edit.setReadOnly(True)
        self.latest_version_edit.setPlaceholderText("(not scraped yet)")
        scrape_layout.addWidget(QLabel("Latest version:"), 0, 2)
        scrape_layout.addWidget(self.latest_version_edit, 0, 3)

        # Row 1: Homepage – now spans columns 1 to 3 (full width after label)
        self.homepage_edit = QLineEdit()
        self.homepage_edit.setReadOnly(True)
        self.homepage_edit.setPlaceholderText("(not scraped yet)")
        self.homepage_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        scrape_layout.addWidget(QLabel("Homepage:"), 1, 0)
        scrape_layout.addWidget(self.homepage_edit, 1, 1, 1, 3)   # ← spans 3 columns

        # Row 2: Winget ID | Choco ID
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

        # Row 3: Tags (spans columns 1-3)
        self.scraped_tags_edit = QLabel()
        self.scraped_tags_edit.setWordWrap(True)
        self.scraped_tags_edit.setMinimumHeight(30)
        self.scraped_tags_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.scraped_tags_edit.setStyleSheet("QLabel { background: transparent; }")
        scrape_layout.addWidget(QLabel("Tags:"), 3, 0)
        scrape_layout.addWidget(self.scraped_tags_edit, 3, 1, 1, 3)

        # Column stretches – column 1 and 3 take extra space
        scrape_layout.setColumnStretch(1, 1)
        scrape_layout.setColumnStretch(3, 1)

        layout.addWidget(scrape_box)

        # Action buttons (unchanged)
        actions_row = QHBoxLayout()
        self.verify_btn = QPushButton("Mark verified")
        self.verify_btn.clicked.connect(self._mark_verified)
        self.reresolve_btn = QPushButton("Re-resolve this app")
        self.reresolve_btn.clicked.connect(self._reresolve)
        actions_row.addWidget(self.verify_btn)
        actions_row.addWidget(self.reresolve_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        # Variants table (unchanged)
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
        self.variants_table.setEditTriggers(QTableWidget.NoEditTriggers)
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

        layout.addWidget(variants_box)

    def clear(self):
        self.current_app_id = None
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

        self.publisher_edit.setText(app.get("publisher") or "")
        self.homepage_edit.setText(app.get("homepage_url") or "")

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

        # Tags
        tag_rows = conn.execute(
            """SELECT t.name FROM tags t JOIN app_tags at ON at.tag_id = t.id
               WHERE at.app_id = ? ORDER BY t.name""",
            (app_id,),
        ).fetchall()
        self.scraped_tags_edit.setText(", ".join(r["name"] for r in tag_rows))

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
        self.variants_table.setSortingEnabled(False)
        self.variants_table.setRowCount(len(variants))
        for i, v in enumerate(variants):
            scanned = (v["scanned_at"] or "")[:10]
            values = [v["version"], v["file_name"], v["edition"], v["file_type"],
                      v["name_source"], scanned, v["source_path"]]
            for j, val in enumerate(values):
                item = QTableWidgetItem(val or "")
                item.setData(Qt.UserRole, v["id"])
                if VARIANT_COLUMNS[j][0] == "scanned_at" and v["scanned_at"]:
                    item.setToolTip(v["scanned_at"])
                if v["is_ignored"]:
                    item.setForeground(Qt.gray)
                self.variants_table.setItem(i, j, item)
        self.variants_table.setSortingEnabled(True)
        self.variants_table.resizeColumnsToContents()

        # (Removed duplicate populate/setCurrentText calls)

    def _commit_field(self, field: str, widget):
        if self.current_app_id is None:
            return
        if isinstance(widget, QComboBox):
            value = widget.currentText().strip()
        else:
            value = widget.text().strip()

        # Name is not allowed to be cleared (a nameless app is unusable in
        # the table and every picker dialog). Catalog/subcatalog CAN be
        # cleared -- an empty catalog is a valid state (the app just isn't
        # filed under any category yet) and the user needs a way to get
        # back to it if they mis-typed one.
        if field == "name" and not value:
            return

        from resolver import edit_app_field
        edit_app_field(self.db, self.current_app_id, field, value)
        if field == "name":
            self.name_lock_label.setText("🔒")
        self.app_changed.emit()
        
    def _on_catalog_changed(self, text):
        if self.current_app_id is None:
            return
        self._commit_field("catalog", self.catalog_edit)

    def _on_subcatalog_changed(self, text):
        if self.current_app_id is None:
            return
        self._commit_field("subcatalog", self.subcatalog_edit)

    def _mark_verified(self):
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
        name, ok = QInputDialog.getText(self, "Split into new app", "New app name:")
        if ok and name.strip():
            split_variant_to_new_app(self.db, vid, name.strip())
            self.load_app(self.current_app_id)
            self.app_changed.emit()

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

    def _variant_full_path(self, variant: dict) -> str:
        if variant.get("file_name"):
            return os.path.join(variant["source_path"], variant["file_name"])
        return variant["source_path"]

    def _show_variant_context_menu(self, pos):
        rows = self._selected_variant_rows()
        if not rows:
            return
        menu = QMenu(self)
        open_location_action = menu.addAction("Open file location")
        run_action = menu.addAction("Run / open file")
        menu.addSeparator()
        reeval_action = menu.addAction("Re-evaluate selected")
        scrape_action = menu.addAction("Scrape app metadata (Winget)")
        choco_action = menu.addAction("Search & match…")
        menu.addSeparator()
        ignore_action = menu.addAction("Ignore selected")

        single = rows[0] if len(rows) == 1 else None
        open_location_action.setEnabled(single is not None)
        run_action.setEnabled(single is not None)

        chosen = menu.exec(self.variants_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == open_location_action and single:
            self._open_file_location(self._variant_full_path(single))
        elif chosen == run_action and single:
            self._run_file(self._variant_full_path(single))
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

    def _open_search_match(self):
        settings = self.db.get_all_settings()
        dialog = SearchMatchDialog(
            self.db, self.current_app_id, self.name_edit.text() or "", settings, parent=self
        )
        if dialog.exec():
            self.load_app(self.current_app_id)
            self.app_changed.emit()

    def _open_file_location(self, full_path: str):
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
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Path", "Last scan started", "Last scan finished", "Status", "", ""]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add new scan root…")
        add_btn.clicked.connect(self._add_new_root)
        btn_row.addWidget(add_btn)
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
                self.table.setItem(i, j, QTableWidgetItem(val))
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
        self.table.resizeColumnsToContents()

    def _rescan(self, path: str):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_scan_and_resolve"):
            main_window._run_scan_and_resolve(path)
        self.accept()

    def _clean_library(self, path: str):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_run_clean_library"):
            main_window._run_clean_library(path)
        self.accept()

    def _add_new_root(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to scan")
        if not folder:
            return
        self._rescan(folder)


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
            f"<b>{len(missing)} item(s)</b> already in the catalog no longer exist on disk "
            "(deleted, moved, or replaced outside this app). Uncheck anything you don't want "
            "removed from the catalog -- this only removes the catalog record, there's nothing "
            "left on disk to touch either way."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(missing), 4)
        self.table.setHorizontalHeaderLabels(["Remove", "App", "Version", "Source path (no longer exists)"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
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
            self.table.setItem(i, 3, QTableWidgetItem(item.source_path))
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
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Settings — applies live, no restart needed")
        self.resize(520, 500)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        settings = db.get_all_settings()

        # Resolver confidence
        conf_tab = QWidget()
        conf_form = QFormLayout(conf_tab)
        self.auto_accept = QDoubleSpinBox()
        self.auto_accept.setRange(0, 1)
        self.auto_accept.setSingleStep(0.05)
        self.auto_accept.setValue(settings.get("confidence_auto_accept", 0.85))
        conf_form.addRow("Auto-accept threshold", self.auto_accept)

        self.needs_review = QDoubleSpinBox()
        self.needs_review.setRange(0, 1)
        self.needs_review.setSingleStep(0.05)
        self.needs_review.setValue(settings.get("confidence_needs_review", 0.60))
        conf_form.addRow("Needs-review threshold", self.needs_review)

        self.fuzzy_threshold = QSpinBox()
        self.fuzzy_threshold.setRange(50, 100)
        self.fuzzy_threshold.setValue(settings.get("fuzzy_match_threshold", 88))
        conf_form.addRow("Fuzzy match threshold (0-100)", self.fuzzy_threshold)

        self.prefer_pe = QCheckBox("Prefer .exe version metadata over parsed name")
        self.prefer_pe.setChecked(settings.get("prefer_pe_version_over_parsed", True))
        conf_form.addRow(self.prefer_pe)
        tabs.addTab(conf_tab, "Resolver")

        # Archive handling
        arch_tab = QWidget()
        arch_form = QFormLayout(arch_tab)
        self.read_pe_metadata_toggle = QCheckBox("Read .exe version metadata (ProductName, Version, etc.)")
        self.read_pe_metadata_toggle.setChecked(settings.get("read_exe_metadata_enabled", False))
        self.read_pe_metadata_toggle.setToolTip(
            "OFF (default): fast, name-based identification only.\n"
            "ON: opens each .exe to read its embedded version resource --\n"
            "more accurate, slower over a large collection."
        )
        arch_form.addRow(self.read_pe_metadata_toggle)

        self.inspect_archives_toggle = QCheckBox("Inspect archive contents (.zip/.rar/.7z/.iso)")
        self.inspect_archives_toggle.setChecked(settings.get("inspect_archive_contents_enabled", False))
        self.inspect_archives_toggle.setToolTip(
            "OFF (default): archives are identified by filename only.\n"
            "ON: lists archive contents, and extracts when ambiguous, to find\n"
            "and identify the installer inside -- slower, some extraction risk."
        )
        arch_form.addRow(self.inspect_archives_toggle)

        self.escalate_ambiguous = QCheckBox("Fully extract archives when ambiguous")
        self.escalate_ambiguous.setChecked(settings.get("archive_ambiguity_escalates_to_extraction", True))
        arch_form.addRow(self.escalate_ambiguous)

        self.max_extract_mb = QSpinBox()
        self.max_extract_mb.setRange(1, 100_000)
        self.max_extract_mb.setValue(settings.get("archive_max_full_extract_mb", 2048))
        arch_form.addRow("Max archive size to fully extract (MB)", self.max_extract_mb)

        self.purge_after = QCheckBox("Purge extracted files after inspection")
        self.purge_after.setChecked(settings.get("archive_purge_scratch_after_use", True))
        arch_form.addRow(self.purge_after)
        tabs.addTab(arch_tab, "Archives")

        # Scan behavior
        scan_tab = QWidget()
        scan_form = QFormLayout(scan_tab)
        self.incremental = QCheckBox("Incremental scan (skip unchanged folders)")
        self.incremental.setChecked(settings.get("incremental_scan_by_default", True))
        scan_form.addRow(self.incremental)

        self.follow_symlinks = QCheckBox("Follow symlinks while scanning")
        self.follow_symlinks.setChecked(settings.get("scan_follow_symlinks", False))
        scan_form.addRow(self.follow_symlinks)
        tabs.addTab(scan_tab, "Scan")

        # Scraper
        scraper_tab = QWidget()
        scraper_form = QFormLayout(scraper_tab)
        self.scraper_manifest_url = QLineEdit(settings.get("scraper_winget_manifest_url", ""))
        scraper_form.addRow("Winget manifest URL (svrooij index.v2.json)", self.scraper_manifest_url)

        self.scraper_staleness_hours = QSpinBox()
        self.scraper_staleness_hours.setRange(1, 24 * 30)
        self.scraper_staleness_hours.setValue(int(settings.get("scraper_manifest_staleness_hours", 4)))
        scraper_form.addRow("Re-download manifest if cache is older than (hours)", self.scraper_staleness_hours)

        self.scraper_auto_rename = QCheckBox("Auto-rename an app to the manifest's display name on a match")
        self.scraper_auto_rename.setChecked(bool(settings.get("scraper_auto_rename", False)))
        scraper_form.addRow(self.scraper_auto_rename)
        scraper_form.addRow(QLabel(
            "Off by default -- a manifest match still fills in publisher/description/\n"
            "tags/version, it just won't overwrite an already-resolved app name.\n"
            "Locked names (name_locked) are never renamed either way."
        ))

        self.scraper_auto_enrich_statuses = QPlainTextEdit(
            "\n".join(settings.get("scraper_auto_enrich_statuses", ["resolved", "verified"]))
        )
        self.scraper_auto_enrich_statuses.setMaximumHeight(60)
        scraper_form.addRow("App statuses eligible for \"Scrape all\" (one per line)", self.scraper_auto_enrich_statuses)

        self.scraper_choco_max_results = QSpinBox()
        self.scraper_choco_max_results.setRange(1, 20)
        self.scraper_choco_max_results.setValue(int(settings.get("scraper_choco_default_max_results", 5)))
        scraper_form.addRow("Default Chocolatey search result count", self.scraper_choco_max_results)
        tabs.addTab(scraper_tab, "Scraper")

        # Appearance
        appearance_tab = QWidget()
        appearance_form = QFormLayout(appearance_tab)
        self.ui_scale = QDoubleSpinBox()
        self.ui_scale.setRange(0.5, 3.0)
        self.ui_scale.setSingleStep(0.1)
        self.ui_scale.setValue(settings.get("ui_scale_multiplier", 1.0))
        appearance_form.addRow("UI scale multiplier", self.ui_scale)
        appearance_form.addRow(QLabel(
            "Scales all fonts and widget sizes. Takes effect after restarting\n"
            "the app (Qt reads this before the window is created)."
        ))
        tabs.addTab(appearance_tab, "Appearance")

        # Keywords
        kw_tab = QWidget()
        kw_layout = QVBoxLayout(kw_tab)
        kw_layout.addWidget(QLabel(
            "<b>Keywords</b> are exact‑match (case‑insensitive) words or phrases.<br>"
            "They are applied as <u>whole‑token</u> matches (i.e., they must be separate words).<br>"
            "They are used to strip generic words, classify folders, extract edition/language tokens."
        ))
        self.container_keywords = QPlainTextEdit("\n".join(settings.get("container_folder_keywords", [])))
        kw_layout.addWidget(self.container_keywords)
        kw_layout.addWidget(QLabel("Noise folder keywords (one per line):"))
        self.noise_keywords = QPlainTextEdit("\n".join(settings.get("noise_folder_keywords", [])))
        kw_layout.addWidget(self.noise_keywords)

        kw_layout.addWidget(QLabel(
            "Noise keywords excluded ONLY when the folder name is short (see max\n"
            "length below) -- e.g. a folder literally named \"Crack\" or \"Skins\" is\n"
            "skipped entirely, but a long release name that merely mentions one of\n"
            "these words among much more content (\"Adobe.Captivate...Keygen.Only-\n"
            "ViRiLiTY\") is still the real install folder and is kept:"
        ))
        self.noise_short_only_keywords = QPlainTextEdit("\n".join(settings.get("noise_short_only_keywords", [])))
        self.noise_short_only_keywords.setMaximumHeight(70)
        kw_layout.addWidget(self.noise_short_only_keywords)

        short_len_row = QHBoxLayout()
        short_len_row.addWidget(QLabel("Max folder name length still counted as \"short\":"))
        self.noise_short_only_max_len = QSpinBox()
        self.noise_short_only_max_len.setRange(1, 200)
        self.noise_short_only_max_len.setValue(int(settings.get("noise_short_only_max_len", 25)))
        short_len_row.addWidget(self.noise_short_only_max_len)
        short_len_row.addStretch()
        kw_layout.addLayout(short_len_row)
        tabs.addTab(kw_tab, "Keywords")

        # Filters
        filters_tab = QWidget()
        filters_layout = QVBoxLayout(filters_tab)
        filters_layout.addWidget(QLabel(
            "<b>Filters</b> are <u>regular expressions</u> (regex) that match substrings.<br>"
            "They are applied in the order shown below during the name‑cleaning pipeline.<br>"
            "Each step is documented in the pipeline description (see below)."
        ))

        filters_layout.addWidget(QLabel("1. Bracket-content patterns (regex):"))
        self.bracket_content_patterns = QPlainTextEdit("\n".join(settings.get("bracket_content_patterns", [])))
        self.bracket_content_patterns.setMaximumHeight(60)
        filters_layout.addWidget(self.bracket_content_patterns)

        filters_layout.addWidget(QLabel("2. Website/domain tag patterns (regex):"))
        self.website_patterns = QPlainTextEdit("\n".join(settings.get("website_tag_patterns", [])))
        self.website_patterns.setMaximumHeight(70)
        filters_layout.addWidget(self.website_patterns)

        filters_layout.addWidget(QLabel("3. Release-tag / scene-flag patterns (regex):"))
        self.release_patterns = QPlainTextEdit("\n".join(settings.get("release_tag_patterns", [])))
        self.release_patterns.setMaximumHeight(70)
        filters_layout.addWidget(self.release_patterns)

        filters_layout.addWidget(QLabel("4. Ignore words in FILE names (whole word, one per line):"))
        self.ignore_filename_words = QPlainTextEdit("\n".join(settings.get("ignore_filename_words", [])))
        self.ignore_filename_words.setMaximumHeight(70)
        filters_layout.addWidget(self.ignore_filename_words)

        filters_layout.addWidget(QLabel("5. Ignore FOLDER names (whole name, one per line):"))
        self.ignore_folder_names = QPlainTextEdit("\n".join(settings.get("ignore_folder_names", [])))
        self.ignore_folder_names.setMaximumHeight(70)
        filters_layout.addWidget(self.ignore_folder_names)

        filters_layout.addWidget(QLabel("5b. Ignore FOLDER name patterns (regex, fullmatch):"))
        self.ignore_folder_name_patterns = QPlainTextEdit("\n".join(settings.get("ignore_folder_name_patterns", [])))
        self.ignore_folder_name_patterns.setMaximumHeight(60)
        filters_layout.addWidget(self.ignore_folder_name_patterns)

        filters_layout.addWidget(QLabel("5c. Ignore FILE name patterns (regex, fullmatch):"))
        self.ignore_filename_patterns = QPlainTextEdit("\n".join(settings.get("ignore_filename_patterns", [])))
        self.ignore_filename_patterns.setMaximumHeight(60)
        filters_layout.addWidget(self.ignore_filename_patterns)

        filters_layout.addWidget(QLabel("6. Catalog/subcatalog display-name conversions (RAW=Display):"))
        aliases = settings.get("folder_name_aliases", {})
        self.folder_aliases = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in aliases.items()))
        self.folder_aliases.setMaximumHeight(70)
        filters_layout.addWidget(self.folder_aliases)

        filters_layout.addWidget(QLabel("7. Edition keywords (whole word, one per line):"))
        self.edition_keywords = QPlainTextEdit("\n".join(settings.get("edition_keywords", [])))
        self.edition_keywords.setMaximumHeight(70)
        filters_layout.addWidget(self.edition_keywords)

        filters_layout.addWidget(QLabel("8. Language keywords (word=Label, one per line):"))
        lang = settings.get("language_keywords", {})
        self.language_keywords = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in lang.items()))
        self.language_keywords.setMaximumHeight(60)
        filters_layout.addWidget(self.language_keywords)

        filters_layout.addWidget(QLabel("9. Build-number pattern (regex, one capture group):"))
        self.build_number_pattern = QLineEdit(settings.get("build_number_pattern", ""))
        filters_layout.addWidget(self.build_number_pattern)

        filters_scroll = QScrollArea()
        filters_scroll.setWidgetResizable(True)
        filters_scroll.setWidget(filters_tab)
        tabs.addTab(filters_scroll, "Filters")

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        # ---- Advanced tab ----
        adv_tab = QWidget()
        adv_layout = QVBoxLayout(adv_tab)

        # App name synonyms (pattern=replacement)
        syn_group = QGroupBox("App name synonyms (pattern=replacement)")
        syn_layout = QVBoxLayout(syn_group)
        self.synonyms_edit = QPlainTextEdit()
        self._populate_synonyms(self.synonyms_edit, settings.get("app_name_synonyms", []))
        syn_layout.addWidget(self.synonyms_edit)
        adv_layout.addWidget(syn_group)

        # Category rules (pattern=value)
        cat_group = QGroupBox("Category rules (pattern=value)")
        cat_layout = QVBoxLayout(cat_group)
        self.category_rules_edit = QPlainTextEdit()
        self._populate_rules(self.category_rules_edit, settings.get("category_rules", []))
        cat_layout.addWidget(self.category_rules_edit)
        adv_layout.addWidget(cat_group)

        # Subcategory rules (pattern=value)
        subcat_group = QGroupBox("Subcategory rules (pattern=value)")
        subcat_layout = QVBoxLayout(subcat_group)
        self.subcategory_rules_edit = QPlainTextEdit()
        self._populate_rules(self.subcategory_rules_edit, settings.get("subcategory_rules", []))
        subcat_layout.addWidget(self.subcategory_rules_edit)
        adv_layout.addWidget(subcat_group)

        # Winutil URL
        url_layout = QFormLayout()
        self.winutil_url_edit = QLineEdit(settings.get("scraper_winutil_apps_url", ""))
        url_layout.addRow("Winutil apps URL", self.winutil_url_edit)
        adv_layout.addLayout(url_layout)

        # Portable indicator words (one per line)
        port_group = QGroupBox("Portable indicator words (one per line)")
        port_layout = QVBoxLayout(port_group)
        self.portable_words_edit = QPlainTextEdit()
        self._populate_list(self.portable_words_edit, settings.get("portable_indicator_words", []))
        port_layout.addWidget(self.portable_words_edit)
        adv_layout.addWidget(port_group)

        # Allow bare trailing number as version
        self.bare_number_check = QCheckBox("Allow bare trailing number as version")
        self.bare_number_check.setChecked(settings.get("allow_bare_trailing_number_as_version", True))
        adv_layout.addWidget(self.bare_number_check)

        adv_layout.addStretch()
        tabs.addTab(adv_tab, "Advanced")

        # Monitor
        mon_tab = QWidget()
        mon_layout = QVBoxLayout(mon_tab)

        mon_layout.addWidget(QLabel(
            "<b>Monitor job</b> — a manually-triggered pass over the folders "
            "below. New .exe / .msi / archive files are identified, matched to "
            "an existing app (or flagged for creation of a new one), and moved "
            "into the organized structure. Already-compressed inputs are moved "
            "as-is; everything else is archived first. Nothing on disk changes "
            "until you review the dry-run plan and click <b>Execute</b>."
        ))

        mon_layout.addWidget(QLabel("Folders to monitor:"))
        self.monitor_folders_table = QTableWidget(0, 1)
        self.monitor_folders_table.setHorizontalHeaderLabels(["Folder"])
        self.monitor_folders_table.horizontalHeader().setStretchLastSection(True)
        self.monitor_folders_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.monitor_folders_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.monitor_folders_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.monitor_folders_table.setMinimumHeight(120)
        for f in settings.get("monitor_folders", []):
            self._append_monitor_folder_row(f)
        mon_layout.addWidget(self.monitor_folders_table)

        mon_folder_btns = QHBoxLayout()
        mon_add_btn = QPushButton("Add folder…")
        mon_add_btn.clicked.connect(self._add_monitor_folder)
        mon_del_btn = QPushButton("Delete selected")
        mon_del_btn.clicked.connect(self._remove_monitor_folders)
        mon_folder_btns.addWidget(mon_add_btn)
        mon_folder_btns.addWidget(mon_del_btn)
        mon_folder_btns.addStretch()
        mon_layout.addLayout(mon_folder_btns)

        mon_form = QFormLayout()
        self.monitor_extensions = QPlainTextEdit(
            " ".join(settings.get("monitor_extensions", []))
        )
        self.monitor_extensions.setMaximumHeight(40)
        self.monitor_extensions.setToolTip("Space-separated, leading dot required")
        mon_form.addRow("Extensions to watch:", self.monitor_extensions)

        self.monitor_already_compressed = QPlainTextEdit(
            " ".join(settings.get("monitor_already_compressed_extensions", []))
        )
        self.monitor_already_compressed.setMaximumHeight(40)
        self.monitor_already_compressed.setToolTip(
            "Files with these extensions are moved as-is, not re-compressed"
        )
        mon_form.addRow("Already-compressed extensions:", self.monitor_already_compressed)

        self.monitor_skip_partial = QPlainTextEdit(
            " ".join(settings.get("monitor_skip_partial_extensions", []))
        )
        self.monitor_skip_partial.setMaximumHeight(40)
        self.monitor_skip_partial.setToolTip(
            "Files ending in these are ignored entirely (mid-download markers)"
        )
        mon_form.addRow("Skip partial-download suffixes:", self.monitor_skip_partial)

        self.monitor_min_size = QSpinBox()
        self.monitor_min_size.setRange(0, 100_000)
        self.monitor_min_size.setValue(int(settings.get("monitor_min_size_mb", 1)))
        self.monitor_min_size.setSuffix(" MB")
        mon_form.addRow("Minimum file size:", self.monitor_min_size)

        self.monitor_settle = QSpinBox()
        self.monitor_settle.setRange(0, 300)
        self.monitor_settle.setValue(int(settings.get("monitor_settle_seconds", 3)))
        self.monitor_settle.setSuffix(" seconds")
        self.monitor_settle.setToolTip(
            "A file must be unchanged for this long before it's processed"
        )
        mon_form.addRow("Settle delay:", self.monitor_settle)

        self.monitor_format = QComboBox()
        self.monitor_format.addItems(["7z", "zip", "rar"])
        fmt = settings.get("monitor_archive_format", "7z")
        idx = self.monitor_format.findText(fmt)
        if idx >= 0:
            self.monitor_format.setCurrentIndex(idx)
        mon_form.addRow("Default archive format:", self.monitor_format)

        self.monitor_move_mode = QComboBox()
        self.monitor_move_mode.addItems(["move", "copy"])
        self.monitor_move_mode.setCurrentText(settings.get("monitor_move_mode", "move"))
        mon_form.addRow("Default mode:", self.monitor_move_mode)

        self.monitor_auto_scrape = QCheckBox(
            "Refresh Winget metadata for an app right after a file is attached"
        )
        self.monitor_auto_scrape.setChecked(
            bool(settings.get("monitor_auto_scrape_on_attach", True))
        )
        mon_form.addRow(self.monitor_auto_scrape)

        mon_layout.addLayout(mon_form)
        mon_layout.addStretch()
        tabs.addTab(mon_tab, "Monitor")
        # Long descriptive labels have no word wrap by default; their
        # single-line natural width would otherwise force the whole tab
        # widget (and therefore the dialog) to expand well past the
        # screen. Wrap anything long enough to be prose rather than a
        # field label, and cap the dialog's opening size so a huge
        # display doesn't make it fill the entire screen.
        for lbl in self.findChildren(QLabel):
            text = lbl.text() or ""
            if len(text) > 60 and " " in text and "://" not in text:
                lbl.setWordWrap(True)
        self.resize(700, 620)
        self.setMaximumWidth(1100)

    def _populate_synonyms(self, text_edit, data):
        lines = [f"{item['pattern']}={item['replacement']}" for item in data if 'pattern' in item]
        text_edit.setPlainText("\n".join(lines))

    def _populate_rules(self, text_edit, data):
        lines = [f"{item['pattern']}={item['value']}" for item in data if 'pattern' in item]
        text_edit.setPlainText("\n".join(lines))

    def _populate_list(self, text_edit, data):
        text_edit.setPlainText("\n".join(data) if isinstance(data, list) else "")

    def _append_monitor_folder_row(self, folder: str):
        row = self.monitor_folders_table.rowCount()
        self.monitor_folders_table.insertRow(row)
        self.monitor_folders_table.setItem(row, 0, QTableWidgetItem(folder))

    def _current_monitor_folders(self) -> list[str]:
        out = []
        for r in range(self.monitor_folders_table.rowCount()):
            item = self.monitor_folders_table.item(r, 0)
            if item and item.text().strip():
                out.append(item.text().strip())
        return out

    def _add_monitor_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to monitor")
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
        if not rows:
            return
        for r in rows:
            self.monitor_folders_table.removeRow(r)
    
    def _save(self):
        self.db.set_setting("confidence_auto_accept", self.auto_accept.value(), bump_version=True, note="settings dialog save")
        self.db.set_setting("confidence_needs_review", self.needs_review.value(), bump_version=False)
        self.db.set_setting("fuzzy_match_threshold", self.fuzzy_threshold.value(), bump_version=False)
        self.db.set_setting("prefer_pe_version_over_parsed", self.prefer_pe.isChecked(), bump_version=False)
        self.db.set_setting("read_exe_metadata_enabled", self.read_pe_metadata_toggle.isChecked(), bump_version=False)
        self.db.set_setting("inspect_archive_contents_enabled", self.inspect_archives_toggle.isChecked(), bump_version=False)
        self.db.set_setting("archive_ambiguity_escalates_to_extraction", self.escalate_ambiguous.isChecked(), bump_version=False)
        self.db.set_setting("archive_max_full_extract_mb", self.max_extract_mb.value(), bump_version=False)
        self.db.set_setting("archive_purge_scratch_after_use", self.purge_after.isChecked(), bump_version=False)
        self.db.set_setting("incremental_scan_by_default", self.incremental.isChecked(), bump_version=False)
        self.db.set_setting("scan_follow_symlinks", self.follow_symlinks.isChecked(), bump_version=False)

        self.db.set_setting("scraper_winget_manifest_url", self.scraper_manifest_url.text().strip(), bump_version=False)
        self.db.set_setting("scraper_manifest_staleness_hours", self.scraper_staleness_hours.value(), bump_version=False)
        self.db.set_setting("scraper_auto_rename", self.scraper_auto_rename.isChecked(), bump_version=False)
        self.db.set_setting("scraper_auto_enrich_statuses", [l.strip() for l in self.scraper_auto_enrich_statuses.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("scraper_choco_default_max_results", self.scraper_choco_max_results.value(), bump_version=False)
        self.db.set_setting("ui_scale_multiplier", self.ui_scale.value(), bump_version=False)

        self.db.set_setting("container_folder_keywords", [l.strip() for l in self.container_keywords.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("noise_folder_keywords", [l.strip() for l in self.noise_keywords.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("noise_short_only_keywords", [l.strip() for l in self.noise_short_only_keywords.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("noise_short_only_max_len", self.noise_short_only_max_len.value(), bump_version=False)

        self.db.set_setting("bracket_content_patterns", [l.strip() for l in self.bracket_content_patterns.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("website_tag_patterns", [l.strip() for l in self.website_patterns.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("release_tag_patterns", [l.strip() for l in self.release_patterns.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("ignore_filename_words", [l.strip() for l in self.ignore_filename_words.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("ignore_folder_names", [l.strip() for l in self.ignore_folder_names.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("ignore_folder_name_patterns", [l.strip() for l in self.ignore_folder_name_patterns.toPlainText().splitlines() if l.strip()], bump_version=False)
        self.db.set_setting("ignore_filename_patterns", [l.strip() for l in self.ignore_filename_patterns.toPlainText().splitlines() if l.strip()], bump_version=False)

        alias_dict = {}
        for line in self.folder_aliases.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    alias_dict[k.strip().lower()] = v.strip()
        self.db.set_setting("folder_name_aliases", alias_dict, bump_version=False)

        self.db.set_setting("edition_keywords", [l.strip() for l in self.edition_keywords.toPlainText().splitlines() if l.strip()], bump_version=False)
        lang_dict = {}
        for line in self.language_keywords.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    lang_dict[k.strip().lower()] = v.strip()
        self.db.set_setting("language_keywords", lang_dict, bump_version=False)
        self.db.set_setting("build_number_pattern", self.build_number_pattern.text().strip(), bump_version=False)

        # Advanced settings
        synonyms = []
        for line in self.synonyms_edit.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    synonyms.append({"pattern": k.strip(), "replacement": v.strip()})
        self.db.set_setting("app_name_synonyms", synonyms, bump_version=False)

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

        self.db.set_setting("scraper_winutil_apps_url", self.winutil_url_edit.text().strip(), bump_version=False)

        portable_words = [line.strip() for line in self.portable_words_edit.toPlainText().splitlines() if line.strip()]
        self.db.set_setting("portable_indicator_words", portable_words, bump_version=False)

        self.db.set_setting("allow_bare_trailing_number_as_version", self.bare_number_check.isChecked(), bump_version=False)

        # Monitor
        self.db.set_setting(
            "monitor_folders",
            self._current_monitor_folders(),
            bump_version=False)
        self.db.set_setting(
            "monitor_extensions",
            self.monitor_extensions.toPlainText().split(),
            bump_version=False)
        self.db.set_setting(
            "monitor_already_compressed_extensions",
            self.monitor_already_compressed.toPlainText().split(),
            bump_version=False)
        self.db.set_setting(
            "monitor_skip_partial_extensions",
            self.monitor_skip_partial.toPlainText().split(),
            bump_version=False)
        self.db.set_setting("monitor_min_size_mb", self.monitor_min_size.value(), bump_version=False)
        self.db.set_setting("monitor_settle_seconds", self.monitor_settle.value(), bump_version=False)
        self.db.set_setting("monitor_archive_format", self.monitor_format.currentText(), bump_version=False)
        self.db.set_setting("monitor_move_mode", self.monitor_move_mode.currentText(), bump_version=False)
        self.db.set_setting("monitor_auto_scrape_on_attach", self.monitor_auto_scrape.isChecked(), bump_version=False)

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

        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()
        self.refresh_all()

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        pick_root_action = QAction("Add scan root…", self)
        pick_root_action.triggered.connect(self._pick_and_scan_root)
        toolbar.addAction(pick_root_action)

        scan_roots_action = QAction("Scan roots…", self)
        scan_roots_action.setToolTip(
            "View previously-added scan roots and re-scan any of them "
            "(picks up new/changed files, skips anything unchanged)"
        )
        scan_roots_action.triggered.connect(self._show_scan_roots)
        toolbar.addAction(scan_roots_action)

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
        self.catalog_tree.addTopLevelItem(self._make_catalog_node("(all catalogs)"))
        self.catalog_tree.setCurrentItem(self.catalog_tree.topLevelItem(0))
        self.catalog_tree.currentItemChanged.connect(lambda *_: self._apply_filters())
        self.catalog_tree.setMaximumWidth(240)
        splitter.addWidget(self.catalog_tree)

        self.model = AppsTableModel(self.db)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.selectionModel().currentRowChanged.connect(self._on_selection_changed)
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
        """Capture current sort column/order and selected app id."""
        return {
            'sort_col': self.table.horizontalHeader().sortIndicatorSection(),
            'sort_order': self.table.horizontalHeader().sortIndicatorOrder(),
            'app_id': self.detail_panel.current_app_id,
        }

    def _restore_state(self, state: dict):
        """Restore sort and selection from a captured state."""
        if state['sort_col'] >= 0:
            self.table.sortByColumn(state['sort_col'], state['sort_order'])
        if state['app_id'] is not None:
            for row in range(self.model.rowCount()):
                if self.model.app_id_at(row) == state['app_id']:
                    self.table.selectRow(row)
                    self.detail_panel.load_app(state['app_id'])
                    break

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
        if getattr(self.model, "search_text", "") or self.model.catalog_filter or \
           getattr(self.model, "subcatalog_filter", None) or self.model.status_filter or \
           getattr(self.model, "scrape_status_filter", None):
            self.search_box.clear()
            self.catalog_tree.setCurrentItem(self.catalog_tree.topLevelItem(0))  # "(all catalogs)"
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

    def _current_catalog_selection(self) -> tuple[Optional[str], Optional[str]]:
        """(catalog, subcatalog) for whatever's selected in the left tree --
        both None for "(all catalogs)", subcatalog None for a catalog-level
        node, both set for a subcatalog leaf."""
        item = self.catalog_tree.currentItem()
        if item is None:
            return None, None
        data = item.data(0, Qt.UserRole) or {}
        return data.get("catalog"), data.get("subcatalog")

    def refresh_all(self, preserve_state: bool = True):
        """
        Refresh the entire UI (table, catalog list, column visibility).
        If preserve_state is True, restore sort order and selected app.
        """
        state = self._capture_state() if preserve_state else None

        # Update column visibility
        self._apply_column_visibility()

        # Update catalog/subcatalog tree, preserving the selected
        # (catalog, subcatalog) pair rather than raw text -- a subcatalog
        # name could otherwise collide with an unrelated catalog's name.
        prev_catalog, prev_subcatalog = self._current_catalog_selection()
        self.catalog_tree.blockSignals(True)
        self.catalog_tree.clear()
        all_item = self._make_catalog_node("(all catalogs)")
        self.catalog_tree.addTopLevelItem(all_item)
        selected_item = all_item if prev_catalog is None else None
        for cat, subs in self.model.catalog_subcatalog_tree().items():
            cat_item = self._make_catalog_node(cat, catalog=cat)
            self.catalog_tree.addTopLevelItem(cat_item)
            if prev_catalog == cat and prev_subcatalog is None:
                selected_item = cat_item
            for sub in sorted(set(subs)):
                sub_item = self._make_catalog_node(sub, catalog=cat, subcatalog=sub)
                cat_item.addChild(sub_item)
                if prev_catalog == cat and prev_subcatalog == sub:
                    selected_item = sub_item
            cat_item.setExpanded(True)
        self.catalog_tree.setCurrentItem(selected_item or all_item)
        self.catalog_tree.blockSignals(False)

        # Apply filters – this will refresh the model
        self._apply_filters()

        # Restore state if requested
        if state:
            self._restore_state(state)

    def _apply_filters(self):
        self.model.search_text = self.search_box.text().strip()
        catalog, subcatalog = self._current_catalog_selection()
        self.model.catalog_filter = catalog
        self.model.subcatalog_filter = subcatalog
        status = self.status_combo.currentText()
        self.model.status_filter = None if status == "(all)" else status
        scrape_status = self.scrape_status_combo.currentText()
        self.model.scrape_status_filter = None if scrape_status == "(all)" else scrape_status
        self.model.refresh()

    def _on_selection_changed(self, current, previous):
        if not current.isValid():
            self.detail_panel.clear()
            return
        app_id = self.model.app_id_at(current.row())
        if app_id is not None:
            self.detail_panel.load_app(app_id)

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

    def delete_selected_apps(self):
        app_ids = self._selected_app_ids()
        if not app_ids:
            return
        if QMessageBox.question(self, "Confirm Delete",
                                f"Delete {len(app_ids)} app(s) and their versions?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            for app_id in app_ids:
                self.db.delete_app(app_id)
            self.refresh_all()   # replaces load_filters() + load_table()

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

    def _pick_and_scan_root(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to scan")
        if not folder:
            return
        self._run_scan_and_resolve(folder)

    def _show_scan_roots(self):
        dialog = ScanRootsDialog(self.db, parent=self)
        dialog.exec()

    def _show_column_picker(self):
        visible = set(self.db.get_setting("visible_columns", [k for k, _ in COLUMNS]))
        menu = QMenu(self)
        menu.setTitle("Columns")
        actions = {}
        for key, label in COLUMNS:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key in visible)
            actions[act] = key
        for act, key in actions.items():
            if key == "name":
                act.setEnabled(False)
        chosen = menu.exec(self.table.viewport().mapToGlobal(self.table.rect().topLeft()))
        if chosen is None:
            return
        key = actions[chosen]
        if key == "name":
            return
        if key in visible:
            visible.discard(key)
        else:
            visible.add(key)
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

    def _run_scan_and_resolve(self, root_path: str):
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A scan/resolve job is already running.")
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

    def _run_clean_library(self, root_path: str):
        """Confirms every app already in the catalog from `root_path`
        still exists on disk -- the opposite of a scan, see
        app_manager.scan_for_missing_sources()'s docstring. Never looks
        for new apps."""
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
        self.status_label.setText(f"Checking whether apps from {root_path} still exist…")
        worker.start()

    def _on_clean_library_progress(self, checked: int, total: int):
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(checked)
        self.status_label.setText(f"Checking… {checked}/{total}")

    def _on_clean_library_scanned(self, missing: list, root_path: str):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._active_worker = None
        self.status_label.setText("Ready.")
        if not missing:
            QMessageBox.information(
                self, "Clean library",
                f"Everything already in the catalog from:\n{root_path}\n\n"
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
        worker = ResolveWorker(self.db_path)
        worker.finished_ok.connect(self._on_resolve_only_finished)
        worker.failed.connect(self._on_job_failed)
        self._active_worker = worker
        self.progress_bar.setVisible(True)
        self.status_label.setText("Re-resolving all apps with current settings…")
        worker.start()

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
        self._active_worker = None
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
        self._active_worker = None
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
        self._active_worker = None
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
        self._active_worker = None
        if resolve_result is None:
            self.status_label.setText("Scan cancelled.")
        else:
            self.status_label.setText(
                f"Done. {scan_result.install_units_found} install units found, "
                f"{resolve_result.apps_created} new apps, {resolve_result.apps_updated} apps updated."
            )
        self.refresh_all()

    def _on_resolve_only_finished(self, resolve_result):
        self.progress_bar.setVisible(False)
        self._active_worker = None
        self.status_label.setText(
            f"Re-resolve done. {resolve_result.apps_created} new, "
            f"{resolve_result.apps_updated} updated, "
            f"{resolve_result.apps_skipped_locked} had locked fields preserved."
        )
        self.refresh_all()

    def _on_job_failed(self, error_message: str):
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self._active_worker = None
        self.status_label.setText("Job failed.")
        QMessageBox.critical(self, "Job failed", error_message)

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
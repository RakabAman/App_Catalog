"""
curation_dialogs.py -- Qt dialogs for the manual-curation actions in
app_curation.py (checkpoint 32):

  AddAppDialog          add an app / variant directly, without a scan root
  RepairEmptyAppsDialog fix apps left empty by the old rescan duplicate bug
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import app_curation as cur
from database import Database

INSTALLER_FILTER = ("Installer files (*.exe *.msi *.msix *.msixbundle *.zip *.rar *.7z *.iso)"
                    ";;All files (*)")


class AddAppDialog(QDialog):
    """Add an app straight from an installer file -- no scan root needed."""

    def __init__(self, db: Database, parent=None, start_dir: Optional[str] = None,
                 split_variant: Optional[dict] = None, split_app: Optional[dict] = None):
        """split_variant / split_app: use the dialog to SPLIT an existing variant out of
        its app. Everything is prefilled from the variant and stays editable; on OK the
        variant is moved into the new app (see app_curation.split_variant_with_details)."""
        super().__init__(parent)
        self.db = db
        self.split_variant = split_variant
        self.split_app = split_app or {}
        self.setWindowTitle("Split into new app" if split_variant else "Add app")
        self.setMinimumWidth(620)
        self.result_info: Optional[dict] = None
        self._folder_edited = bool(split_variant)
        self._start_dir = start_dir or ""

        root = QVBoxLayout(self)
        if split_variant:
            root.addWidget(QLabel(
                f"Move this variant out of “{self.split_app.get('name', '?')}” into a new app. "
                "Everything below is prefilled from the variant — change what you need. "
                "Changing the installer file or app folder re-points the variant."))
        else:
            root.addWidget(QLabel(
                "Add an app without scanning a folder. Pick its installer; the app folder "
                "(where the installer and its dependent files live) defaults to the installer's folder."))

        form = QFormLayout()
        self.installer_edit = QLineEdit()
        browse_file = QPushButton("Browse…")
        browse_file.clicked.connect(self._browse_installer)
        row = QHBoxLayout(); row.addWidget(self.installer_edit, 1); row.addWidget(browse_file)
        form.addRow("Installer file:", row)

        self.folder_edit = QLineEdit()
        self.folder_edit.textEdited.connect(lambda _t: setattr(self, "_folder_edited", True))
        browse_folder = QPushButton("Browse…")
        browse_folder.clicked.connect(self._browse_folder)
        row2 = QHBoxLayout(); row2.addWidget(self.folder_edit, 1); row2.addWidget(browse_folder)
        form.addRow("App folder:", row2)

        self.name_edit = QLineEdit()
        form.addRow("App name:", self.name_edit)

        self.catalog_combo = QComboBox(); self.catalog_combo.setEditable(True)
        self.sub_combo = QComboBox(); self.sub_combo.setEditable(True)
        conn = self.db.connect()
        cats = [r[0] for r in conn.execute(
            "SELECT DISTINCT catalog FROM apps WHERE catalog IS NOT NULL AND catalog != '' ORDER BY catalog COLLATE NOCASE")]
        self.catalog_combo.addItems([""] + cats)
        self.catalog_combo.setCurrentIndex(0)
        self.catalog_combo.currentTextChanged.connect(self._reload_subcatalogs)
        form.addRow("Catalog:", self.catalog_combo)
        form.addRow("Subcatalog:", self.sub_combo)

        self.version_edit = QLineEdit()
        self.edition_edit = QLineEdit()
        self.arch_combo = QComboBox(); self.arch_combo.setEditable(True)
        self.arch_combo.addItems(["", "x64", "x86", "arm64"])
        self.lang_edit = QLineEdit()
        form.addRow("Version:", self.version_edit)
        form.addRow("Edition:", self.edition_edit)
        form.addRow("Architecture:", self.arch_combo)
        form.addRow("Language:", self.lang_edit)
        root.addLayout(form)

        self.scrape_check = QCheckBox("Scrape Winget metadata after adding")
        root.addWidget(self.scrape_check)
        root.addWidget(QLabel(
            "The app is added as verified with its name, catalog and installer file locked, so "
            "later scans and re-resolves leave it alone. If this folder is inside a scan root, "
            "a later scan recognises it instead of adding a duplicate."))
        self.status = QLabel(""); self.status.setStyleSheet("color: #b00020;")
        root.addWidget(self.status)

        buttons = QHBoxLayout(); buttons.addStretch(1)
        add_btn = QPushButton("Split" if split_variant else "Add"); add_btn.setDefault(True)
        add_btn.clicked.connect(self._on_add)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(add_btn); buttons.addWidget(cancel_btn)
        root.addLayout(buttons)
        if split_variant:
            self._prefill_from_variant()

    def _prefill_from_variant(self):
        v = self.split_variant
        full = os.path.normpath(os.path.join(v["source_path"], v["file_name"])) if v.get("file_name") else ""
        self.installer_edit.setText(full)
        self.folder_edit.setText(os.path.normpath(v["source_path"]))
        s = cur.suggest_app_fields(self.db, full or v["source_path"], v["source_path"]) if full else {}
        self.name_edit.setText(s.get("name") or os.path.basename(v["source_path"].rstrip("\\/")))
        self.catalog_combo.setCurrentText(self.split_app.get("catalog") or "")
        self._reload_subcatalogs(self.split_app.get("catalog") or "")
        self.sub_combo.setCurrentText(self.split_app.get("subcatalog") or "")
        self.version_edit.setText(v.get("version") or "")
        self.edition_edit.setText(v.get("edition") or "")
        self.arch_combo.setCurrentText(v.get("architecture") or "")
        self.lang_edit.setText(v.get("language") or "")

    # -- helpers ---------------------------------------------------------
    @property
    def scrape_after(self) -> bool:
        return self.scrape_check.isChecked()

    def _reload_subcatalogs(self, catalog: str):
        self.sub_combo.clear()
        conn = self.db.connect()
        subs = [r[0] for r in conn.execute(
            "SELECT DISTINCT subcatalog FROM apps WHERE subcatalog IS NOT NULL AND subcatalog != '' "
            "AND lower(COALESCE(catalog,'')) = lower(?) ORDER BY subcatalog COLLATE NOCASE", (catalog or "",))]
        self.sub_combo.addItems([""] + subs)

    def _browse_installer(self):
        start = os.path.dirname(self.installer_edit.text()) or self._start_dir
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose installer file", start, INSTALLER_FILTER)
        if not chosen:
            return
        self.installer_edit.setText(os.path.normpath(chosen))
        if not self._folder_edited:
            self.folder_edit.setText(os.path.dirname(os.path.normpath(chosen)))
        s = cur.suggest_app_fields(self.db, chosen, self.folder_edit.text() or None)
        for edit, key in ((self.name_edit, "name"), (self.version_edit, "version"),
                          (self.edition_edit, "edition"), (self.lang_edit, "language")):
            if not edit.text().strip() and s.get(key):
                edit.setText(s[key])
        if not self.arch_combo.currentText().strip() and s.get("architecture"):
            self.arch_combo.setCurrentText(s["architecture"])

    def _browse_folder(self):
        start = self.folder_edit.text() or os.path.dirname(self.installer_edit.text()) or self._start_dir
        chosen = QFileDialog.getExistingDirectory(self, "Choose the app folder", start)
        if chosen:
            self.folder_edit.setText(os.path.normpath(chosen))
            self._folder_edited = True

    # -- accept ----------------------------------------------------------
    def _on_add(self):
        self.status.setText("")
        installer = self.installer_edit.text().strip()
        name = self.name_edit.text().strip()
        add_to = None
        existing = cur.find_existing_app(self.db, name) if name else None
        if self.split_variant is not None:
            self._on_split(name, existing)
            return
        if existing is not None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("App already exists")
            box.setText(f"An app named “{existing['name']}” is already in the catalog"
                        + (f" ({existing['catalog']})" if existing["catalog"] else "") + ".")
            box.setInformativeText("Add this installer as another variant of it, or create a separate app?")
            as_variant = box.addButton("Add as variant", QMessageBox.AcceptRole)
            separate = box.addButton("Create separate app", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is as_variant:
                add_to = existing["id"]
            elif clicked is not separate:
                return
        try:
            self.result_info = cur.add_app_manually(
                self.db, name=name, installer_path=installer,
                unit_folder=self.folder_edit.text().strip() or None,
                catalog=self.catalog_combo.currentText().strip() or None,
                subcatalog=self.sub_combo.currentText().strip() or None,
                version=self.version_edit.text().strip() or None,
                edition=self.edition_edit.text().strip() or None,
                architecture=self.arch_combo.currentText().strip() or None,
                language=self.lang_edit.text().strip() or None,
                add_to_app_id=add_to)
        except ValueError as e:
            self.status.setText(str(e).replace("\n", " "))
            return
        self.accept()


    def _on_split(self, name: str, existing):
        target = None
        if existing is not None:
            if existing["id"] == self.split_variant["app_id"]:
                self.status.setText("That is the app the variant is already in — pick a different name.")
                return
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("App already exists")
            box.setText(f"An app named “{existing['name']}” already exists"
                        + (f" ({existing['catalog']})" if existing["catalog"] else "") + ".")
            box.setInformativeText("Move the variant into that app, or create a separate new app with this name?")
            move = box.addButton("Move into that app", QMessageBox.AcceptRole)
            separate = box.addButton("Create separate app", QMessageBox.ActionRole)
            cancel = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is move:
                target = existing["id"]
            elif clicked is not separate:
                return
        try:
            self.result_info = cur.split_variant_with_details(
                self.db, self.split_variant["id"], name=name,
                installer_path=self.installer_edit.text().strip() or None,
                unit_folder=self.folder_edit.text().strip() or None,
                catalog=self.catalog_combo.currentText().strip() or None,
                subcatalog=self.sub_combo.currentText().strip() or None,
                version=self.version_edit.text(), edition=self.edition_edit.text(),
                architecture=self.arch_combo.currentText(), language=self.lang_edit.text(),
                target_app_id=target)
        except ValueError as e:
            self.status.setText(str(e).replace("\n", " "))
            return
        self.accept()


class RepairEmptyAppsDialog(QDialog):
    """Shows apps that ended up with no variants (the old rescan bug) and what
    would fix each one; the user ticks what to apply."""

    COLS = ["Apply", "Empty app (kept)", "Catalog", "Action", "Takes the variants of", "Variants", "Match"]

    def __init__(self, db: Database, items: list, parent=None):
        super().__init__(parent)
        self.db = db
        self.items = items
        self.applied: Optional[dict] = None
        self.setWindowTitle("Repair empty apps")
        self.resize(900, 460)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "These apps have no variants. Usually a rescan created a duplicate holding the variants and "
            "left the app you renamed / scraped empty. “merge” moves the duplicate's variants back into "
            "the empty app (it keeps its name and scraped data). “delete” removes an empty app that "
            "has nothing worth keeping. A backup of the catalog is saved first."))
        self.table = QTableWidget(len(items), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)       # every column resizable
        hh.setStretchLastSection(True)
        for col, w in enumerate([60, 230, 130, 80, 230, 70, 60]):
            self.table.setColumnWidth(col, w)
        for r, it in enumerate(items):
            check = QTableWidgetItem()
            actionable = it["action"] in ("merge", "delete")
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if actionable else Qt.NoItemFlags)
            default_on = (it["action"] == "delete" and not it["has_manual"]) or \
                         (it["action"] == "merge" and it["score"] >= 80)
            check.setCheckState(Qt.Checked if (actionable and default_on) else Qt.Unchecked)
            self.table.setItem(r, 0, check)
            vals = [it["empty_name"] + ("  ★" if it["has_manual"] else ""), it["catalog"] or "",
                    it["action"], it["sibling_name"] or "", str(it["sibling_variants"] or ""),
                    f"{it['score']}%" if it["score"] else ""]
            for c, v in enumerate(vals, start=1):
                cell = QTableWidgetItem(v); cell.setToolTip(v); self.table.setItem(r, c, cell)
        root.addWidget(self.table, 1)
        root.addWidget(QLabel("★ = has manual or scraped data (locked name, verified, Winget id, description …). "
                              "“review” = nothing to merge it with; left alone."))

        row = QHBoxLayout()
        all_btn = QPushButton("Select all"); all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select none"); none_btn.clicked.connect(lambda: self._set_all(False))
        row.addWidget(all_btn); row.addWidget(none_btn); row.addStretch(1)
        apply_btn = QPushButton("Apply selected"); apply_btn.setDefault(True); apply_btn.clicked.connect(self._apply)
        close_btn = QPushButton("Close"); close_btn.clicked.connect(self.reject)
        row.addWidget(apply_btn); row.addWidget(close_btn)
        root.addLayout(row)

    def _set_all(self, on: bool):
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _apply(self):
        chosen = [self.items[r] for r in range(self.table.rowCount())
                  if self.table.item(r, 0).checkState() == Qt.Checked]
        if not chosen:
            QMessageBox.information(self, "Repair empty apps", "Nothing is ticked.")
            return
        cur.create_backup(self.db.path, "before-repair", keep=10)
        self.applied = cur.apply_empty_app_repairs(self.db, chosen)
        self.accept()

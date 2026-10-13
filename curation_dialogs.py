"""
curation_dialogs.py -- Qt dialogs for the manual-curation actions in
app_curation.py:

  AddAppDialog          one dialog, three modes
                          * add         a new app from an installer (no scan root)
                          * split       move an existing variant into a new app
                          * add variant add another installer to an existing app
  RepairEmptyAppsDialog fix apps left empty by the old rescan duplicate bug

Layout rules (so the dialogs behave on small screens): every long label wraps
(an unwrapped label forces the whole dialog to its text width), dialogs are
sized to at most ~90 % of the parent window / screen, can be resized freely, and
tables scroll both ways.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import app_curation as cur
import app_sidecars as sidecars
from database import Database

INSTALLER_FILTER = ("Installer files (*.exe *.msi *.msix *.msixbundle *.zip *.rar *.7z *.iso)"
                    ";;All files (*)")


def _fit_dialog(dlg: QDialog, parent, max_w: int, max_h: int, min_w: int = 420, min_h: int = 260):
    """Size `dlg` for its content but never beyond ~90 % of the parent window (or
    the screen). The user can still resize it; a size grip is shown."""
    try:
        screen = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
    except Exception:
        screen = QApplication.primaryScreen().availableGeometry()
    limit_w, limit_h = int(screen.width() * 0.9), int(screen.height() * 0.9)
    if parent is not None and parent.width() > 100:
        limit_w = min(limit_w, int(parent.width() * 0.9))
        limit_h = min(limit_h, int(parent.height() * 0.9))
    dlg.setMinimumSize(min(min_w, limit_w), min(min_h, limit_h))
    dlg.resize(min(max_w, limit_w), min(max_h, limit_h))
    dlg.setSizeGripEnabled(True)


def _wrapped(text: str, subtle: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)     # never dictates the dialog width
    if subtle:
        lab.setStyleSheet("color: palette(mid);")
    return lab


class AddAppDialog(QDialog):
    """Add an app / split a variant out / add a variant to an app -- same dialog,
    same fields, so the three actions look and behave alike."""

    def __init__(self, db: Database, parent=None, start_dir: Optional[str] = None,
                 split_variant: Optional[dict] = None, split_app: Optional[dict] = None,
                 initial_installer: Optional[str] = None, initial_folder: Optional[str] = None,
                 add_to_app: Optional[dict] = None, split_variants: Optional[list] = None):
        """
        split_variant / split_app  SPLIT mode: prefilled from the variant, moved into a new app.
        add_to_app                 ADD-VARIANT mode: the app is fixed (name / category read-only).
        initial_installer / initial_folder   e.g. something dropped onto the main window.
        """
        super().__init__(parent)
        self.db = db
        # SPLIT several variants at once (>= 2): one new app for all, or one app each
        self.split_variants = split_variants if (split_variants and len(split_variants) > 1) else None
        if self.split_variants:
            split_variant = None
        self.split_variant = split_variant
        self.split_app = split_app or {}
        self.add_to_app = add_to_app
        self.result_info: Optional[dict] = None
        self._folder_edited = bool(split_variant)
        self._start_dir = start_dir or ""

        if self.split_variants:
            self.setWindowTitle(f"Split {len(self.split_variants)} variants into new app(s)")
            action = "Split"
        elif split_variant:
            self.setWindowTitle("Split into new app")
            action = "Split"
        elif add_to_app:
            self.setWindowTitle(f"Add variant — {add_to_app.get('name', '')}")
            action = "Add variant"
        else:
            self.setWindowTitle("Add app")
            action = "Add"

        root = QVBoxLayout(self)
        root.setSpacing(10)
        # The body scrolls if the window is tiny; the buttons stay fixed at the bottom.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self._body = body
        if self.split_variants:
            body.addWidget(_wrapped(
                f"Move the {len(self.split_variants)} selected variants out of "
                f"“{self.split_app.get('name', '?')}”. They keep their own version, edition and file; "
                "choose how to group them.", subtle=True))
            self.rb_one = QRadioButton(f"All {len(self.split_variants)} variants into ONE new app")
            self.rb_each = QRadioButton("Each variant into its OWN new app (name suggested from its file)")
            self.rb_one.setChecked(True)
            body.addWidget(self.rb_one)
            body.addWidget(self.rb_each)
        elif split_variant:
            body.addWidget(_wrapped(
                f"Move this variant out of “{self.split_app.get('name', '?')}” into a new app. "
                "Everything is prefilled from the variant — change what you need. Changing the "
                "installer file or app folder re-points the variant.", subtle=True))
        elif add_to_app:
            body.addWidget(_wrapped(
                f"Add another installer to “{add_to_app.get('name', '')}”. "
                "The app's name and category stay as they are.", subtle=True))
        else:
            body.addWidget(_wrapped(
                "Add an app without scanning a folder. Pick its installer; the app folder (where the "
                "installer and its dependent files live) defaults to the installer's folder.", subtle=True))

        # ---- Files ---------------------------------------------------------------------------
        files_box = QGroupBox("Files")
        fg = QGridLayout(files_box)
        fg.setColumnStretch(1, 1)
        self.installer_edit = QLineEdit()
        browse_file = QPushButton("Browse…")
        browse_file.clicked.connect(self._browse_installer)
        fg.addWidget(QLabel("Installer file"), 0, 0)
        fg.addWidget(self.installer_edit, 0, 1)
        fg.addWidget(browse_file, 0, 2)
        self.folder_edit = QLineEdit()
        self.folder_edit.setToolTip("The folder holding the installer and the files it depends on.")
        self.folder_edit.textEdited.connect(lambda _t: setattr(self, "_folder_edited", True))
        browse_folder = QPushButton("Browse…")
        browse_folder.clicked.connect(self._browse_folder)
        fg.addWidget(QLabel("App folder"), 1, 0)
        fg.addWidget(self.folder_edit, 1, 1)
        fg.addWidget(browse_folder, 1, 2)
        body.addWidget(files_box)

        # ---- App | Variant, side by side ------------------------------------------------------------
        cols = QHBoxLayout()
        cols.setSpacing(10)
        app_box = QGroupBox("App")
        af = QFormLayout(app_box)
        af.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit()
        self.catalog_combo = QComboBox(); self.catalog_combo.setEditable(True)
        self.sub_combo = QComboBox(); self.sub_combo.setEditable(True)
        conn = self.db.connect()
        cats = [r[0] for r in conn.execute(
            "SELECT DISTINCT catalog FROM apps WHERE catalog IS NOT NULL AND catalog != '' "
            "ORDER BY catalog COLLATE NOCASE")]
        self.catalog_combo.addItems([""] + cats)
        self.catalog_combo.setCurrentIndex(0)
        self.catalog_combo.currentTextChanged.connect(self._reload_subcatalogs)
        af.addRow("Name", self.name_edit)
        af.addRow("Catalog", self.catalog_combo)
        af.addRow("Subcatalog", self.sub_combo)

        var_box = QGroupBox("Variant")
        vf = QFormLayout(var_box)
        vf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.version_edit = QLineEdit()
        self.edition_edit = QLineEdit()
        self.arch_combo = QComboBox(); self.arch_combo.setEditable(True)
        self.arch_combo.addItems(["", "x64", "x86", "arm64"])
        self.lang_edit = QLineEdit()
        vf.addRow("Version", self.version_edit)
        vf.addRow("Edition", self.edition_edit)
        vf.addRow("Architecture", self.arch_combo)
        vf.addRow("Language", self.lang_edit)
        cols.addWidget(app_box, 1)
        cols.addWidget(var_box, 1)
        body.addLayout(cols)

        # ---- options / status / buttons -------------------------------------------------------------
        self.scrape_check = QCheckBox("Scrape Winget metadata after adding")
        self.scrape_check.setVisible(add_to_app is None)
        body.addWidget(self.scrape_check)
        self.status = _wrapped("")
        self.status.setStyleSheet("color: #b00020;")
        body.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        add_btn = QPushButton(action); add_btn.setDefault(True); add_btn.clicked.connect(self._on_add)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(add_btn)
        root.addLayout(buttons)

        if add_to_app is not None:
            self._lock_to_app(add_to_app)
        if self.split_variants:
            self._prepare_multi_split(files_box, var_box)
        elif split_variant:
            self._prefill_from_variant()
        elif initial_installer:                      # e.g. a file dropped onto the window
            self._set_installer(initial_installer)
        elif initial_folder:                         # a folder dropped: pick the installer inside it
            self.folder_edit.setText(os.path.normpath(initial_folder))
            self._folder_edited = True
            self._start_dir = initial_folder

        _fit_dialog(self, parent, max_w=760, max_h=content.sizeHint().height() + 90, min_w=480, min_h=260)

    # -- modes --------------------------------------------------------------------------------------------
    def _lock_to_app(self, app: dict):
        """ADD-VARIANT mode: the app is fixed."""
        self.name_edit.setText(app.get("name") or "")
        self.catalog_combo.setCurrentText(app.get("catalog") or "")
        self._reload_subcatalogs(app.get("catalog") or "")
        self.sub_combo.setCurrentText(app.get("subcatalog") or "")
        for w in (self.name_edit, self.catalog_combo, self.sub_combo):
            w.setEnabled(False)

    def _prepare_multi_split(self, files_box, var_box):
        """SPLIT-SEVERAL mode: files and per-variant fields are not edited here."""
        files_box.setEnabled(False)
        var_box.setEnabled(False)
        files_box.setTitle("Files (kept as they are)")
        var_box.setTitle("Variant (each keeps its own)")
        first = self.split_variants[0]
        full = os.path.join(first["source_path"], first["file_name"]) if first.get("file_name") else ""
        s = cur.suggest_app_fields(self.db, full or first["source_path"], first["source_path"]) if full else {}
        self.name_edit.setText(s.get("name") or os.path.basename(first["source_path"].rstrip("\\/")))
        self.catalog_combo.setCurrentText(self.split_app.get("catalog") or "")
        self._reload_subcatalogs(self.split_app.get("catalog") or "")
        self.sub_combo.setCurrentText(self.split_app.get("subcatalog") or "")
        self.rb_each.toggled.connect(lambda on: self.name_edit.setEnabled(not on))

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

    # -- helpers ------------------------------------------------------------------------------------------
    @property
    def scrape_after(self) -> bool:
        return self.scrape_check.isVisible() and self.scrape_check.isChecked()

    def _reload_subcatalogs(self, catalog: str):
        self.sub_combo.clear()
        conn = self.db.connect()
        subs = [r[0] for r in conn.execute(
            "SELECT DISTINCT subcatalog FROM apps WHERE subcatalog IS NOT NULL AND subcatalog != '' "
            "AND lower(COALESCE(catalog,'')) = lower(?) ORDER BY subcatalog COLLATE NOCASE", (catalog or "",))]
        self.sub_combo.addItems([""] + subs)

    def _browse_installer(self):
        start = os.path.dirname(self.installer_edit.text()) or self.folder_edit.text() or self._start_dir
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose installer file", start, INSTALLER_FILTER)
        if chosen:
            self._set_installer(chosen)

    def _set_installer(self, chosen: str):
        self.installer_edit.setText(os.path.normpath(chosen))
        if not self._folder_edited:
            self.folder_edit.setText(os.path.dirname(os.path.normpath(chosen)))
        s = cur.suggest_app_fields(self.db, chosen, self.folder_edit.text() or None)
        fields = [(self.version_edit, "version"), (self.edition_edit, "edition"), (self.lang_edit, "language")]
        if self.add_to_app is None:
            fields.insert(0, (self.name_edit, "name"))
        for edit, key in fields:
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

    # -- accept -------------------------------------------------------------------------------------------
    def _on_add(self):
        self.status.setText("")
        installer = self.installer_edit.text().strip()
        name = self.name_edit.text().strip()
        if self.add_to_app is not None:
            self._on_add_variant(installer)
            return
        existing = cur.find_existing_app(self.db, name) if name else None
        if self.split_variants:
            self._on_split_multi(name, existing)
            return
        if self.split_variant is not None:
            self._on_split(name, existing)
            return
        add_to = None
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

    def _on_add_variant(self, installer: str):
        try:
            self.result_info = cur.add_app_manually(
                self.db, name=self.add_to_app["name"], installer_path=installer,
                unit_folder=self.folder_edit.text().strip() or None,
                version=self.version_edit.text().strip() or None,
                edition=self.edition_edit.text().strip() or None,
                architecture=self.arch_combo.currentText().strip() or None,
                language=self.lang_edit.text().strip() or None,
                add_to_app_id=self.add_to_app["id"])
        except ValueError as e:
            self.status.setText(str(e).replace("\n", " "))
            return
        self.accept()

    def _on_split_multi(self, name: str, existing):
        ids = [v["id"] for v in self.split_variants]
        per_variant = self.rb_each.isChecked()
        target = None
        if not per_variant and existing is not None:
            if existing["id"] == self.split_app.get("id"):
                self.status.setText("That is the app the variants are already in — pick a different name.")
                return
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("App already exists")
            box.setText(f"An app named “{existing['name']}” already exists"
                        + (f" ({existing['catalog']})" if existing["catalog"] else "") + ".")
            box.setInformativeText(f"Move the {len(ids)} variants into that app, or create a separate new app with this name?")
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
            res = cur.split_variants_multi(
                self.db, ids, name=name, per_variant=per_variant, target_app_id=target,
                catalog=self.catalog_combo.currentText().strip() or None,
                subcatalog=self.sub_combo.currentText().strip() or None)
        except ValueError as e:
            self.status.setText(str(e).replace("\n", " "))
            return
        self.result_info = {"app_id": res["app_ids"][0], "app_ids": res["app_ids"],
                            "created_app": res["created"] > 0, "count": res["moved"]}
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
    WIDTHS = [56, 220, 120, 70, 220, 64, 56]

    def __init__(self, db: Database, items: list, parent=None):
        super().__init__(parent)
        self.db = db
        self.items = items
        self.applied: Optional[dict] = None
        self.setWindowTitle("Repair empty apps")

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addWidget(_wrapped(
            "These apps have no variants. Usually a rescan created a duplicate holding the variants and "
            "left the app you renamed / scraped empty.\n"
            "• merge — moves the duplicate's variants back into the empty app (it keeps its name and scraped data)\n"
            "• delete — removes an empty app that has nothing worth keeping\n"
            "• review — nothing to merge it with; left alone\n"
            "A backup of the catalog is saved first.", subtle=True))

        self.table = QTableWidget(len(items), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)       # every column resizable
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(40)
        for col, w in enumerate(self.WIDTHS):
            self.table.setColumnWidth(col, w)
        self.table.setMinimumSize(200, 120)                      # may be shrunk freely
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
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
                cell = QTableWidgetItem(v)
                cell.setToolTip(v)
                self.table.setItem(r, c, cell)
        root.addWidget(self.table, 1)
        root.addWidget(_wrapped("★ = has manual or scraped data (locked name, verified, Winget id, description …).",
                                subtle=True))

        row = QHBoxLayout()
        all_btn = QPushButton("Select all"); all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select none"); none_btn.clicked.connect(lambda: self._set_all(False))
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        close_btn = QPushButton("Close"); close_btn.clicked.connect(self.reject)
        apply_btn = QPushButton("Apply selected"); apply_btn.setDefault(True); apply_btn.clicked.connect(self._apply)
        row.addWidget(close_btn)
        row.addWidget(apply_btn)
        root.addLayout(row)

        # smaller than the main window, user-resizable
        _fit_dialog(self, parent, max_w=780, max_h=460, min_w=360, min_h=260)

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


def _fmt_size(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class SidecarsDialog(QDialog):
    """What counts as a SIDECAR of one variant (readme, crack, plugins, dependent
    folders ...): shows the automatically detected ones, the ones the user added and
    the ones the user excluded, and lets the user change that. The choices are used
    by Reorganize (shared-folder copies, "compress the whole variant") and are saved
    in the variant's metadata file."""

    COLS = ["Name", "Kind", "Size", "Source"]

    def __init__(self, db: Database, variant_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.variant_id = variant_id
        self.changed = False
        conn = db.connect()
        self.variant = dict(conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
        self.folder = self.variant["source_path"]
        self.setWindowTitle(f"Sidecars — {self.variant.get('file_name') or self.folder}")

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addWidget(_wrapped(
            "Sidecars are the extra files and folders that belong with this installer (readme, crack / keygen, "
            "plugins, skins, dependent folders …). Automatic ones are found next to the installer; you can add "
            "your own or remove ones that don't belong. Reorganize copies them along with a shared-folder "
            "installer and can compress them together with it.", subtle=True))
        info = _wrapped(f"Folder: {self.folder}", subtle=True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(info)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for col, w in enumerate([260, 60, 80, 110]):
            self.table.setColumnWidth(col, w)
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.table.setMinimumSize(200, 120)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files…")
        self.add_files_btn.clicked.connect(self._add_files)
        self.add_folder_btn = QPushButton("Add folder…")
        self.add_folder_btn.clicked.connect(self._add_folder)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.setToolTip("An added item is forgotten; an automatically detected one is excluded "
                                   "(it stays listed, greyed out, and can be restored).")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.restore_btn = QPushButton("Restore selected")
        self.restore_btn.clicked.connect(self._restore_selected)
        self.reset_btn = QPushButton("Reset to automatic")
        self.reset_btn.setToolTip("Forget everything you added or excluded.")
        self.reset_btn.clicked.connect(self._reset)
        for b in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.restore_btn, self.reset_btn):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        self.status = _wrapped("")
        root.addWidget(self.status)
        bottom = QHBoxLayout()
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder)))
        bottom.addWidget(open_btn)
        bottom.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self._reload()
        _fit_dialog(self, parent, max_w=700, max_h=440, min_w=420, min_h=300)

    # -- data ------------------------------------------------------------------------------
    def _reload(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            items = sidecars.list_sidecars(self.db, self.variant_id, with_sizes=True)
        finally:
            QApplication.restoreOverrideCursor()
        self._items = items
        self.table.setRowCount(len(items))
        labels = {"auto": "Automatic", "added": "Added by you", "excluded": "Excluded"}
        for r, it in enumerate(items):
            vals = [it.rel_path + ("/" if it.is_dir else ""), "Folder" if it.is_dir else "File",
                    _fmt_size(it.size) if it.exists else "missing", labels[it.origin]]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                cell.setToolTip(it.abs_path)
                if it.origin == "excluded":
                    cell.setForeground(QColor("gray"))
                    f = cell.font(); f.setStrikeOut(True); cell.setFont(f)
                elif not it.exists:
                    cell.setForeground(QColor("#c0392b"))
                self.table.setItem(r, c, cell)
        live = sum(1 for i in items if i.origin != "excluded")
        self.status.setText(f"{live} sidecar(s)" + (f", {len(items) - live} excluded" if len(items) > live else "") + ".")
        folder_ok = bool(self.folder) and os.path.isdir(self.folder)
        self.add_files_btn.setEnabled(folder_ok)
        self.add_folder_btn.setEnabled(folder_ok)
        self.reset_btn.setEnabled(any(i.origin in ("added", "excluded") for i in items))
        self._update_buttons()

    def _selected(self) -> list:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._items[r] for r in rows if r < len(self._items)]

    def _update_buttons(self):
        sel = self._selected()
        self.remove_btn.setEnabled(any(i.origin in ("auto", "added") for i in sel))
        self.restore_btn.setEnabled(any(i.origin == "excluded" for i in sel))

    # -- actions ---------------------------------------------------------------------------------
    def _report(self, errors: list):
        if errors:
            QMessageBox.warning(self, "Sidecars", "\n\n".join(errors[:6]) + (
                f"\n\n… and {len(errors) - 6} more" if len(errors) > 6 else ""))

    def _add_paths(self, paths: list):
        errors, already = [], 0
        for p in paths:
            try:
                state = sidecars.add_sidecar(self.db, self.variant_id, p)
                if state == "already":
                    already += 1
                else:
                    self.changed = True
            except ValueError as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self._reload()
        if already:
            self.status.setText(self.status.text() + f"  {already} item(s) were already sidecars.")
        self._report(errors)

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add sidecar files", self.folder)
        if files:
            self._add_paths(files)

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add sidecar folder", self.folder)
        if d:
            self._add_paths([d])

    def _remove_selected(self):
        for it in self._selected():
            if it.origin in ("auto", "added"):
                sidecars.remove_sidecar(self.db, self.variant_id, it.rel_path)
                self.changed = True
        self._reload()

    def _restore_selected(self):
        for it in self._selected():
            if it.origin == "excluded":
                sidecars.restore_sidecar(self.db, self.variant_id, it.rel_path)
                self.changed = True
        self._reload()

    def _reset(self):
        if QMessageBox.question(self, "Reset to automatic",
                                "Forget the items you added and the ones you excluded?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        sidecars.clear_overrides(self.db, self.variant_id)
        self.changed = True
        self._reload()

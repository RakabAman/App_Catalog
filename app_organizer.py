"""
app_organizer.py -- the "Organize" dialog (Qt GUI): duplicate detection,
category rename/merge, the catalog health report, and physical file
reorganization. The business logic behind all of it lives in
app_manager.py (kept as its own module since monitor.py also imports
directly from there, not just this GUI).

MainWindow (gui_main.py) opens the dialog via:
    from app_organizer import OrganizeDialog
    OrganizeDialog(self.db, parent=self).exec()

Double-clicking a report finding that has an app attached calls back
into MainWindow.select_app_by_id() (see gui_main.py) to jump to that
app in the main table -- this module doesn't otherwise reach back into
the main window.

Reorganize is the one feature here that moves real files on disk --
runs on a background thread (_ReorganizeWorker) so the GUI never blocks/
appears stuck during a large batch; see app_manager.execute_reorganize()'s
docstring for the file-safety choices (mandatory dry-run, collision-safe,
persisted move log, console logging via "appcatalog.organizer.job").
"""
from __future__ import annotations

import csv
import os
import webbrowser
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget,
    QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from database import Database
from resolver import merge_apps
from app_manager import (
    log,
    find_duplicate_groups, rename_catalog, rename_subcatalog,
    get_category_tree, move_subcatalog, promote_subcatalog_to_catalog,
    demote_catalog_to_subcatalog, merge_subcatalogs,
    generate_catalog_report, report_to_markdown, report_to_csv_rows,
    preview_reorganize, execute_reorganize, ReorganizeResult,
)

class _ReorganizeWorker(QThread):
    """
    Runs execute_reorganize() on a background thread so the GUI stays
    responsive for the whole run. Calling execute_reorganize() directly
    from a button handler blocks the Qt event loop until it returns --
    nothing repaints, the window looks frozen/"stuck", and there's no
    way to tell a long batch from a hang. This fixes that: progress is
    emitted after every item via execute_reorganize()'s progress_callback
    parameter, and OrganizeDialog updates a progress bar/activity log/
    status line from it as the run happens.

    Database's sqlite3 connection is opened with check_same_thread=False
    specifically so it can be used from a thread other than the one that
    created it, which is what makes running this here safe.
    """
    progress = Signal(int, int, str, str)   # index, total, source_path, status
    finished_ok = Signal(object)            # ReorganizeResult
    failed = Signal(str)                    # message, if something unexpected blew up the whole run

    def __init__(self, db: Database, plans: list, copy_mode: str, archive_format: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.plans = plans
        self.copy_mode = copy_mode
        self.archive_format = archive_format

    def run(self):
        def _on_progress(index, total, plan, status):
            self.progress.emit(index, total, plan.source_path, status)
        try:
            result = execute_reorganize(
                self.db, self.plans,
                copy_mode=self.copy_mode, archive_format=self.archive_format,
                progress_callback=_on_progress,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            log.exception("Reorganize worker thread crashed")
            self.failed.emit(str(e))


class OrganizeDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Organize")
        self.setMinimumSize(600, 400)
        self.setSizeGripEnabled(True)
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_duplicates_tab(), "Duplicates")
        tabs.addTab(self._build_categories_tab(), "Categories")
        tabs.addTab(self._build_report_tab(), "Report")
        tabs.addTab(self._build_file_structure_tab(), "Reorganize Files")
        layout.addWidget(tabs)

        close_row = QHBoxLayout()
        close_row.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        close_row.addWidget(self.close_btn)
        layout.addLayout(close_row)

    def closeEvent(self, event):
        """
        Blocks closing the dialog (the X button, Alt+F4, or the Close
        button -- all route through this) while a reorganize is still
        running on its background thread. The worker is parented to this
        dialog, so letting the dialog get destroyed mid-run would tear
        down a QThread that's still moving/copying/archiving files --
        at best an orphaned thread, at worst a half-written file. Safer
        to just make the user wait for it to finish; the progress bar and
        activity log exist precisely so that wait isn't a mystery.
        """
        if self._reorg_worker is not None and self._reorg_worker.isRunning():
            QMessageBox.warning(
                self, "Reorganize still running",
                "A reorganize is still in progress. Please wait for it to "
                "finish (see the progress bar and activity log in the "
                "Reorganize Files tab) before closing this window.",
            )
            event.ignore()
            return
        event.accept()

    def _scan_duplicates(self):
        self._dup_groups = find_duplicate_groups(self.db)
        self.dup_tree.clear()
        for idx, group in enumerate(self._dup_groups):
            # Top-level item: group header
            group_item = QTreeWidgetItem(self.dup_tree)
            header_text = f"{group.reason} (score {group.score:.0f}) — {len(group.members)} apps"
            group_item.setText(0, header_text)
            group_item.setData(0, Qt.UserRole, {"group_index": idx})
            # group checkbox (selects/deselects all children)
            group_item.setFlags(group_item.flags() | Qt.ItemIsUserCheckable)
            group_item.setCheckState(0, Qt.Unchecked)

            # Add children (apps)
            for app in group.members:
                child = QTreeWidgetItem(group_item)
                child.setText(0, app["name"])
                child.setText(1, app["catalog"] or "")
                child.setText(2, app["subcatalog"] or "")
                child.setText(3, str(app["variant_count"]))
                # Show first sample path (truncated), full path in tooltip
                sample = app["sample_paths"][0] if app["sample_paths"] else ""
                display_sample = sample
                if len(display_sample) > 80:
                    display_sample = display_sample[:77] + "..."
                child.setText(4, display_sample)
                child.setToolTip(4, sample)   # full path on hover
                child.setData(0, Qt.UserRole, {"app_id": app["id"], "group_index": idx})
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
            group_item.setExpanded(True)
        self.dup_tree.resizeColumnToContents(0)
        self.dup_tree.resizeColumnToContents(4)

    def _build_duplicates_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        intro = QLabel(
            "Groups of apps that may be duplicates. Check the apps you want to merge "
            "(the first checked app will be the target). You can also select/deselect all "
            "in a group with the group's checkbox."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)
        scan_btn = QPushButton("Scan for duplicates")
        scan_btn.clicked.connect(self._scan_duplicates)
        v.addWidget(scan_btn)

        # Tree widget: top‑level items are groups, children are apps
        self.dup_tree = QTreeWidget()
        self.dup_tree.setHeaderLabels(["App / Group", "Catalog", "Subcatalog", "Variants", "Sample Path"])
        self.dup_tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.dup_tree.setIndentation(20)
        # Enable column resizing and reordering
        self.dup_tree.header().setSectionsMovable(True)
        for col in range(5):
            self.dup_tree.header().setSectionResizeMode(col, QHeaderView.Interactive)
        v.addWidget(self.dup_tree)

        btn_row = QHBoxLayout()
        self.merge_selected_btn = QPushButton("Merge selected in group")
        self.merge_selected_btn.clicked.connect(self._merge_selected_duplicate_group)
        self.merge_all_btn = QPushButton("Merge all groups (auto)")
        self.merge_all_btn.clicked.connect(self._merge_all_groups)
        btn_row.addWidget(self.merge_selected_btn)
        btn_row.addWidget(self.merge_all_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._dup_groups = []  # store groups for reference
        return w



    def _merge_selected_duplicate_group(self):
        current = self.dup_tree.currentItem()
        if not current:
            QMessageBox.information(self, "No selection", "Select an item in a duplicate group.")
            return
        # Find the top-level group item
        parent = current.parent()
        group_item = parent if parent else current
        if group_item.parent() is not None:  # ensure it's a top-level
            group_item = group_item.parent()
        # Get group index
        data = group_item.data(0, Qt.UserRole)
        if not data:
            return
        group_idx = data["group_index"]
        group = self._dup_groups[group_idx]
        # Collect checked children
        checked_apps = []
        for i in range(group_item.childCount()):
            child = group_item.child(i)
            if child.checkState(0) == Qt.Checked:
                app_id = child.data(0, Qt.UserRole)["app_id"]
                checked_apps.append(app_id)
        if len(checked_apps) < 2:
            QMessageBox.information(self, "Not enough", "Select at least two apps in the group to merge.")
            return
        # Target: first checked
        target_id = checked_apps[0]
        source_ids = checked_apps[1:]
        confirm = QMessageBox.question(
            self, "Merge group",
            f"Merge {len(source_ids)} app(s) into '{next(app['name'] for app in group.members if app['id'] == target_id)}'?\n\n"
            f"Apps to merge: {', '.join(next(app['name'] for app in group.members if app['id'] == sid) for sid in source_ids)}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
        for sid in source_ids:
            merge_apps(self.db, sid, target_id)
        # Refresh duplicates
        self._scan_duplicates()

    def _merge_all_groups(self):
        # Gather all groups that have at least two checked apps
        groups_to_merge = []
        for idx, group in enumerate(self._dup_groups):
            # Find the group item in the tree
            # We need to locate the top-level item with matching group_index
            # We'll iterate over top-level items
            found = None
            for i in range(self.dup_tree.topLevelItemCount()):
                item = self.dup_tree.topLevelItem(i)
                data = item.data(0, Qt.UserRole)
                if data and data.get("group_index") == idx:
                    found = item
                    break
            if not found:
                continue
            checked_ids = []
            for j in range(found.childCount()):
                child = found.child(j)
                if child.checkState(0) == Qt.Checked:
                    checked_ids.append(child.data(0, Qt.UserRole)["app_id"])
            if len(checked_ids) >= 2:
                groups_to_merge.append((checked_ids, group))
        if not groups_to_merge:
            QMessageBox.information(self, "No merges", "No group has at least two checked apps.")
            return
        # Ask for confirmation
        msg = f"Will merge {len(groups_to_merge)} group(s). Continue?"
        if QMessageBox.question(self, "Merge all", msg, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        for checked_ids, group in groups_to_merge:
            target = checked_ids[0]
            for sid in checked_ids[1:]:
                merge_apps(self.db, sid, target)
        self._scan_duplicates()
    # ---------------------------------------------------------------
    # Categories tab -- live catalog/subcatalog tree with contextual
    # rename / move / merge / promote / demote actions. Everything here
    # only touches apps.catalog / apps.subcatalog; it never moves files
    # (that's the File Structure tab).
    # ---------------------------------------------------------------

    def _build_categories_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        intro = QLabel(
            "Current catalog / subcatalog structure, built live from the apps "
            "in the catalog. Right-click an item (or select several of the "
            "same kind) to rename, merge, move to a different catalog, or "
            "promote/demote it between catalog and subcatalog level. Every "
            "change updates the affected apps immediately."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        toolbar_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_category_views)
        toolbar_row.addWidget(refresh_btn)
        toolbar_row.addSpacing(16)
        self.cat_tree_view_btn = QPushButton("Tree view")
        self.cat_board_view_btn = QPushButton("Board view")
        self.cat_tree_view_btn.setCheckable(True)
        self.cat_board_view_btn.setCheckable(True)
        self.cat_tree_view_btn.setChecked(True)
        self.cat_tree_view_btn.clicked.connect(lambda: self._set_category_view(0))
        self.cat_board_view_btn.clicked.connect(lambda: self._set_category_view(1))
        toolbar_row.addWidget(self.cat_tree_view_btn)
        toolbar_row.addWidget(self.cat_board_view_btn)
        toolbar_row.addStretch()
        toolbar_row.addWidget(QLabel("Tip: double-click an item to rename it."))
        v.addLayout(toolbar_row)

        self.cat_stack = QStackedWidget()

        # -- Page 0: tree (catalog -> subcatalog, one column) --------
        self.cat_tree = QTreeWidget()
        self.cat_tree.setHeaderLabels(["Catalog / Subcatalog", "Apps"])
        #self.cat_tree.setColumnWidth(0, 420)
        # Let the first column stretch and the second shrink to content
        self.cat_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.cat_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.cat_tree.setColumnWidth(1, 60)   # give the "Apps" column a reasonable 
        self.cat_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.cat_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cat_tree.customContextMenuRequested.connect(self._category_tree_context_menu)
        self.cat_tree.itemDoubleClicked.connect(
            lambda item, _col: self._rename_category_data(item.data(0, Qt.UserRole))
        )
        self.cat_stack.addWidget(self.cat_tree)

        # -- Page 1: board (every catalog as its own column) ---------
        board_scroll = QScrollArea()
        board_scroll.setWidgetResizable(True)
        self.cat_board_container = QWidget()
        self.cat_board_layout = QHBoxLayout(self.cat_board_container)
        self.cat_board_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        board_scroll.setWidget(self.cat_board_container)
        self.cat_stack.addWidget(board_scroll)

        v.addWidget(self.cat_stack, stretch=1)

        self._refresh_category_views()
        return w

    def _set_category_view(self, index: int):
        self.cat_stack.setCurrentIndex(index)
        self.cat_tree_view_btn.setChecked(index == 0)
        self.cat_board_view_btn.setChecked(index == 1)

    def _refresh_category_views(self):
        """Re-reads the catalog/subcatalog structure once and repopulates
        both the tree and board views from the same snapshot, so switching
        between them never shows stale data."""
        self._category_data = get_category_tree(self.db)
        self._refresh_category_tree()
        self._refresh_category_board()

    def _refresh_category_tree(self):
        self.cat_tree.clear()
        for cat in self._category_data:
            cat_item = QTreeWidgetItem([cat["catalog"], str(cat["app_count"])])
            cat_item.setData(0, Qt.UserRole, {"level": "catalog", "catalog": cat["catalog"]})
            for sub in cat["subcatalogs"]:
                label = sub["subcatalog"] or "(none)"
                sub_item = QTreeWidgetItem([label, str(sub["app_count"])])
                sub_item.setData(0, Qt.UserRole, {
                    "level": "subcatalog", "catalog": cat["catalog"], "subcatalog": sub["subcatalog"],
                })
                cat_item.addChild(sub_item)
            self.cat_tree.addTopLevelItem(cat_item)
        self.cat_tree.expandAll()

    def _refresh_category_board(self):
        while self.cat_board_layout.count():
            child = self.cat_board_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        for cat in self._category_data:
            self.cat_board_layout.addWidget(self._build_category_column(cat))

    def _build_category_column(self, cat: dict) -> QWidget:
        """One catalog's card: header (name + count + a small ⋯ menu for
        catalog-level actions) over a plain list of its subcategories.
        Selecting/right-clicking/double-clicking a subcategory row goes
        through the exact same dialogs as the tree view -- this is just a
        different arrangement of the same data, not a separate feature."""
        box = QGroupBox()
        box.setMinimumWidth(220)
        box.setMaximumWidth(260)
        vbox = QVBoxLayout(box)

        header_row = QHBoxLayout()
        title = QLabel(f"<b>{cat['catalog']}</b>")
        title.setWordWrap(True)
        header_row.addWidget(title, stretch=1)
        count_label = QLabel(str(cat["app_count"]))
        count_label.setStyleSheet("color: palette(mid);")
        header_row.addWidget(count_label)
        menu_btn = QToolButton()
        menu_btn.setText("⋯")
        menu_btn.setPopupMode(QToolButton.InstantPopup)
        header_menu = QMenu(menu_btn)
        catalog_name = cat["catalog"]
        rename_act = header_menu.addAction("Rename catalog…")
        rename_act.triggered.connect(
            lambda _checked=False, c=catalog_name: self._rename_category_data({"level": "catalog", "catalog": c})
        )
        demote_act = header_menu.addAction("Demote to subcatalog of…")
        demote_act.triggered.connect(
            lambda _checked=False, c=catalog_name: self._demote_catalog_data({"level": "catalog", "catalog": c})
        )
        menu_btn.setMenu(header_menu)
        header_row.addWidget(menu_btn)
        vbox.addLayout(header_row)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        for sub in cat["subcatalogs"]:
            label = sub["subcatalog"] or "(none)"
            item = QListWidgetItem(f"{label}   ({sub['app_count']})")
            item.setData(Qt.UserRole, {
                "level": "subcatalog", "catalog": cat["catalog"], "subcatalog": sub["subcatalog"],
            })
            list_widget.addItem(item)
        list_widget.itemDoubleClicked.connect(
            lambda item: self._rename_category_data(item.data(Qt.UserRole))
        )
        list_widget.customContextMenuRequested.connect(
            lambda pos, lw=list_widget: self._show_category_context_menu(
                [i.data(Qt.UserRole) for i in lw.selectedItems()], lw.viewport().mapToGlobal(pos)
            )
        )
        vbox.addWidget(list_widget)
        return box

    def _existing_catalogs(self, exclude: Optional[str] = None) -> list[str]:
        return [c["catalog"] for c in get_category_tree(self.db) if c["catalog"] != exclude]

    def _category_tree_context_menu(self, pos):
        items = self.cat_tree.selectedItems()
        datas = [i.data(0, Qt.UserRole) for i in items]
        self._show_category_context_menu(datas, self.cat_tree.viewport().mapToGlobal(pos))

    def _show_category_context_menu(self, datas: list[dict], global_pos):
        """Shared by both views -- builds and executes the right-click menu
        for whatever's selected, then dispatches to the same rename/move/
        promote/demote/merge handlers either view's selection produced."""
        if not datas:
            return
        levels = {d["level"] for d in datas}
        menu = QMenu(self)
        rename_action = move_action = promote_action = demote_action = merge_action = None
        if len(datas) == 1:
            rename_action = menu.addAction("Rename…")
            if datas[0]["level"] == "subcatalog":
                move_action = menu.addAction("Move to different catalog…")
                promote_action = menu.addAction("Promote to top-level catalog…")
            else:
                demote_action = menu.addAction("Demote to subcatalog of…")
        elif levels == {"subcatalog"} and len({d["catalog"] for d in datas}) == 1:
            merge_action = menu.addAction("Merge selected subcatalogs into one…")
        if menu.isEmpty():
            return
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is rename_action:
            self._rename_category_data(datas[0])
        elif chosen is move_action:
            self._move_subcatalog_data(datas[0])
        elif chosen is promote_action:
            self._promote_subcatalog_data(datas[0])
        elif chosen is demote_action:
            self._demote_catalog_data(datas[0])
        elif chosen is merge_action:
            self._merge_subcatalogs_data(datas)

    def _rename_category_data(self, data: dict):
        if data["level"] == "catalog":
            old = data["catalog"]
            new, ok = QInputDialog.getText(self, "Rename catalog", "New name:", text=old)
            if not ok or not new.strip() or new.strip() == old:
                return
            affected = rename_catalog(self.db, old, new.strip())
        else:
            old = data["subcatalog"]
            new, ok = QInputDialog.getText(self, "Rename subcatalog", "New name:", text=old)
            if not ok or not new.strip() or new.strip() == old:
                return
            affected = rename_subcatalog(self.db, new.strip(), old, catalog=data["catalog"])
        QMessageBox.information(self, "Done", f"{affected} app(s) moved from '{old}' to '{new.strip()}'.")
        self._refresh_category_views()

    def _move_subcatalog_data(self, data: dict):
        catalogs = self._existing_catalogs(exclude=data["catalog"])
        if not catalogs:
            QMessageBox.information(self, "No other catalogs", "There's no other catalog to move this into yet.")
            return
        target, ok = QInputDialog.getItem(
            self, "Move subcatalog",
            f"Move '{data['subcatalog'] or '(none)'}' to which catalog?",
            catalogs, 0, editable=True,
        )
        if not ok or not target.strip():
            return
        affected = move_subcatalog(self.db, data["subcatalog"], data["catalog"], target.strip())
        QMessageBox.information(self, "Done", f"{affected} app(s) moved to '{target.strip()}'.")
        self._refresh_category_views()

    def _promote_subcatalog_data(self, data: dict):
        default_name = data["subcatalog"] or "New Catalog"
        name, ok = QInputDialog.getText(self, "Promote to catalog", "New top-level catalog name:", text=default_name)
        if not ok or not name.strip():
            return
        affected = promote_subcatalog_to_catalog(self.db, data["catalog"], data["subcatalog"], name.strip())
        QMessageBox.information(self, "Done", f"{affected} app(s) now under catalog '{name.strip()}'.")
        self._refresh_category_views()

    def _demote_catalog_data(self, data: dict):
        catalogs = self._existing_catalogs(exclude=data["catalog"])
        if not catalogs:
            QMessageBox.information(self, "No other catalogs", "There's no other catalog to demote this into yet.")
            return
        parent, ok = QInputDialog.getItem(
            self, "Demote to subcatalog",
            f"Make '{data['catalog']}' a subcatalog of which catalog?",
            catalogs, 0, editable=False,
        )
        if not ok:
            return
        name, ok2 = QInputDialog.getText(self, "Subcatalog name", "Name for this subcatalog:", text=data["catalog"])
        if not ok2 or not name.strip():
            return
        affected = demote_catalog_to_subcatalog(self.db, data["catalog"], parent, name.strip())
        QMessageBox.information(self, "Done", f"{affected} app(s) moved into '{parent} / {name.strip()}'.")
        self._refresh_category_views()

    def _merge_subcatalogs_data(self, datas: list[dict]):
        names = [d["subcatalog"] or "(none)" for d in datas]
        target, ok = QInputDialog.getItem(
            self, "Merge subcatalogs", "Merge into which name?", names, 0, editable=True,
        )
        if not ok or not target.strip():
            return
        sources = [d["subcatalog"] for d in datas]
        affected = merge_subcatalogs(self.db, datas[0]["catalog"], sources, target.strip())
        QMessageBox.information(self, "Done", f"{affected} app(s) merged into '{target.strip()}'.")
        self._refresh_category_views()

    # ---------------------------------------------------------------
    # Report tab -- full catalog health report (see
    # app_manager.generate_catalog_report). Rendered as a collapsible
    # tree: one top-level item per section, one child per finding.
    # Findings that map to a specific app can be double-clicked to jump
    # to that app in the main window's table. Exportable as Markdown,
    # CSV, or straight to the clipboard.
    # ---------------------------------------------------------------

    _SEVERITY_COLOR = {
        "warning": "#b02a2a",
        "info": "#8a6d1a",
        "ok": "#2a7a3d",
    }
    _SEVERITY_LABEL = {"warning": "⚠", "info": "ℹ", "ok": "✓"}

    def _build_report_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        intro = QLabel(
            "A point-in-time health check of the whole catalog: review queue, "
            "unresolved scans, duplicate candidates, metadata gaps, and scan "
            "errors. Read-only -- fix things from the other tabs or the main "
            "window, then re-generate. Double-click a finding to jump to that app."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        btn_row = QHBoxLayout()
        gen_btn = QPushButton("Generate report")
        gen_btn.clicked.connect(self._generate_report)
        btn_row.addWidget(gen_btn)
        self.report_expand_btn = QPushButton("Expand all")
        self.report_expand_btn.clicked.connect(lambda: self.report_tree.expandAll())
        btn_row.addWidget(self.report_expand_btn)
        self.report_collapse_btn = QPushButton("Collapse all")
        self.report_collapse_btn.clicked.connect(lambda: self.report_tree.collapseAll())
        btn_row.addWidget(self.report_collapse_btn)
        btn_row.addStretch()
        self.report_summary_label = QLabel("No report generated yet.")
        btn_row.addWidget(self.report_summary_label)
        v.addLayout(btn_row)

        self.report_tree = QTreeWidget()
        self.report_tree.setHeaderLabels(["Finding"])
        self.report_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.report_tree.itemDoubleClicked.connect(self._on_report_item_activated)
        v.addWidget(self.report_tree)

        export_row = QHBoxLayout()
        export_md_btn = QPushButton("Export as Markdown…")
        export_md_btn.clicked.connect(self._export_report_markdown)
        export_row.addWidget(export_md_btn)
        export_csv_btn = QPushButton("Export as CSV…")
        export_csv_btn.clicked.connect(self._export_report_csv)
        export_row.addWidget(export_csv_btn)
        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.clicked.connect(self._copy_report_to_clipboard)
        export_row.addWidget(copy_btn)
        export_row.addStretch()
        v.addLayout(export_row)

        self._current_report = None
        return w

    def _generate_report(self):
        self._current_report = generate_catalog_report(self.db)
        report = self._current_report

        self.report_tree.clear()
        for section in report.sections:
            color = self._SEVERITY_COLOR.get(section.severity, "#000000")
            mark = self._SEVERITY_LABEL.get(section.severity, "")
            top = QTreeWidgetItem([f"{mark}  {section.title}"])
            top.setForeground(0, QColor(color))
            top.setData(0, Qt.UserRole, None)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            if section.items:
                for item in section.items:
                    child = QTreeWidgetItem([item.text])
                    child.setData(0, Qt.UserRole, item.app_id)
                    if item.app_id is not None:
                        # visually hint that this row is clickable
                        cfont = child.font(0)
                        cfont.setUnderline(True)
                        child.setFont(0, cfont)
                    top.addChild(child)
            else:
                empty = QTreeWidgetItem([section.empty_text])
                empty.setData(0, Qt.UserRole, None)
                efont = empty.font(0)
                efont.setItalic(True)
                empty.setFont(0, efont)
                top.addChild(empty)
            self.report_tree.addTopLevelItem(top)
            # Warnings default to expanded (need attention), everything
            # else stays collapsed so the tree isn't a wall of "OK" text.
            top.setExpanded(section.severity == "warning" and bool(section.items))

        self.report_summary_label.setText(
            f"Generated {report.generated_at}  --  "
            f"{report.warning_count} warning(s), {report.info_count} informational"
        )

    def _on_report_item_activated(self, item: QTreeWidgetItem, column: int):
        app_id = item.data(0, Qt.UserRole)
        if app_id is None:
            return
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "select_app_by_id"):
            main_window.select_app_by_id(app_id)

    def _ensure_report(self) -> bool:
        if self._current_report is None:
            QMessageBox.information(self, "No report yet", "Click \"Generate report\" first.")
            return False
        return True

    def _export_report_markdown(self):
        if not self._ensure_report():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export report as Markdown", "catalog_report.md", "Markdown files (*.md);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report_to_markdown(self._current_report))
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        QMessageBox.information(self, "Exported", f"Report saved to {path}")

    def _export_report_csv(self):
        if not self._ensure_report():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export report as CSV", "catalog_report.csv", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(report_to_csv_rows(self._current_report))
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        QMessageBox.information(self, "Exported", f"Report saved to {path}")

    def _copy_report_to_clipboard(self):
        if not self._ensure_report():
            return
        QApplication.clipboard().setText(report_to_markdown(self._current_report))
        QMessageBox.information(self, "Copied", "Report copied to clipboard as Markdown.")

    def _build_file_structure_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        intro = QLabel(
            "Physically moves (or copies) each app version's install folder into a clean\n"
            "  <destination>/Catalog/Subcatalog/AppName/Version/\n"
            "structure -- Portable/OriginalCatalog/AppName/Version/ instead, for apps "
            "tagged Portable. A version's whole install folder moves as one unit, so "
            "anything sitting alongside the installer (readme, crack, keygen, theme, "
            "serial, ...) travels with it automatically. The rare case of two distinct "
            "installers sharing one folder is handled per-file, flagged below as "
            "\"Shared\". Archiving compresses only the installer file itself (e.g. "
            "Setup.exe -> Setup.7z) -- readme/crack/theme/serial/etc sitting next to "
            "it stay as ordinary loose files, never swept into the archive. A file "
            "that's already packaged (.zip/.rar/.7z/...) is left alone rather than "
            "compressed again. ALWAYS preview first -- nothing on disk changes until "
            "you explicitly execute the previewed plan. A move log (JSON) is written "
            "next to the database for every run, so any run can be audited or "
            "reversed by hand."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        dest_row.addWidget(self.dest_edit, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_dest)
        dest_row.addWidget(browse_btn)
        v.addLayout(dest_row)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Transfer:"))
        self.reorg_copy_mode_combo = QComboBox()
        self.reorg_copy_mode_combo.addItem("Move (default)", "move")
        self.reorg_copy_mode_combo.addItem("Copy (originals stay in place)", "copy")
        opts_row.addWidget(self.reorg_copy_mode_combo)

        opts_row.addWidget(QLabel("Archive as:"))
        self.reorg_archive_combo = QComboBox()
        self.reorg_archive_combo.addItem("None -- leave as folder", "none")
        self.reorg_archive_combo.addItem("7z (recommended)", "7z")
        self.reorg_archive_combo.addItem("Zip", "zip")
        self.reorg_archive_combo.addItem("RAR (needs rar/WinRAR on PATH)", "rar")
        opts_row.addWidget(self.reorg_archive_combo)
        opts_row.addStretch()
        v.addLayout(opts_row)

        self.reorg_portable_check = QCheckBox(
            "Route Portable-tagged apps into a dedicated \"Portable\" category "
            "(original catalog becomes the subcategory)"
        )
        self.reorg_portable_check.setChecked(True)
        v.addWidget(self.reorg_portable_check)

        self.reorg_preview_btn = QPushButton("Preview (dry run -- changes nothing)")
        self.reorg_preview_btn.clicked.connect(self._preview_reorganize)
        v.addWidget(self.reorg_preview_btn)

        self.reorg_table = QTableWidget(0, 5)
        self.reorg_table.setHorizontalHeaderLabels(
            ["Source folder", "Destination", "Portable?", "Shared folder?", "Notes"]
        )
        self.reorg_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.reorg_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.reorg_table)

        self.reorg_status_label = QLabel("")
        # This label receives long status strings both after Preview
        # ("N items planned -- X portable, Y sharing...") and during a
        # run ("[N/M] starting: /very/long/source/path"). Without wrap,
        # it clips or forces the dialog wider.
        self.reorg_status_label.setWordWrap(True)
        v.addWidget(self.reorg_status_label)

        self.execute_btn = QPushButton("Execute previewed plan")
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self._execute_reorganize)
        v.addWidget(self.execute_btn)

        # Progress bar + a live-scrolling activity log so a run never
        # looks "stuck" -- execute_reorganize() runs on a background
        # thread (see _ReorganizeWorker below) precisely so this can
        # update while it's happening instead of the whole window
        # freezing until it's done.
        self.reorg_progress_bar = QProgressBar()
        self.reorg_progress_bar.setTextVisible(True)
        v.addWidget(self.reorg_progress_bar)

        self.reorg_activity_log = QPlainTextEdit()
        self.reorg_activity_log.setReadOnly(True)
        self.reorg_activity_log.setMaximumBlockCount(2000)  # cap growth on very large batches
        self.reorg_activity_log.setPlaceholderText("Activity will appear here while a run is in progress…")
        self.reorg_activity_log.setFixedHeight(120)
        v.addWidget(self.reorg_activity_log)

        self._reorg_plans = []
        self._reorg_worker = None
        return w

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose destination folder")
        if folder:
            self.dest_edit.setText(folder)

    def _preview_reorganize(self):
        dest = self.dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return
        self._reorg_plans = preview_reorganize(
            self.db, dest,
            portable_to_dedicated_category=self.reorg_portable_check.isChecked(),
        )
        self.reorg_table.setRowCount(len(self._reorg_plans))
        collisions = 0
        portable_count = 0
        shared_count = 0
        for i, p in enumerate(self._reorg_plans):
            self.reorg_table.setItem(i, 0, QTableWidgetItem(p.source_path))
            self.reorg_table.setItem(i, 1, QTableWidgetItem(p.dest_path))
            self.reorg_table.setItem(i, 2, QTableWidgetItem("Yes" if p.is_portable else ""))
            self.reorg_table.setItem(i, 3, QTableWidgetItem("Yes" if p.shared_folder else ""))
            notes = []
            if p.collision:
                notes.append("destination already has content")
            if p.shared_folder:
                notes.append(f"{len(p.shared_extra_paths)} shared extra item(s) will be copied, not moved")
            self.reorg_table.setItem(i, 4, QTableWidgetItem("; ".join(notes)))
            if p.collision:
                collisions += 1
            if p.is_portable:
                portable_count += 1
            if p.shared_folder:
                shared_count += 1
        self.reorg_table.resizeColumnsToContents()
        self.reorg_status_label.setText(
            f"{len(self._reorg_plans)} item(s) planned -- {portable_count} portable, "
            f"{shared_count} sharing a folder with another install unit, {collisions} "
            f"destination(s) already have content (individual files there will be "
            f"skipped, never overwritten)."
        )
        self.reorg_progress_bar.setRange(0, max(len(self._reorg_plans), 1))
        self.reorg_progress_bar.setValue(0)
        self.reorg_activity_log.clear()
        self.execute_btn.setEnabled(len(self._reorg_plans) > 0)

    def _execute_reorganize(self):
        if not self._reorg_plans:
            return
        copy_mode = self.reorg_copy_mode_combo.currentData()
        archive_format = self.reorg_archive_combo.currentData()
        verb = "copy" if copy_mode == "copy" else "move"
        archive_note = (
            f" Each bare installer file will then be compressed to .{archive_format} "
            f"in place (sidecars like readme/crack/theme are left as-is; already-"
            f"compressed installers are skipped)."
            if archive_format != "none" else ""
        )
        reply = QMessageBox.question(
            self, "Confirm reorganize",
            f"This will physically {verb} {len(self._reorg_plans)} item(s) on disk."
            f"{archive_note}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Runs on a background thread -- see _ReorganizeWorker's docstring
        # for why (calling execute_reorganize() directly here would block
        # the GUI thread for the whole run, which is what made the app
        # look stuck with no feedback).
        self.execute_btn.setEnabled(False)
        self.reorg_preview_btn.setEnabled(False)
        self.close_btn.setEnabled(False)  # see closeEvent() -- closing mid-run is unsafe
        self.reorg_activity_log.clear()
        self.reorg_progress_bar.setRange(0, len(self._reorg_plans))
        self.reorg_progress_bar.setValue(0)
        self.reorg_status_label.setText("Starting…")
        self.reorg_activity_log.appendPlainText(
            f"Starting: {len(self._reorg_plans)} item(s), mode={copy_mode}, archive={archive_format} …"
        )

        self._reorg_worker = _ReorganizeWorker(
            self.db, self._reorg_plans, copy_mode, archive_format, parent=self
        )
        self._reorg_worker.progress.connect(self._on_reorg_progress)
        self._reorg_worker.finished_ok.connect(self._on_reorg_finished)
        self._reorg_worker.failed.connect(self._on_reorg_failed)
        self._reorg_worker.start()

    def _on_reorg_progress(self, index: int, total: int, source_path: str, status: str):
        # "starting" fires the instant work begins on item `index`, before
        # it's actually done -- keep the bar's fill at completed-count
        # (index - 1) for that one, so it doesn't visually claim an item
        # is finished before it is, but still update the status line/log
        # immediately so a slow item (a big folder, a slow archive step)
        # doesn't look identical to the whole run being stuck.
        self.reorg_progress_bar.setValue(min(index - 1 if status == "starting" else index, total))
        self.reorg_status_label.setText(f"[{index}/{total}] {status}: {source_path}")
        self.reorg_activity_log.appendPlainText(f"[{index}/{total}] {status.upper()}: {source_path}")

    def _on_reorg_finished(self, result: "ReorganizeResult"):
        summary = (
            f"{'Moved' if self.reorg_copy_mode_combo.currentData() == 'move' else 'Copied'}: {result.moved}\n"
            f"Archived: {result.archived}\n"
            f"Skipped (destination collision): {result.skipped_collisions}\n"
            f"Failed: {len(result.failed)}"
        )
        self.reorg_activity_log.appendPlainText("")
        self.reorg_activity_log.appendPlainText("DONE -- " + summary.replace("\n", "  |  "))
        self.reorg_status_label.setText("Done.")

        # checkpoint 21: a bare counts dialog pointing at a raw JSON log
        # made "what actually failed, and why" a chore to find out. The
        # HTML report has that up front (a "Needs attention" section with
        # the real reason per item), so open it directly instead of
        # making the user go dig for the log file.
        report_note = ""
        if result.html_report_path and os.path.exists(result.html_report_path):
            try:
                webbrowser.open(Path(result.html_report_path).as_uri())
                report_note = "\n\nA detailed report has been opened in your browser."
            except Exception as e:
                report_note = f"\n\nDetailed report: {result.html_report_path}\n(Could not auto-open it: {e})"
        elif result.move_log_path:
            report_note = f"\n\nMove log: {result.move_log_path}"

        QMessageBox.information(self, "Reorganize complete", summary + report_note)
        self._reorg_plans = []
        self.reorg_table.setRowCount(0)
        self.execute_btn.setEnabled(False)
        self.reorg_preview_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        self._reorg_worker = None

    def _on_reorg_failed(self, message: str):
        self.reorg_activity_log.appendPlainText(f"\nUNEXPECTED ERROR: {message}")
        self.reorg_status_label.setText("Failed -- see message.")
        QMessageBox.critical(self, "Reorganize failed", f"An unexpected error stopped the run:\n\n{message}")
        self.execute_btn.setEnabled(True)
        self.reorg_preview_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        self._reorg_worker = None
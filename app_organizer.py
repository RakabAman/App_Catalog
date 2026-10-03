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
import html
import logging
import os
import shutil
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
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
    preview_reorganize, execute_reorganize, ReorganizeResult, plan_bytes_to_write,
    validate_archive_password, fmt_bytes, fmt_duration,
)

class _ReorganizeWorker(QThread):
    """
    Runs execute_reorganize() on a background thread so the GUI stays
    responsive. The engine reports through ReorgEvent objects -- a start
    event per item, phase changes (move / copy / archive / cleanup), ~5
    byte-progress updates a second while data is being written, and one
    final event per item -- which arrive here as the `reorg_event` signal
    (queued onto the GUI thread, so it is safe even though the archive size
    poller emits from yet another thread). cancel() sets the flag the engine
    checks between 4 MB chunks.

    Database's sqlite3 connection is opened with check_same_thread=False
    specifically so it can be used from a thread other than the one that
    created it, which is what makes running this here safe.
    """
    reorg_event = Signal(object)            # app_manager.ReorgEvent
    finished_ok = Signal(object)            # ReorganizeResult
    failed = Signal(str)                    # something unexpected blew up the whole run

    def __init__(self, db: Database, plans: list, copy_mode: str, archive_format: str,
                 archive_password: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.plans = plans
        self.copy_mode = copy_mode
        self.archive_format = archive_format
        self.archive_password = archive_password
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = execute_reorganize(
                self.db, self.plans,
                copy_mode=self.copy_mode, archive_format=self.archive_format,
                archive_password=self.archive_password,
                progress_callback=self.reorg_event.emit,
                cancel_event=self.cancel_event,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            log.exception("Reorganize worker thread crashed")
            self.failed.emit(str(e))


class _ElidedLabel(QLabel):
    """One-line label that shortens its text with '...' in the middle instead of forcing the
    window wide. The full text is always available as the tooltip."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self._color = None
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):               # noqa: N802 (Qt naming)
        self._full = text
        self.update()

    def text(self) -> str:
        return self._full

    def setColor(self, color):                  # noqa: N802
        self._color = QColor(color) if color else None
        self.update()

    def minimumSizeHint(self):                  # noqa: N802
        return QSize(40, super().minimumSizeHint().height())

    def paintEvent(self, event):                # noqa: N802
        p = QPainter(self)
        p.setPen(self._color or self.palette().windowText().color())
        p.drawText(self.rect(), int(Qt.AlignVCenter | Qt.AlignLeft),
                   self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, self.width()))


class _LogBridge(QObject):
    line = Signal(str, int)          # formatted log line, logging level


class _QtLogHandler(logging.Handler):
    """Mirrors the engine's console log lines into the dialog's rolling log box, so the GUI
    shows exactly what the terminal shows. Records come from the worker thread; the signal
    is queued onto the GUI thread."""

    def __init__(self, bridge: _LogBridge):
        super().__init__(logging.INFO)
        self.bridge = bridge
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.bridge.line.emit(self.format(record), record.levelno)
        except Exception:
            pass


class OrganizeDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Organize")
        # The window can be made as small as this; each tab scrolls instead of forcing the
        # window to stay big (a tab's own minimum -- e.g. ~870x450 for Reorganize -- used to
        # be the window's effective minimum).
        self.setMinimumSize(420, 300)
        self.setSizeGripEnabled(True)
        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        w0, h0 = (min(1000, int(avail.width() * 0.9)), min(820, int(avail.height() * 0.85))) if avail else (1000, 780)
        self.resize(w0, h0)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._scrollable(self._build_duplicates_tab()), "Duplicates")
        tabs.addTab(self._scrollable(self._build_categories_tab()), "Categories")
        tabs.addTab(self._scrollable(self._build_report_tab()), "Report")
        tabs.addTab(self._scrollable(self._build_file_structure_tab()), "Reorganize Files")
        layout.addWidget(tabs)

        close_row = QHBoxLayout()
        close_row.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        close_row.addWidget(self.close_btn)
        layout.addLayout(close_row)

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """Wraps a tab page so the dialog can shrink below the page's natural size:
        scrollbars appear instead of the window refusing to get smaller."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(page)
        return area

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

    # ------------------------------------------------------------------
    # Duplicates tab: every row says plainly what will happen to it
    # ------------------------------------------------------------------
    _DUP_REASON_TEXT = {
        "exact fixed name": "same name, ignoring capitals, trailing numbers/versions and (brackets)",
        "exact base (version stripped)": "same name once the version is removed",
        "exact normalized": "same name after ignoring punctuation and capitals",
        "fuzzy base": "very similar names",
        "token set": "same words, in any order",
    }
    _DUP_COLS = ["App / Group", "What will happen", "Catalog", "Subcatalog", "Variants", "Sample Path"]
    _DUP_KEEP = QColor(76, 175, 80)       # green
    _DUP_MERGE = QColor(224, 160, 48)     # amber
    _DUP_IDLE = QColor(144, 144, 144)     # grey

    def _scan_duplicates(self):
        self._dup_scanned = True
        self._dup_groups = find_duplicate_groups(self.db)
        self._dup_syncing = True          # don't fire the check-sync handler while filling the tree
        self.dup_tree.clear()
        bold = self.dup_tree.font()
        bold.setBold(True)
        for idx, group in enumerate(self._dup_groups):
            gi = QTreeWidgetItem(self.dup_tree)
            why = self._DUP_REASON_TEXT.get(group.reason, group.reason)
            if group.reason.startswith("fuzzy"):
                why += f" ({group.score:.0f}% alike)"
            gi.setText(0, f"Group {idx + 1}  ·  {len(group.members)} apps  ·  {why}")
            gi.setFont(0, bold)
            gi.setData(0, Qt.UserRole, {"group_index": idx, "keeper": None})
            gi.setFlags(gi.flags() | Qt.ItemIsUserCheckable)
            gi.setCheckState(0, Qt.Unchecked)
            for app in group.members:
                child = QTreeWidgetItem(gi)
                child.setText(0, app["name"])
                child.setText(2, app["catalog"] or "")
                child.setText(3, app["subcatalog"] or "")
                child.setText(4, str(app["variant_count"]))
                sample = app["sample_paths"][0] if app["sample_paths"] else ""
                child.setText(5, sample if len(sample) <= 80 else sample[:77] + "...")
                child.setToolTip(5, sample)
                child.setData(0, Qt.UserRole, {"app_id": app["id"], "group_index": idx})
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
            gi.setExpanded(True)
        self._dup_syncing = False
        for i in range(self.dup_tree.topLevelItemCount()):
            self._refresh_dup_group(self.dup_tree.topLevelItem(i))
        for col in (0, 2, 3, 4):
            self.dup_tree.resizeColumnToContents(col)
        self.dup_tree.setColumnWidth(1, 360)      # fits the longest 'What will happen' text
        self._update_dup_summary()

    def _build_duplicates_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        intro = QLabel(
            "Each group lists apps that look like the same program. <b>Tick the apps you want to "
            "merge.</b> The <b style='color:#4caf50'>★ KEEP</b> app stays; every other ticked app "
            "<b style='color:#e0a030'>→ merges into it</b>: its versions/files are re-attached to the "
            "kept app and its duplicate catalog entry is removed. <i>No files on disk are touched.</i> "
            "By default the first ticked app is kept; right-click an app to choose another."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        v.addWidget(intro)
        scan_btn = QPushButton("Scan for duplicates")
        scan_btn.clicked.connect(self._scan_duplicates)
        v.addWidget(scan_btn)

        self.dup_tree = QTreeWidget()
        self.dup_tree.setColumnCount(len(self._DUP_COLS))
        self.dup_tree.setHeaderLabels(self._DUP_COLS)
        self.dup_tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.dup_tree.setIndentation(20)
        self.dup_tree.setUniformRowHeights(True)
        self._dup_syncing = False
        self.dup_tree.itemChanged.connect(self._on_dup_item_changed)
        self.dup_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.dup_tree.customContextMenuRequested.connect(self._dup_context_menu)
        self.dup_tree.header().setSectionsMovable(True)
        for col in range(len(self._DUP_COLS)):
            self.dup_tree.header().setSectionResizeMode(col, QHeaderView.Interactive)
        v.addWidget(self.dup_tree, 1)

        self.dup_summary_label = QLabel("Press “Scan for duplicates”.")
        self.dup_summary_label.setWordWrap(True)
        v.addWidget(self.dup_summary_label)

        btn_row = QHBoxLayout()
        self.merge_selected_btn = QPushButton("Merge checked apps")
        self.merge_selected_btn.setEnabled(False)
        self.merge_selected_btn.setToolTip(
            "Merges every group that has two or more ticked apps into that group's ★ KEEP app.")
        self.merge_selected_btn.clicked.connect(self._merge_checked_groups)
        btn_row.addWidget(self.merge_selected_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._dup_groups = []
        return w

    def _dup_group_state(self, group_item):
        """(keeper_app_id or None, [app_ids that merge into it]) for one group, from the checkboxes."""
        data = group_item.data(0, Qt.UserRole) or {}
        ids = [group_item.child(j).data(0, Qt.UserRole)["app_id"]
               for j in range(group_item.childCount())
               if group_item.child(j).checkState(0) == Qt.Checked]
        keeper = data.get("keeper")
        if keeper not in ids:
            keeper = ids[0] if ids else None
        return keeper, [i for i in ids if i != keeper]

    def _refresh_dup_group(self, group_item):
        """Rewrites the 'What will happen' column + colours for one group."""
        prev = self._dup_syncing
        self._dup_syncing = True          # setText/setBackground also fire itemChanged
        try:
            keeper, merging = self._dup_group_state(group_item)
            keeper_name = ""
            for j in range(group_item.childCount()):
                if group_item.child(j).data(0, Qt.UserRole)["app_id"] == keeper:
                    keeper_name = group_item.child(j).text(0)
            n_checked = (1 if keeper is not None else 0) + len(merging)

            for j in range(group_item.childCount()):
                ch = group_item.child(j)
                app_id = ch.data(0, Qt.UserRole)["app_id"]
                if ch.checkState(0) != Qt.Checked:
                    text, colour = "left as it is", self._DUP_IDLE
                elif app_id == keeper and merging:
                    text, colour = f"★ KEEP  (+{len(merging)} merged into it)", self._DUP_KEEP
                elif app_id == keeper:
                    text, colour = "tick another app to merge into this", self._DUP_IDLE
                else:
                    text, colour = f"→ merges into “{keeper_name}”", self._DUP_MERGE
                ch.setText(1, text)
                ch.setForeground(1, colour)
                tint = None
                if ch.checkState(0) == Qt.Checked and merging:
                    tint = QColor(colour.red(), colour.green(), colour.blue(), 40)
                for col in range(len(self._DUP_COLS)):
                    ch.setBackground(col, tint if tint else QColor(0, 0, 0, 0))
                f = ch.font(1)
                f.setBold(app_id == keeper and bool(merging))
                ch.setFont(1, f)

            if merging:
                group_item.setText(1, f"{len(merging)} app(s)  →  “{keeper_name}”")
                group_item.setForeground(1, self._DUP_MERGE)
            elif n_checked == 1:
                group_item.setText(1, "tick at least one more app")
                group_item.setForeground(1, self._DUP_IDLE)
            else:
                group_item.setText(1, "nothing ticked — no change")
                group_item.setForeground(1, self._DUP_IDLE)
        finally:
            self._dup_syncing = prev

    def _update_dup_summary(self):
        plans = self._collect_checked_merges()
        absorbed = sum(len(ids) - 1 for _g, ids in plans)
        if not self._dup_groups:
            self.dup_summary_label.setText("No duplicate groups found." if self.dup_tree.topLevelItemCount() == 0
                                           and getattr(self, "_dup_scanned", False)
                                           else "Press “Scan for duplicates”.")
        elif absorbed:
            self.dup_summary_label.setText(
                f"Ready: {absorbed} app(s) will be merged into the ★ KEEP app of "
                f"{len(plans)} group(s). Review the amber rows, then press Merge.")
        else:
            self.dup_summary_label.setText(
                f"{len(self._dup_groups)} group(s) found. Tick the apps to merge in a group "
                f"(tick the group's box for all of them).")
        self.merge_selected_btn.setEnabled(absorbed > 0)
        self.merge_selected_btn.setText(f"Merge checked apps ({absorbed})" if absorbed else "Merge checked apps")

    def _dup_context_menu(self, pos):
        item = self.dup_tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        if item.parent() is None:                  # group row
            a_all = menu.addAction("Tick all apps in this group")
            a_none = menu.addAction("Untick all apps in this group")
            chosen = menu.exec(self.dup_tree.viewport().mapToGlobal(pos))
            if chosen in (a_all, a_none):
                item.setCheckState(0, Qt.Checked if chosen is a_all else Qt.Unchecked)
            return
        a_keep = menu.addAction("★ Keep this one (merge the other ticked apps into it)")
        chosen = menu.exec(self.dup_tree.viewport().mapToGlobal(pos))
        if chosen is a_keep:
            grp = item.parent()
            data = dict(grp.data(0, Qt.UserRole))
            data["keeper"] = item.data(0, Qt.UserRole)["app_id"]
            self._dup_syncing = True
            grp.setData(0, Qt.UserRole, data)
            self._dup_syncing = False
            item.setCheckState(0, Qt.Checked)      # triggers refresh via _on_dup_item_changed
            self._refresh_dup_group(grp)
            self._update_dup_summary()

    def _on_dup_item_changed(self, item, column):
        """Keeps group box and children consistent, then refreshes the plain-language plan."""
        if column != 0 or self._dup_syncing:
            return
        self._dup_syncing = True
        try:
            if item.parent() is None:
                state = item.checkState(0)
                if state != Qt.PartiallyChecked:
                    for j in range(item.childCount()):
                        item.child(j).setCheckState(0, state)
                grp = item
            else:
                grp = item.parent()
                n = grp.childCount()
                ticked = sum(1 for j in range(n) if grp.child(j).checkState(0) == Qt.Checked)
                grp.setCheckState(0, Qt.Unchecked if ticked == 0
                                  else Qt.Checked if ticked == n else Qt.PartiallyChecked)
        finally:
            self._dup_syncing = False
        self._refresh_dup_group(grp)
        self._update_dup_summary()

    def _collect_checked_merges(self):
        """[(group, [keeper_id, absorbed_id, ...]), ...] for EVERY group with 2+ ticked apps.
        Checkboxes are the only source of truth; the keeper is the ★ one if ticked, otherwise
        the first ticked app. The highlighted row is irrelevant."""
        plans = []
        for i in range(self.dup_tree.topLevelItemCount()):
            gi = self.dup_tree.topLevelItem(i)
            data = gi.data(0, Qt.UserRole)
            if not data:
                continue
            keeper, merging = self._dup_group_state(gi)
            if keeper is not None and merging:
                plans.append((self._dup_groups[data["group_index"]], [keeper] + merging))
        return plans

    def _merge_checked_groups(self):
        plans = self._collect_checked_merges()
        if not plans:
            QMessageBox.information(
                self, "Nothing to merge",
                "Tick at least two apps inside a group, then merge. Every group with two or "
                "more ticked apps is merged into its ★ KEEP app.")
            return
        lines = []
        for group, ids in plans:
            names = {a["id"]: a["name"] for a in group.members}
            lines.append(f"• {', '.join('“' + names[s] + '”' for s in ids[1:])}  →  KEEP “{names[ids[0]]}”")
        if QMessageBox.question(
            self, "Merge checked apps",
            f"Merge in {len(plans)} group(s)?\n\n" + "\n".join(lines) +
            "\n\nThe versions/files of each merged app are re-attached to the KEEP app and the "
            "duplicate catalog entry is removed. No files on disk are touched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        gone = set()      # an app can appear in two groups; never merge one that was already absorbed
        for _group, ids in plans:
            ids = [i for i in ids if i not in gone]
            if len(ids) < 2:
                continue
            target = ids[0]
            for sid in ids[1:]:
                merge_apps(self.db, sid, target)
                gone.add(sid)
        self._dup_scanned = True
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
        v.setSpacing(6)

        intro = QLabel(
            "<b>Moves or copies</b> every app version into "
            "<code>&lt;destination&gt;/Catalog/Subcatalog/AppName/Version/</code> "
            "(Portable-tagged apps go under <code>Portable/…</code>).<br>"
            "• A version's own folder moves as one unit, so readme / crack / keygen / serial / skins travel with it.<br>"
            "• Installers <b>sharing</b> a folder: only that version's own file moves; unclaimed sidecars "
            "(Patch/, Crack/, Tutorial/ …) are <b>copied</b> alongside. Other apps' folders are never copied "
            "— they move on their own row.<br>"
            "• Only <b>bare installers</b> (.exe / .msi …) are compressed — that one file, never the folder.<br>"
            "• <b>Preview first</b>: the table shows what each row will really write. "
            "Nothing changes until you press Execute."
        )
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        v.addWidget(intro)

        # --- setup: destination / options / password / portable / preview, 3 compact rows ---
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        self.dest_edit = QLineEdit()
        dest_row.addWidget(self.dest_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_dest)
        dest_row.addWidget(browse_btn)
        v.addLayout(dest_row)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Transfer:"))
        self.reorg_copy_mode_combo = QComboBox()
        self.reorg_copy_mode_combo.addItem("Move (default)", "move")
        self.reorg_copy_mode_combo.addItem("Copy (originals stay in place)", "copy")
        self.reorg_copy_mode_combo.currentIndexChanged.connect(self._refresh_preview_sizes)
        opts_row.addWidget(self.reorg_copy_mode_combo)
        opts_row.addSpacing(12)
        opts_row.addWidget(QLabel("Archive as:"))
        self.reorg_archive_combo = QComboBox()
        self.reorg_archive_combo.addItem("None -- leave as folder", "none")
        self.reorg_archive_combo.addItem("7z (recommended)", "7z")
        self.reorg_archive_combo.addItem("Zip", "zip")
        self.reorg_archive_combo.addItem("RAR (needs rar/WinRAR on PATH)", "rar")
        opts_row.addWidget(self.reorg_archive_combo)
        opts_row.addSpacing(12)

        _s = self.db.get_all_settings()
        self.reorg_pw_check = QCheckBox("Password")
        self.reorg_pw_check.setChecked(bool(_s.get("archive_password_enabled", False)))
        self.reorg_pw_edit = QLineEdit(_s.get("archive_password") or "")
        self.reorg_pw_edit.setPlaceholderText("password")
        self.reorg_pw_edit.setToolTip(
            "The password is also added to each archive's filename in brackets,\n"
            "e.g. Setup.exe -> Setup(password).7z. Avoid \\ / : * ? \" < > | ( )\n"
            "-- they can't be used in a filename. Zip encryption needs pyzipper\n"
            "or a 7z binary on PATH; 7z needs py7zr; RAR needs rar on PATH.")
        opts_row.addWidget(self.reorg_pw_check)
        opts_row.addWidget(self.reorg_pw_edit, 1)
        v.addLayout(opts_row)

        def _sync_pw_enabled(*_):
            archiving = self.reorg_archive_combo.currentData() != "none"
            self.reorg_pw_check.setEnabled(archiving)
            self.reorg_pw_edit.setEnabled(archiving and self.reorg_pw_check.isChecked())
        self.reorg_archive_combo.currentIndexChanged.connect(_sync_pw_enabled)
        self.reorg_pw_check.toggled.connect(_sync_pw_enabled)
        _sync_pw_enabled()

        prev_row = QHBoxLayout()
        self.reorg_portable_check = QCheckBox(
            "Route Portable-tagged apps into a \"Portable\" category (original catalog becomes the subcategory)")
        self.reorg_portable_check.setChecked(True)
        prev_row.addWidget(self.reorg_portable_check, 1)
        self.reorg_preview_btn = QPushButton("Preview (dry run -- changes nothing)")
        self.reorg_preview_btn.clicked.connect(self._preview_reorganize)
        prev_row.addWidget(self.reorg_preview_btn)
        v.addLayout(prev_row)

        self.reorg_status_label = QLabel(
            "Choose a destination and press Preview. Nothing is changed until you press Execute.")
        self.reorg_status_label.setWordWrap(True)
        v.addWidget(self.reorg_status_label)

        # --- plan table (top) and live panel (bottom) share a draggable splitter ---------
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        self.reorg_table = QTableWidget(0, 7)
        self.reorg_table.setHorizontalHeaderLabels(
            ["Status", "Source folder", "Destination", "Portable?", "Shared?", "Will write", "Notes"])
        self.reorg_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.reorg_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.reorg_table.setWordWrap(False)
        # Every column is user-resizable (drag a header divider; double-click a
        # divider to fit that column to its contents). The last column takes any
        # spare room; if the columns are wider than the window the table scrolls
        # sideways. Widths are remembered between sessions.
        hh = self.reorg_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(40)
        hh.setHighlightSections(False)
        self.reorg_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.reorg_table.setTextElideMode(Qt.ElideMiddle)     # keep both ends of a long path visible
        default_widths = [90, 380, 380, 80, 70, 110, 320]
        saved_widths = None
        try:
            saved_widths = self.db.get_setting("reorg_table_col_widths", None)
        except Exception:
            pass
        widths = (saved_widths if isinstance(saved_widths, list)
                  and len(saved_widths) == len(default_widths) else default_widths)
        for col, col_w in enumerate(widths):     # (not `w`: that is this tab's page widget)
            try:
                self.reorg_table.setColumnWidth(col, max(40, int(col_w)))
            except (TypeError, ValueError):
                self.reorg_table.setColumnWidth(col, default_widths[col])
        self._reorg_width_timer = QTimer(self)
        self._reorg_width_timer.setSingleShot(True)
        self._reorg_width_timer.timeout.connect(self._save_reorg_col_widths)
        hh.sectionResized.connect(lambda *_: self._reorg_width_timer.start(600))
        self.reorg_table.setMinimumHeight(90)
        splitter.addWidget(self.reorg_table)

        live = QGroupBox("Live status")
        lv = QVBoxLayout(live)
        lv.setContentsMargins(6, 4, 6, 6)
        lv.setSpacing(4)

        # the rolling log takes ALL the space (same detailed lines the terminal prints)
        self.reorg_activity_log = QPlainTextEdit()
        self.reorg_activity_log.setReadOnly(True)
        self.reorg_activity_log.setMaximumBlockCount(5000)
        self.reorg_activity_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.reorg_activity_log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.reorg_activity_log.setPlaceholderText("Every step of a run scrolls past here, line for line as in the terminal…")
        lv.addWidget(self.reorg_activity_log, 1)

        # ...and ONE thin status bar underneath: progress bars + the current step on a single line
        status_bar = QHBoxLayout()
        status_bar.setSpacing(8)
        self.reorg_overall_bar = QProgressBar()
        self.reorg_overall_bar.setFixedSize(150, 16)
        self.reorg_overall_bar.setFormat("%v / %m items")
        self.reorg_overall_bar.setToolTip("Items finished out of the whole plan")
        status_bar.addWidget(self.reorg_overall_bar)
        self.reorg_current_bar = QProgressBar()
        self.reorg_current_bar.setFixedSize(120, 16)
        self.reorg_current_bar.setRange(0, 1)
        self.reorg_current_bar.setValue(0)
        self.reorg_current_bar.setTextVisible(False)
        self.reorg_current_bar.setToolTip("Progress of the file/folder being copied or compressed right now")
        status_bar.addWidget(self.reorg_current_bar)
        self.reorg_phase_label = _ElidedLabel("Idle")
        status_bar.addWidget(self.reorg_phase_label, 1)
        self.reorg_time_label = QLabel("")
        status_bar.addWidget(self.reorg_time_label)
        lv.addLayout(status_bar)

        live.setMinimumHeight(110)
        splitter.addWidget(live)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.reorg_splitter = splitter
        v.addWidget(splitter, 1)

        # --- run controls ------------------------------------------------------------
        btn_row = QHBoxLayout()
        self.execute_btn = QPushButton("Execute previewed plan")
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self._execute_reorganize)
        btn_row.addWidget(self.execute_btn, 1)
        self.reorg_cancel_btn = QPushButton("Cancel run")
        self.reorg_cancel_btn.setEnabled(False)
        self.reorg_cancel_btn.setToolTip(
            "Stops after the current few MB. A half-written copy is deleted again; "
            "originals are never touched until a copy is complete. A 7z/RAR compression "
            "already in progress finishes its one file first.")
        self.reorg_cancel_btn.clicked.connect(self._cancel_reorganize)
        btn_row.addWidget(self.reorg_cancel_btn)
        v.addLayout(btn_row)

        self._reorg_plans = []
        self._reorg_worker = None
        self._reorg_run_started = 0.0
        self._reorg_last_event = 0.0
        self._reorg_last_msg = ""
        self._reorg_last_copied = 0
        self._reorg_timer = QTimer(self)
        self._reorg_timer.setInterval(1000)
        self._reorg_timer.timeout.connect(self._tick_reorg_clock)
        return w

    def _rebalance_reorg_splitter(self, table_share: float):
        """Gives the plan table `table_share` (0-1) of the height, the live panel the rest."""
        total = sum(self.reorg_splitter.sizes()) or 1
        self.reorg_splitter.setSizes([int(total * table_share), int(total * (1 - table_share))])

    def _reset_current_bar(self):
        self.reorg_current_bar.setRange(0, 1)
        self.reorg_current_bar.setValue(0)
        self.reorg_current_bar.setTextVisible(False)

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose destination folder")
        if folder:
            self.dest_edit.setText(folder)

    # ---- preview ----------------------------------------------------------

    _GB = 1024 ** 3

    def _save_reorg_col_widths(self):
        """Remember the preview table's column widths (debounced; best effort)."""
        try:
            widths = [self.reorg_table.columnWidth(c) for c in range(self.reorg_table.columnCount())]
            self.db.set_setting("reorg_table_col_widths", widths, bump_version=False)
        except Exception:
            logging.getLogger("appcatalog.organizer").debug("could not save reorganize column widths", exc_info=True)

    def _plan_notes(self, p, copy_mode: str) -> str:
        notes = []
        if p.collision:
            notes.append("destination already has content")
        if p.container_folder:
            notes.append("folder also contains other apps -- own file only")
        elif p.shared_folder:
            notes.append("shared folder -- own file only")
        if p.shared_extra_paths:
            notes.append(f"{len(p.shared_extra_paths)} sidecar item(s) ({fmt_bytes(p.extras_bytes)}) "
                         f"copied alongside, originals stay")
        if p.skipped_items:
            names = ", ".join(os.path.basename(x[0]) for x in p.skipped_items[:4])
            more = f" +{len(p.skipped_items) - 4} more" if len(p.skipped_items) > 4 else ""
            notes.append(f"left in place: {names}{more}")
        if p.cross_volume and copy_mode == "move":
            notes.append("DIFFERENT DRIVE: real copy + delete, not an instant rename")
        return "; ".join(notes)

    def _refresh_preview_sizes(self, *_):
        if not self._reorg_plans or self._reorg_worker is not None:
            return
        mode = self.reorg_copy_mode_combo.currentData()
        total = 0
        for i, p in enumerate(self._reorg_plans):
            n = plan_bytes_to_write(p, mode)
            total += n
            it = QTableWidgetItem("instant (rename)" if n == 0 else fmt_bytes(n))
            if n >= self._GB:
                it.setForeground(QColor("#e0a030"))
            self.reorg_table.setItem(i, 5, it)
            note = QTableWidgetItem(self._plan_notes(p, mode))
            note.setToolTip(note.text().replace('; ', '\n'))
            self.reorg_table.setItem(i, 6, note)
        self._reorg_write_total = total
        self._update_preview_summary()

    def _update_preview_summary(self):
        plans = self._reorg_plans
        mode = self.reorg_copy_mode_combo.currentData()
        portable = sum(1 for p in plans if p.is_portable)
        shared = sum(1 for p in plans if p.shared_folder)
        collisions = sum(1 for p in plans if p.collision)
        left = sum(len(p.skipped_items) for p in plans)
        text = (f"{len(plans)} item(s) planned -- {portable} portable, {shared} sharing a folder, "
                f"{collisions} destination(s) already have content (never overwritten). "
                f"Data that will be physically written: {fmt_bytes(self._reorg_write_total)}"
                f"{' (everything else is an instant rename)' if mode == 'move' else ''}.")
        if left:
            text += f"  {left} folder/file(s) deliberately left in place (see Notes)."
        self.reorg_status_label.setText(text)

    def _preview_reorganize(self):
        dest = self.dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return
        self.reorg_status_label.setText("Scanning folders and measuring sizes… (nothing is changed)")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            self._reorg_plans = preview_reorganize(
                self.db, dest,
                portable_to_dedicated_category=self.reorg_portable_check.isChecked(),
            )
        finally:
            QApplication.restoreOverrideCursor()
        mode = self.reorg_copy_mode_combo.currentData()
        self.reorg_table.setRowCount(len(self._reorg_plans))
        for i, p in enumerate(self._reorg_plans):
            self.reorg_table.setItem(i, 0, QTableWidgetItem("Planned"))
            src_item = QTableWidgetItem(p.source_path)
            src_item.setToolTip(p.source_path)           # full path when the column is narrow
            dst_item = QTableWidgetItem(p.dest_path)
            dst_item.setToolTip(p.dest_path)
            self.reorg_table.setItem(i, 1, src_item)
            self.reorg_table.setItem(i, 2, dst_item)
            self.reorg_table.setItem(i, 3, QTableWidgetItem("Yes" if p.is_portable else ""))
            self.reorg_table.setItem(i, 4, QTableWidgetItem("Yes" if p.shared_folder else ""))
        self._reorg_write_total = 0
        self._refresh_preview_sizes()
        self.reorg_overall_bar.setRange(0, max(len(self._reorg_plans), 1))
        self.reorg_overall_bar.setValue(0)
        self.reorg_phase_label.setText("Idle -- review the plan, then Execute")
        self.reorg_time_label.setText("")
        self.reorg_activity_log.clear()
        self.execute_btn.setEnabled(len(self._reorg_plans) > 0)
        self._rebalance_reorg_splitter(0.62)

    # ---- execute ----------------------------------------------------------

    def _execute_reorganize(self):
        if not self._reorg_plans:
            return
        copy_mode = self.reorg_copy_mode_combo.currentData()
        archive_format = self.reorg_archive_combo.currentData()
        archive_password = None
        if archive_format != "none" and self.reorg_pw_check.isChecked():
            archive_password = self.reorg_pw_edit.text()
            pw_err = validate_archive_password(archive_password)
            if pw_err:
                QMessageBox.warning(self, "Archive password", pw_err)
                return

        to_write = sum(plan_bytes_to_write(p, copy_mode) for p in self._reorg_plans)
        # Free-space check on the destination drive before touching anything.
        try:
            probe = self.dest_edit.text().strip()
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            free = shutil.disk_usage(probe).free if probe else None
        except OSError:
            free = None
        if free is not None and to_write > free * 0.98:
            QMessageBox.critical(
                self, "Not enough free space",
                f"This plan will write about {fmt_bytes(to_write)}, but the destination drive has "
                f"only {fmt_bytes(free)} free. Nothing has been changed.")
            return

        verb = "copy" if copy_mode == "copy" else "move"
        lines = [f"This will {verb} {len(self._reorg_plans)} item(s) on disk.",
                 f"Data physically written: {fmt_bytes(to_write)}"
                 + (f"  (destination has {fmt_bytes(free)} free)" if free is not None else "") + "."]
        if archive_format != "none":
            lines.append(f"Each bare installer (.exe/.msi/...) is then compressed to .{archive_format}, "
                         f"that one file only; everything else is left as it is.")
        if archive_password:
            lines.append("Archives are password-protected; the password is added to each archive's "
                         f"filename, e.g. Setup(password).{archive_format}.")
        lines.append("You can Cancel at any time; a half-written copy is removed again.")
        lines.append("\nContinue?")
        if QMessageBox.question(self, "Confirm reorganize", "\n".join(lines),
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        self.execute_btn.setEnabled(False)
        self.reorg_preview_btn.setEnabled(False)
        self.reorg_cancel_btn.setEnabled(True)
        self.reorg_cancel_btn.setText("Cancel run")
        self.close_btn.setEnabled(False)          # see closeEvent() -- closing mid-run is unsafe
        self.reorg_activity_log.clear()
        self.reorg_overall_bar.setRange(0, len(self._reorg_plans))
        self.reorg_overall_bar.setValue(0)
        for i in range(self.reorg_table.rowCount()):
            self._set_row_status(i, "Waiting", None)
        self.reorg_phase_label.setText("Starting…")
        self._attach_run_log()
        self._reorg_run_started = self._reorg_last_event = time.monotonic()
        self._reorg_last_msg = ""
        self._reorg_last_copied = 0
        self._reorg_timer.start()
        self._rebalance_reorg_splitter(0.35)

        self._reorg_worker = _ReorganizeWorker(
            self.db, self._reorg_plans, copy_mode, archive_format,
            archive_password=archive_password, parent=self)
        self._reorg_worker.reorg_event.connect(self._on_reorg_event)
        self._reorg_worker.finished_ok.connect(self._on_reorg_finished)
        self._reorg_worker.failed.connect(self._on_reorg_failed)
        self._reorg_worker.start()

    def _cancel_reorganize(self):
        if self._reorg_worker is not None and self._reorg_worker.isRunning():
            self._reorg_worker.cancel()
            self.reorg_cancel_btn.setEnabled(False)
            self.reorg_cancel_btn.setText("Cancelling…")
            self.reorg_phase_label.setText("Cancelling -- stopping after the current step…")
            self._log_reorg("Cancel requested -- finishing the current step, then stopping")

    # ---- live updates -----------------------------------------------------

    _STATUS_STYLE = {
        "Running": None, "Waiting": None,
        "Moved": "#4caf50", "Copied": "#4caf50",
        "Skipped": "#e0a030", "Cancelled": "#e0a030", "Not started": "#909090",
        "Failed": "#e05555",
    }

    def _set_row_status(self, row: int, text: str, tooltip: Optional[str]):
        if not (0 <= row < self.reorg_table.rowCount()):
            return
        it = QTableWidgetItem(text)
        colour = self._STATUS_STYLE.get(text)
        if colour:
            it.setForeground(QColor(colour))
        if tooltip:
            it.setToolTip(tooltip)
        self.reorg_table.setItem(row, 0, it)

    def _append_log_line(self, text: str, level: int = logging.INFO):
        """Adds one line to the rolling log, colour-coded: red = failure, amber = warning /
        skipped / cancelled, green = item finished, bold = start of an item or the run."""
        colour, bold = None, False
        if level >= logging.ERROR or " FAILED" in text:
            colour = "#e05555"
        elif level >= logging.WARNING or any(k in text for k in (" SKIPPED", " CANCELLED", "still working")):
            colour = "#e0a030"
        elif " MOVED " in text or " COPIED " in text:
            colour = "#4caf50"
        elif "] START " in text or "REORGANIZE " in text or "PLAN OVERVIEW" in text:
            bold = True
        body = html.escape(text).replace(" ", "&nbsp;").replace("\n", "<br>")
        if bold:
            body = f"<b>{body}</b>"
        if colour:
            body = f'<span style="color:{colour}">{body}</span>'
        self.reorg_activity_log.appendHtml(body)

    def _log_reorg(self, text: str):
        """A GUI-side line (things the engine can't know about, e.g. the Cancel click)."""
        self._append_log_line(f"{time.strftime('%H:%M:%S')}  {text}")

    def _attach_run_log(self):
        """Mirror the engine's INFO log lines into the rolling box for the duration of a run."""
        self._log_bridge = _LogBridge(self)
        self._log_bridge.line.connect(self._append_log_line)
        self._log_handler = _QtLogHandler(self._log_bridge)
        lg = logging.getLogger("appcatalog.organizer")
        self._log_prev_level = lg.level
        if lg.getEffectiveLevel() > logging.INFO:
            lg.setLevel(logging.INFO)
        lg.addHandler(self._log_handler)

    def _detach_run_log(self):
        handler = getattr(self, "_log_handler", None)
        if handler is not None:
            lg = logging.getLogger("appcatalog.organizer")
            lg.removeHandler(handler)
            lg.setLevel(self._log_prev_level)
            self._log_handler = None

    def _on_reorg_event(self, ev):
        """Drives the thin status bar and the table row. (The rolling log gets its lines from
        the engine's own console log, see _attach_run_log.)"""
        self._reorg_last_event = time.monotonic()
        self._reorg_last_copied = ev.bytes_copied_total
        plan = ev.plan
        label = f"{plan.app_name} {plan.version}".strip() or os.path.basename(plan.source_path)
        row = ev.index - 1
        where = f"{label}\n{plan.source_path}  →  {plan.dest_path}"
        self.reorg_overall_bar.setMaximum(max(ev.total, 1))
        self.reorg_overall_bar.setValue(ev.index if ev.final else ev.index - 1)

        if ev.final:
            names = {"moved": "Moved", "copied": "Copied", "failed": "Failed",
                     "skipped_collision": "Skipped", "cancelled": "Cancelled"}
            text = names.get(ev.status, ev.status)
            self._set_row_status(row, text, ev.message)
            self._reset_current_bar()
            status = f"[{ev.index}/{ev.total}] {text}: {label} -- {ev.message}"
            self.reorg_phase_label.setText(status)
            self.reorg_phase_label.setToolTip(f"{where}\n{status}")
        else:
            if ev.phase == "start":
                self._set_row_status(row, "Running", None)
                self.reorg_table.selectRow(row)
                self.reorg_table.scrollToItem(self.reorg_table.item(row, 0))
            status = f"[{ev.index}/{ev.total}] {ev.message}"
            if ev.detail and ev.phase != "start":
                status += f"   —   {ev.detail}"
            self.reorg_phase_label.setText(status)
            self.reorg_phase_label.setToolTip(f"{where}\n{status}")
            if ev.bytes_total > 0:
                self.reorg_current_bar.setTextVisible(True)
                self.reorg_current_bar.setRange(0, 1000)
                self.reorg_current_bar.setValue(int(1000 * min(ev.bytes_done, ev.bytes_total) / ev.bytes_total))
            else:
                self.reorg_current_bar.setTextVisible(False)
                self.reorg_current_bar.setRange(0, 0)       # busy indicator: size unknown
        self._tick_reorg_clock()

    def _tick_reorg_clock(self):
        if self._reorg_worker is None:
            return
        now = time.monotonic()
        elapsed = now - self._reorg_run_started
        quiet = now - self._reorg_last_event
        text = f"{fmt_duration(elapsed)}  ·  {fmt_bytes(self._reorg_last_copied)} written  ·  updated {quiet:.0f}s ago"
        if quiet > 20:
            text += "  ·  still working"
            self.reorg_time_label.setStyleSheet("color: #e0a030;")
        else:
            self.reorg_time_label.setStyleSheet("")
        self.reorg_time_label.setText(text)

    def _finish_run_ui(self):
        self._reorg_timer.stop()
        self._detach_run_log()
        self._reset_current_bar()
        self.reorg_cancel_btn.setEnabled(False)
        self.reorg_cancel_btn.setText("Cancel run")
        self.reorg_preview_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        self._reorg_worker = None

    def _on_reorg_finished(self, result: "ReorganizeResult"):
        mode = self.reorg_copy_mode_combo.currentData()
        # rows that never ran (cancel) -> visibly "Not started"
        for i, e in enumerate(result.entries):
            if e.get("status") == "not_started":
                self._set_row_status(i, "Not started", None)
        summary = (
            f"{'Moved' if mode == 'move' else 'Copied'}: {result.moved}\n"
            f"Archived: {result.archived}\n"
            f"Skipped (destination collision): {result.skipped_collisions}\n"
            f"Failed: {len(result.failed)}\n"
            f"Data written: {fmt_bytes(result.bytes_copied)} in {fmt_duration(result.elapsed_seconds)}"
        )
        if result.cancelled:
            summary = f"CANCELLED -- {result.not_started} item(s) were not started.\n" + summary
        self.reorg_phase_label.setText("Cancelled." if result.cancelled else "Done.")
        self.reorg_overall_bar.setValue(self.reorg_overall_bar.maximum() if not result.cancelled
                                        else self.reorg_overall_bar.value())
        self.reorg_status_label.setText(summary.replace("\n", "   |   "))
        self._finish_run_ui()
        self.execute_btn.setEnabled(False)
        self._reorg_plans = []       # a finished/cancelled plan is stale -- preview again for a new one

        report_note = ""
        if result.html_report_path and os.path.exists(result.html_report_path):
            try:
                webbrowser.open(Path(result.html_report_path).as_uri())
                report_note = "\n\nA detailed report has been opened in your browser."
            except Exception as e:
                report_note = f"\n\nDetailed report: {result.html_report_path}\n(Could not auto-open it: {e})"
        elif result.move_log_path:
            report_note = f"\n\nMove log: {result.move_log_path}"
        QMessageBox.information(self, "Reorganize cancelled" if result.cancelled else "Reorganize complete",
                                summary + report_note)

    def _on_reorg_failed(self, message: str):
        self._log_reorg(f"UNEXPECTED ERROR: {message}")
        self.reorg_phase_label.setText("Failed -- see message.")
        self._finish_run_ui()
        self.execute_btn.setEnabled(True)
        QMessageBox.critical(self, "Reorganize failed", f"An unexpected error stopped the run:\n\n{message}")
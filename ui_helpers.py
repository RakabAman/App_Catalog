"""
Small UI helpers shared by the GUI modules.

install_scrollable_messageboxes(app)
    Long QMessageBox texts (import / merge / report summaries ...) used to produce
    boxes wider or taller than the screen. With this installed, any message box whose
    text is long gets its text placed in a scroll area: it wraps at the window width,
    scrolls vertically, and stays selectable / copyable. Short boxes are untouched.
    Works for QMessageBox.information/warning/question/critical and for boxes built
    by hand, because it hooks the box when it is first shown.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QScrollArea, QSizePolicy

_LONG_CHARS = 260       # more characters than this -> scroll
_LONG_LINES = 9         # more lines than this -> scroll
_LONG_LINE_WIDTH = 90   # any single line wider than this -> scroll


def _plain(text: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>", "\n", text or "")
    return re.sub(r"<[^>]+>", "", text)


def text_is_long(text: str) -> bool:
    plain = _plain(text)
    lines = plain.splitlines() or [""]
    return (len(plain) > _LONG_CHARS or len(lines) > _LONG_LINES
            or max(len(l) for l in lines) > _LONG_LINE_WIDTH)


class _ScrollableBoxFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and isinstance(obj, QMessageBox):
            if not obj.property("_scroll_done"):
                obj.setProperty("_scroll_done", True)
                try:
                    self._make_scrollable(obj)
                except Exception:
                    pass            # never break a message box over cosmetics
        return False

    @staticmethod
    def _make_scrollable(box: QMessageBox) -> None:
        label = box.findChild(QLabel, "qt_msgbox_label")
        if label is None or not text_is_long(label.text()):
            return
        grid = box.layout()
        idx = grid.indexOf(label)
        if idx < 0:
            return
        row, col, rspan, cspan = grid.getItemPosition(idx)
        grid.removeWidget(label)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
                                      | Qt.LinksAccessibleByMouse)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setMinimumWidth(0)
        label.setMaximumWidth(16777215)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(label)
        scroll.setMinimumSize(560, 280)
        scroll.setMaximumHeight(520)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        grid.addWidget(scroll, row, col, rspan, cspan)
        # the box was sized for the huge label before we got here -> size it again
        box.setMinimumWidth(640)
        box.setMaximumWidth(1000)
        box.setMaximumHeight(720)
        box.layout().activate()
        box.adjustSize()


_filter = None


def install_scrollable_messageboxes(app: QApplication | None = None) -> None:
    """Idempotent; call once after the QApplication exists."""
    global _filter
    if _filter is not None:
        return
    app = app or QApplication.instance()
    if app is None:
        return
    _filter = _ScrollableBoxFilter(app)
    app.installEventFilter(_filter)

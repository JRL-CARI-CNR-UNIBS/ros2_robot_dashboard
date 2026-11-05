"""Small reusable widgets and helpers."""

from __future__ import annotations

import math
import traceback
from typing import Any, Callable, Iterable, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTableView,
                               QToolButton, QWidget)

from .. import theme


# --------------------------------------------------------------------------- formatting

def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps) + '/s'


def fmt_hz(hz: Optional[float]) -> str:
    if hz is None:
        return ''
    if hz >= 100:
        return f'{hz:.0f} Hz'
    if hz >= 10:
        return f'{hz:.1f} Hz'
    return f'{hz:.2f} Hz'


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return ''
    if seconds < 1:
        return f'{seconds * 1000:.0f} ms'
    if seconds < 120:
        return f'{seconds:.1f} s'
    return f'{seconds / 60:.0f} min'


def fmt_float(v: Optional[float], digits: int = 4) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return f'{v:.{digits}f}'


def copy_to_clipboard(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def refilter(proxy, change) -> None:
    """Apply ``change()`` and re-run the filter (Qt >= 6.10 API when available)."""
    begin = getattr(proxy, 'beginFilterChange', None)
    if begin is not None:
        begin()
        change()
        proxy.endFilterChange()
    else:
        change()
        proxy.invalidateFilter()


# --------------------------------------------------------------------------- dialogs

def is_long_text(text: str, max_chars: int = 300, max_line: int = 110) -> bool:
    """Too long for a message box label (it would make the dialog huge)."""
    return len(text) > max_chars or any(len(line) > max_line for line in text.splitlines())


def confirm(parent: QWidget, title: str, text: str, details: str = '',
            ok_text: str = 'Confirm', danger: bool = True) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if details and is_long_text(details):
        # Long commands go in the expandable details, not in the label.
        box.setInformativeText(details[:100].splitlines()[0] + '…  (see "Show Details")')
        box.setDetailedText(details)
    elif details:
        box.setInformativeText(details)
    ok = box.addButton(ok_text, QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    box.exec()
    return box.clickedButton() is ok


def show_error(parent: QWidget, title: str, error: Any) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle(title)
    if isinstance(error, BaseException):
        text = f'{type(error).__name__}: {error}'
        details = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    else:
        text, details = str(error), ''
    if is_long_text(text):
        details = text + ('\n\n' + details if details else '')
        text = text[:200].splitlines()[0] + '…  (see "Show Details")'
    box.setText(text)
    if details:
        box.setDetailedText(details)
    box.exec()


def info(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


# --------------------------------------------------------------------------- widgets

class ElidedLabel(QLabel):
    """Single-line plain-text label that elides instead of widening the window.

    The full text is in the tooltip.
    """

    def __init__(self, text: str = '', parent: Optional[QWidget] = None,
                 mode=Qt.ElideRight):
        super().__init__(parent)
        self._full = ''
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text: str) -> None:       # noqa: N802 - Qt naming
        self._full = text or ''
        self.setToolTip(self._full if self._full else '')
        self._elide()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self):                          # noqa: N802
        hint = super().sizeHint()
        hint.setWidth(min(self.fontMetrics().horizontalAdvance(self._full) + 8, 600))
        return hint

    def minimumSizeHint(self):                   # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(40)
        return hint

    def resizeEvent(self, event) -> None:        # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        one_line = self._full.replace('\n', '  ')
        super().setText(self.fontMetrics().elidedText(one_line, self._mode,
                                                      max(self.width() - 4, 20)))


class FilterEdit(QLineEdit):
    def __init__(self, placeholder: str = 'Filter…', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)


class StatusDot(QLabel):
    """A colored dot with an optional text."""

    def __init__(self, text: str = '', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.set_state('stale', text)

    def set_state(self, state: str, text: str = '') -> None:
        c = theme.color(state if state in theme.COLORS else 'stale').name()
        self.setText(f'<span style="color:{c}">●</span> {text}')


def button(text: str, slot: Optional[Callable] = None, tooltip: str = '',
           icon=None, checkable: bool = False) -> QPushButton:
    b = QPushButton(text)
    if icon is not None:
        b.setIcon(icon)
    if tooltip:
        b.setToolTip(tooltip)
    b.setCheckable(checkable)
    if slot is not None:
        (b.toggled if checkable else b.clicked).connect(slot)
    return b


def tool_button(text: str, slot: Optional[Callable] = None, tooltip: str = '',
                icon=None, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    if icon is not None:
        b.setIcon(icon)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    if tooltip:
        b.setToolTip(tooltip)
    b.setCheckable(checkable)
    if slot is not None:
        (b.toggled if checkable else b.clicked).connect(slot)
    return b


def hbox(*widgets, stretch_last: bool = False, margins: int = 0) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(margins, margins, margins, margins)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif hasattr(w, 'addWidget') and not isinstance(w, QWidget):
            lay.addLayout(w)
        else:
            lay.addWidget(w)
    if stretch_last:
        lay.addStretch(1)
    return lay


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w


class CheckableComboBox(QComboBox):
    """Combo box with checkable items (multi-selection)."""

    selection_changed = Signal(list)

    def __init__(self, placeholder: str = 'All', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._placeholder = placeholder
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText(placeholder)
        self._model.itemChanged.connect(self._changed)
        self.view().pressed.connect(self._toggle)

    def set_items(self, items: Iterable[str]) -> None:
        checked = set(self.checked())
        self._model.blockSignals(True)
        self._model.clear()
        for text in items:
            it = QStandardItem(text)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            it.setData(Qt.Checked if text in checked else Qt.Unchecked, Qt.CheckStateRole)
            self._model.appendRow(it)
        self._model.blockSignals(False)
        self._update_text()

    def _toggle(self, index) -> None:
        it = self._model.itemFromIndex(index)
        it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def checked(self) -> list[str]:
        return [self._model.item(i).text() for i in range(self._model.rowCount())
                if self._model.item(i).checkState() == Qt.Checked]

    def clear_checks(self) -> None:
        for i in range(self._model.rowCount()):
            self._model.item(i).setCheckState(Qt.Unchecked)

    def _changed(self, _item) -> None:
        self._update_text()
        self.selection_changed.emit(self.checked())

    def _update_text(self) -> None:
        sel = self.checked()
        self.lineEdit().setText('' if not sel else (sel[0] if len(sel) == 1 else
                                                    f'{len(sel)} selected'))

    def hidePopup(self) -> None:   # keep the popup open while checking items
        if not self.view().underMouse():
            super().hidePopup()


# --------------------------------------------------------------------------- table model

class Column:
    def __init__(self, header: str, value: Callable[[Any], Any],
                 fg: Optional[Callable[[Any], Optional[QColor]]] = None,
                 bg: Optional[Callable[[Any], Optional[QColor]]] = None,
                 tooltip: Optional[Callable[[Any], str]] = None,
                 sort: Optional[Callable[[Any], Any]] = None,
                 align: Optional[Qt.AlignmentFlag] = None):
        self.header = header
        self.value = value
        self.fg = fg
        self.bg = bg
        self.tooltip = tooltip
        self.sort = sort
        self.align = align


SORT_ROLE = Qt.UserRole + 1
ROW_ROLE = Qt.UserRole + 2


class RowsModel(QAbstractTableModel):
    """Table model over a list of objects; ``key`` keeps rows stable across updates."""

    def __init__(self, columns: Sequence[Column], key: Callable[[Any], Any] = id,
                 parent=None):
        super().__init__(parent)
        self.columns = list(columns)
        self.rows: list = []
        self._key = key

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section].header
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = self.columns[index.column()]
        if role == Qt.DisplayRole:
            v = col.value(row)
            return '' if v is None else (v if isinstance(v, str) else str(v))
        if role == SORT_ROLE:
            if col.sort is not None:
                return col.sort(row)
            v = col.value(row)
            return v if isinstance(v, (int, float)) else ('' if v is None else str(v))
        if role == ROW_ROLE:
            return row
        if role == Qt.ForegroundRole and col.fg is not None:
            return col.fg(row)
        if role == Qt.BackgroundRole and col.bg is not None:
            return col.bg(row)
        if role == Qt.ToolTipRole and col.tooltip is not None:
            return col.tooltip(row)
        if role == Qt.TextAlignmentRole and col.align is not None:
            return int(col.align | Qt.AlignVCenter)
        return None

    def set_rows(self, rows: Iterable) -> None:
        rows = list(rows)
        if [self._key(r) for r in rows] == [self._key(r) for r in self.rows]:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0),
                                      self.index(len(rows) - 1, len(self.columns) - 1))
            return
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def row_at(self, index: QModelIndex):
        if not index.isValid():
            return None
        if isinstance(index.model(), QSortFilterProxyModel):
            index = index.model().mapToSource(index)
        return self.rows[index.row()] if 0 <= index.row() < len(self.rows) else None


class TextFilterProxy(QSortFilterProxyModel):
    """Case-insensitive substring filter over all columns, numeric-aware sorting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ''
        self._extra: Optional[Callable[[Any], bool]] = None
        self.setSortRole(SORT_ROLE)

    def set_text(self, text: str) -> None:
        refilter(self, lambda: setattr(self, '_text', text.strip().lower()))

    def set_predicate(self, fn: Optional[Callable[[Any], bool]]) -> None:
        refilter(self, lambda: setattr(self, '_extra', fn))

    def filterAcceptsRow(self, row, parent) -> bool:
        m = self.sourceModel()
        if self._extra is not None:
            obj = m.index(row, 0, parent).data(ROW_ROLE)
            if obj is not None and not self._extra(obj):
                return False
        if not self._text:
            return True
        for c in range(m.columnCount(parent)):
            v = m.index(row, c, parent).data(Qt.DisplayRole)
            if v and self._text in str(v).lower():
                return True
        return False

    def lessThan(self, left, right) -> bool:
        a, b = left.data(SORT_ROLE), right.data(SORT_ROLE)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


def make_table(model, proxy: Optional[QSortFilterProxyModel] = None,
               stretch_last: bool = True, selection=QAbstractItemView.ExtendedSelection) \
        -> QTableView:
    view = QTableView()
    view.setModel(proxy if proxy is not None else model)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(selection)
    view.setSortingEnabled(proxy is not None)
    if proxy is not None:
        view.sortByColumn(0, Qt.AscendingOrder)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(22)
    view.horizontalHeader().setStretchLastSection(stretch_last)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    view.setWordWrap(False)
    return view


def selected_rows(view: QTableView, model: RowsModel) -> list:
    rows = []
    for idx in view.selectionModel().selectedRows():
        r = model.row_at(idx)
        if r is not None:
            rows.append(r)
    return rows


def labeled(text: str, widget: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(QLabel(text))
    lay.addWidget(widget, 1)
    return w

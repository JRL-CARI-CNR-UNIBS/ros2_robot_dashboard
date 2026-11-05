"""Append-only log model with a row cap (fast enough for busy /rosout)."""

from __future__ import annotations

import datetime
import time
from typing import Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from ....core.domain.logs import LogLevel, LogRecord
from .. import theme

HEADERS = ['Time', 'Level', 'Logger', 'Message', 'Location', 'Function']
COL_TIME, COL_LEVEL, COL_LOGGER, COL_MESSAGE, COL_LOCATION, COL_FUNCTION = range(6)
LEVEL_COLOR = {LogLevel.DEBUG: 'stale', LogLevel.WARN: 'warn', LogLevel.ERROR: 'error',
               LogLevel.FATAL: 'error'}
RecordRole = Qt.UserRole + 1

# id -> label of the time formats
TIME_FORMATS = [('time', 'Time of day'), ('datetime', 'Date and time'),
                ('relative', 'Relative (s)'), ('stamp', 'ROS stamp (s)')]


def format_stamp(stamp: float, mode: str, origin: float = 0.0) -> str:
    if mode == 'datetime':
        return datetime.datetime.fromtimestamp(stamp).isoformat(sep=' ', timespec='milliseconds')
    if mode == 'relative':
        return f'{stamp - origin:+.3f}'
    if mode == 'stamp':
        return f'{stamp:.6f}'
    return time.strftime('%H:%M:%S', time.localtime(stamp)) + f'.{int((stamp % 1) * 1000):03d}'


def record_text(r: LogRecord, mode: str = 'datetime', extended: bool = False) -> str:
    """One line per record, as copied to the clipboard."""
    text = f'{format_stamp(r.stamp, mode)} [{r.level.name}] [{r.logger_name}] {r.message}'
    if extended and r.file:
        text += f'  ({r.file}:{r.line} in {r.function})'
    return text


class LogTableModel(QAbstractTableModel):
    def __init__(self, max_rows: int = 20000, parent=None):
        super().__init__(parent)
        self.max_rows = max_rows
        self.rows: list[LogRecord] = []
        self.accept: Optional[Callable[[LogRecord], bool]] = None
        self.time_mode = 'time'
        self.origin = 0.0                       # for relative times
        self.logger_colors: dict[str, QColor] = {}
        self.colorize = True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            if c == COL_TIME:
                return format_stamp(r.stamp, self.time_mode, self.origin)
            if c == COL_LEVEL:
                return r.level.name
            if c == COL_LOGGER:
                return r.logger_name
            if c == COL_MESSAGE:
                return r.message.replace('\n', ' ⏎ ')
            if c == COL_LOCATION:
                return f'{r.file.rsplit("/", 1)[-1]}:{r.line}' if r.file else ''
            return r.function
        if role == Qt.ForegroundRole and self.colorize and r.level in LEVEL_COLOR:
            return QBrush(theme.color(LEVEL_COLOR[r.level]))
        if role == Qt.BackgroundRole and self.logger_colors:
            color = self._logger_color(r.logger_name)
            if color is not None:
                return QBrush(color)
        if role == Qt.ToolTipRole and c == COL_LOCATION and r.file:
            return f'{r.file}:{r.line}\n{r.function}'
        if role == Qt.ToolTipRole and c == COL_MESSAGE:
            loc = f'\n{r.file}:{r.line} ({r.function})' if r.file else ''
            return r.message + loc
        if role == RecordRole:
            return r
        return None

    def _logger_color(self, logger: str) -> Optional[QColor]:
        # The most specific colored ancestor wins.
        name = logger
        while name:
            c = self.logger_colors.get(name)
            if c is not None:
                return c
            name = name.rpartition('.')[0]
        return None

    def refresh_display(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.rows) - 1, len(HEADERS) - 1))

    def set_records(self, records) -> None:
        self.beginResetModel()
        rows = [r for r in records if self.accept is None or self.accept(r)]
        self.rows = rows[-self.max_rows:]
        self.endResetModel()

    def append(self, records) -> int:
        new = [r for r in records if self.accept is None or self.accept(r)]
        if not new:
            return 0
        overflow = len(self.rows) + len(new) - self.max_rows
        if overflow > 0:
            n = min(overflow, len(self.rows))
            self.beginRemoveRows(QModelIndex(), 0, n - 1)
            del self.rows[:n]
            self.endRemoveRows()
            new = new[-self.max_rows:]
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()
        return len(new)

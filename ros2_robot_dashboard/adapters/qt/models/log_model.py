"""Append-only log model with a row cap (fast enough for busy /rosout)."""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush

from ....core.domain.logs import LogLevel, LogRecord
from .. import theme

HEADERS = ['Time', 'Level', 'Node', 'Message']
LEVEL_COLOR = {LogLevel.DEBUG: 'stale', LogLevel.WARN: 'warn', LogLevel.ERROR: 'error',
               LogLevel.FATAL: 'error'}
RecordRole = Qt.UserRole + 1


class LogTableModel(QAbstractTableModel):
    def __init__(self, max_rows: int = 20000, parent=None):
        super().__init__(parent)
        self.max_rows = max_rows
        self.rows: list[LogRecord] = []
        self.accept: Optional[Callable[[LogRecord], bool]] = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

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
            if c == 0:
                return time.strftime('%H:%M:%S', time.localtime(r.stamp)) + \
                    f'.{int((r.stamp % 1) * 1000):03d}'
            if c == 1:
                return r.level.name
            if c == 2:
                return r.node
            return r.message.replace('\n', ' ⏎ ')
        if role == Qt.ForegroundRole and r.level in LEVEL_COLOR:
            return QBrush(theme.color(LEVEL_COLOR[r.level]))
        if role == Qt.ToolTipRole and c == 3:
            loc = f'\n{r.file}:{r.line} ({r.function})' if r.file else ''
            return r.message + loc
        if role == RecordRole:
            return r
        return None

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

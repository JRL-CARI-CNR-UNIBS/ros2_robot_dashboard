"""Terminal-like text view: circular buffer, batched appends, pause, filter, save."""

from __future__ import annotations

import collections
import re
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QToolButton, QVBoxLayout, QWidget)

from .common import FilterEdit


class TerminalWidget(QWidget):
    def __init__(self, max_lines: int = 5000, show_toolbar: bool = True,
                 parent: Optional[QWidget] = None, flush_hz: float = 15.0):
        super().__init__(parent)
        self.max_lines = max_lines
        self._lines: collections.deque = collections.deque(maxlen=max_lines)
        self._pending: list[str] = []
        self._paused = False
        self._filter = ''
        self._filter_re: Optional[re.Pattern] = None
        self.view = QPlainTextEdit()
        self.view.setObjectName('terminal')
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(max_lines)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setUndoRedoEnabled(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QHBoxLayout()
        self.filter_edit = FilterEdit('Filter lines (regex)…')
        self.filter_edit.textChanged.connect(self.set_filter)
        self.autoscroll = QCheckBox('Auto-scroll')
        self.autoscroll.setChecked(True)
        self.pause_btn = QToolButton()
        self.pause_btn.setText('Pause')
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self.set_paused)
        clear = QToolButton()
        clear.setText('Clear')
        clear.clicked.connect(self.clear)
        save = QToolButton()
        save.setText('Save…')
        save.clicked.connect(self._save_dialog)
        self.count_label = QLabel('')
        for w in (self.filter_edit, self.autoscroll, self.pause_btn, clear, save,
                  self.count_label):
            self.toolbar.addWidget(w)
        if show_toolbar:
            lay.addLayout(self.toolbar)
        lay.addWidget(self.view, 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.start(int(1000 / flush_hz))

    # ----------------------------------------------------------------- api

    def append(self, text: str) -> None:
        for line in text.splitlines() or ['']:
            self._lines.append(line)
            if self._accept(line):
                self._pending.append(line)

    def append_lines(self, lines) -> None:
        for line in lines:
            self.append(line)

    def clear(self) -> None:
        self._lines.clear()
        self._pending.clear()
        self.view.clear()
        self.count_label.setText('')

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if self.pause_btn.isChecked() != paused:
            self.pause_btn.setChecked(paused)

    def set_filter(self, text: str) -> None:
        self._filter = text
        try:
            self._filter_re = re.compile(text, re.IGNORECASE) if text else None
        except re.error:
            self._filter_re = None
        self._rerender()

    def text(self) -> str:
        return '\n'.join(self._lines)

    def save(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.text() + '\n')

    # ----------------------------------------------------------------- internals

    def _accept(self, line: str) -> bool:
        if not self._filter:
            return True
        if self._filter_re is not None:
            return bool(self._filter_re.search(line))
        return self._filter.lower() in line.lower()

    def _rerender(self) -> None:
        self.view.clear()
        self._pending = [ln for ln in self._lines if self._accept(ln)]

    def _flush(self) -> None:
        if self._paused or not self._pending:
            return
        chunk = self._pending[-self.max_lines:]
        self._pending = []
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        cursor = QTextCursor(self.view.document())
        cursor.movePosition(QTextCursor.End)
        if not self.view.document().isEmpty():
            cursor.insertText('\n')
        cursor.insertText('\n'.join(chunk))
        if self.autoscroll.isChecked() and (at_bottom or True):
            bar.setValue(bar.maximum())
        self.count_label.setText(f'{len(self._lines)} lines')

    def _save_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, 'Save output', 'output.txt',
                                              'Text (*.txt *.log *.yaml);;All files (*)')
        if path:
            self.save(path)

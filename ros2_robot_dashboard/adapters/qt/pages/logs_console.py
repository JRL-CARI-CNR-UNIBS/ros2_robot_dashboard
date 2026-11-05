"""Logs tab: /rosout console in the style of swri_console.

Left: logger tree (nodes and their child loggers) with message counts and logger levels;
select loggers to show only their messages, right-click to change their level.
Right: messages with severity checkboxes, include/exclude filters, time formats,
optional columns, follow/pause, copy, save and load (text, JSONL, rosbag2).
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu,
                               QPlainTextEdit, QSplitter, QTableView, QTreeWidget,
                               QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget)

from ....core.domain.events import LogRecordsBatch
from ....core.domain.logs import LogLevel, logger_matches
from .. import theme
from ..models.log_model import (COL_FUNCTION, COL_LEVEL, COL_LOCATION, COL_LOGGER, COL_MESSAGE,
                                COL_TIME, LEVEL_COLOR, TIME_FORMATS, LogTableModel, RecordRole,
                                record_text)
from ..widgets.common import FilterEdit, button, copy_to_clipboard

NameRole = Qt.UserRole + 10
COL_NAME, COL_COUNT, COL_LEVEL_T = range(3)


class LoggerTree(QTreeWidget):
    """Dotted logger names as a tree; counts include child loggers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(['Logger', 'Msgs', 'Level'])
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_COUNT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_LEVEL_T, QHeaderView.ResizeToContents)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setSortingEnabled(False)
        self.setUniformRowHeights(True)
        self.items: dict[str, QTreeWidgetItem] = {}
        self.levels: dict[str, Optional[LogLevel]] = {}

    def ensure(self, name: str) -> QTreeWidgetItem:
        it = self.items.get(name)
        if it is not None:
            return it
        parent_name, _, leaf = name.rpartition('.')
        parent = self.ensure(parent_name) if parent_name else self.invisibleRootItem()
        it = QTreeWidgetItem([leaf, '0', ''])
        it.setData(COL_NAME, NameRole, name)
        it.setToolTip(COL_NAME, name)
        it.setTextAlignment(COL_COUNT, Qt.AlignRight | Qt.AlignVCenter)
        # keep siblings sorted
        idx = 0
        while idx < parent.childCount() and parent.child(idx).text(COL_NAME) < leaf:
            idx += 1
        parent.insertChild(idx, it)
        self.items[name] = it
        if parent is not self.invisibleRootItem():
            parent.setExpanded(True)
        return it

    def update_counts(self, counts: dict[str, int]) -> list[str]:
        """Update the counts; returns the logger names that were not in the tree."""
        totals: dict[str, int] = defaultdict(int)
        new = []
        for name, n in counts.items():
            if not name:
                continue
            if name not in self.items:
                new.append(name)
            self.ensure(name)
            parts = name.split('.')
            for i in range(1, len(parts) + 1):
                totals['.'.join(parts[:i])] += n
        for name, it in self.items.items():
            text = str(totals.get(name, 0))
            if it.text(COL_COUNT) != text:
                it.setText(COL_COUNT, text)
        return new

    def set_level(self, name: str, level: Optional[LogLevel]) -> None:
        self.levels[name] = level
        it = self.ensure(name)
        it.setText(COL_LEVEL_T, level.name if level is not None else 'inherit')
        it.setToolTip(COL_LEVEL_T, 'level set on this logger' if level is not None else
                      'no level set: the effective level comes from the parent logger')

    def selected_names(self) -> list[str]:
        return [it.data(COL_NAME, NameRole) for it in self.selectedItems()]

    def clear_all(self) -> None:
        self.clear()
        self.items = {}
        self.levels = {}


class LogsTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.svc = page.rt.logs
        self._pattern_in: Optional[re.Pattern] = None
        self._pattern_out: Optional[re.Pattern] = None
        self._selected: list[str] = []
        self._build()
        self._counts_timer = QTimer(self)
        self._counts_timer.timeout.connect(self._update_tree_counts)
        self._counts_timer.start(1000)
        # Levels of new loggers are read automatically (where the node allows it).
        self._to_read: set[str] = set()
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.timeout.connect(self._read_new_levels)
        self._restore_settings()

    # ----------------------------------------------------------------- UI

    def _build(self) -> None:
        # --- logger tree
        self.tree = LoggerTree()
        self.tree.itemSelectionChanged.connect(self._tree_selection_changed)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree_filter = FilterEdit('Filter loggers…')
        self.tree_filter.textChanged.connect(self._filter_tree)
        show_all = button('All', self.tree.clearSelection, 'Show the messages of all loggers')
        read_levels = button('Read levels', self.read_levels,
                             'Ask the nodes for their logger levels (needs logger services)')
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.tree_filter, 1)
        row.addWidget(show_all)
        ll.addLayout(row)
        ll.addWidget(self.tree, 1)
        ll.addWidget(read_levels)
        hint = QLabel('Select loggers to filter. Right-click to change their level.')
        hint.setWordWrap(True)
        hint.setObjectName('hint')
        ll.addWidget(hint)

        # --- messages
        self.model = LogTableModel(int(self.page.rt.config.get('logs.max_rows', 20000)), self)
        self.model.accept = self._accept
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(20)
        self.view.horizontalHeader().setStretchLastSection(False)
        self.view.horizontalHeader().setSectionResizeMode(COL_MESSAGE, QHeaderView.Stretch)
        for col, w in ((COL_TIME, 100), (COL_LEVEL, 55), (COL_LOGGER, 230), (COL_LOCATION, 200),
                       (COL_FUNCTION, 150)):
            self.view.setColumnWidth(col, w)
        self.view.setWordWrap(False)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._msg_menu)
        self.view.selectionModel().currentRowChanged.connect(self._show_detail)
        QShortcut(QKeySequence.Copy, self.view, activated=lambda: self.copy_selected(False))

        self.levels: dict[LogLevel, QCheckBox] = {}
        sev = QHBoxLayout()
        for lv in LogLevel:
            cb = QCheckBox(lv.name.capitalize())
            cb.setChecked(lv != LogLevel.DEBUG)
            if lv in LEVEL_COLOR:
                cb.setStyleSheet(f'color: {theme.COLORS[LEVEL_COLOR[lv]]};')
            cb.toggled.connect(self._refilter)
            self.levels[lv] = cb
            sev.addWidget(cb)
        self.include = FilterEdit('Include…')
        self.include.setToolTip('Show only messages containing this text')
        self.include.textChanged.connect(self._refilter)
        self.exclude = FilterEdit('Exclude…')
        self.exclude.setToolTip('Hide messages containing this text')
        self.exclude.textChanged.connect(self._refilter)
        self.regex = QCheckBox('Regex')
        self.regex.setToolTip('Include/exclude are regular expressions (case insensitive)')
        self.regex.toggled.connect(self._refilter)

        self.time_fmt = QComboBox()
        for key, label in TIME_FORMATS:
            self.time_fmt.addItem(label, key)
        self.time_fmt.currentIndexChanged.connect(self._time_changed)
        self.col_logger = QCheckBox('Logger')
        self.col_logger.setChecked(True)
        self.col_location = QCheckBox('Location')
        self.col_function = QCheckBox('Function')
        for cb in (self.col_logger, self.col_location, self.col_function):
            cb.toggled.connect(self._columns_changed)
        self.colorize = QCheckBox('Colors')
        self.colorize.setChecked(True)
        self.colorize.setToolTip('Color messages by severity')
        self.colorize.toggled.connect(self._colorize_changed)
        self.follow = QCheckBox('Follow')
        self.follow.setChecked(True)
        self.follow.setToolTip('Scroll to the newest message')
        self.pause = QCheckBox('Pause')
        self.pause.setToolTip('Stop updating the list (messages are still collected)')
        self.pause.toggled.connect(lambda v: (not v) and self._refilter())
        clear = button('Clear', self._clear, 'Delete all collected messages')
        save = button('Save…', self._save, 'Save the shown messages (text or JSONL)')
        load = button('Load…', self._load, 'Load messages from a text/JSONL file or a bag')

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setObjectName('terminal')
        self.detail.setMaximumHeight(110)
        self.counts = QLabel('')

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        r1 = QHBoxLayout()
        r1.addLayout(sev)
        r1.addWidget(self.include, 2)
        r1.addWidget(self.exclude, 1)
        r1.addWidget(self.regex)
        rl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel('Time'))
        r2.addWidget(self.time_fmt)
        r2.addWidget(QLabel('  Columns'))
        for w in (self.col_logger, self.col_location, self.col_function, self.colorize):
            r2.addWidget(w)
        r2.addStretch(1)
        for w in (self.follow, self.pause, clear, save, load):
            r2.addWidget(w)
        rl.addLayout(r2)
        rl.addWidget(self.view, 1)
        rl.addWidget(self.detail)
        rl.addWidget(self.counts)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([400, 1000])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)
        self._columns_changed()

    def _restore_settings(self) -> None:
        p = self.page
        mode = p.setting('logs_time_format', 'time')
        idx = self.time_fmt.findData(mode)
        self.time_fmt.setCurrentIndex(max(idx, 0))
        for lv, cb in self.levels.items():
            cb.setChecked(str(p.setting(f'logs_show_{lv.name}',
                                        'false' if lv == LogLevel.DEBUG else 'true')) == 'true')
        self.col_location.setChecked(str(p.setting('logs_col_location', 'false')) == 'true')
        self.col_function.setChecked(str(p.setting('logs_col_function', 'false')) == 'true')

    # ----------------------------------------------------------------- filtering

    def _accept(self, r) -> bool:
        cb = self.levels.get(r.level)
        if cb is not None and not cb.isChecked():
            return False
        if self._selected and not logger_matches(r.logger_name, self._selected):
            return False
        inc, exc = self.include.text(), self.exclude.text()
        if self.regex.isChecked():
            if self._pattern_in is not None and not self._pattern_in.search(r.message):
                return False
            if self._pattern_out is not None and self._pattern_out.search(r.message):
                return False
        else:
            low = r.message.lower()
            if inc and inc.lower() not in low:
                return False
            if exc and exc.lower() in low:
                return False
        return True

    def _compile(self, edit) -> tuple[bool, Optional[re.Pattern]]:
        t = edit.text()
        if not t or not self.regex.isChecked():
            edit.setStyleSheet('')
            return True, None
        try:
            pat = re.compile(t, re.IGNORECASE)
            edit.setStyleSheet('')
            return True, pat
        except re.error:
            edit.setStyleSheet(f'color: {theme.COLORS["error"]};')
            return False, None

    def _refilter(self, *_a) -> None:
        ok_in, self._pattern_in = self._compile(self.include)
        ok_out, self._pattern_out = self._compile(self.exclude)
        if not (ok_in and ok_out):
            return
        for lv, cb in self.levels.items():
            self.page.set_setting(f'logs_show_{lv.name}', 'true' if cb.isChecked() else 'false')
        records = self.svc.records()
        self.model.origin = records[0].stamp if records else 0.0
        self.model.set_records(records)
        self._scroll()
        self._update_counts()

    def _tree_selection_changed(self) -> None:
        self._selected = self.tree.selected_names()
        self._refilter()

    def _filter_tree(self, text: str) -> None:
        text = text.strip().lower()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            name = item.data(COL_NAME, NameRole) or ''
            hidden = bool(text) and text not in name.lower() and not any(
                text in n.lower() for n in self.tree.items if n.startswith(name + '.'))
            item.setHidden(hidden)
            it += 1

    # ----------------------------------------------------------------- live data

    def on_batch(self, ev: LogRecordsBatch) -> None:
        for r in ev.records:
            if r.logger_name not in self.tree.items:
                self.tree.ensure(r.logger_name)
                self._to_read.add(r.logger_name)
        if self._to_read and not self._read_timer.isActive():
            self._read_timer.start(1500)
        if self.pause.isChecked():
            self._update_counts()
            return
        if not self.model.rows and ev.records:
            self.model.origin = ev.records[0].stamp
        if self.model.append(ev.records):
            self._scroll()
        self._update_counts()

    def _update_tree_counts(self) -> None:
        if self.isVisible():
            new = self.tree.update_counts(self.svc.loggers())
            if new:
                self._to_read.update(new)
                if not self._read_timer.isActive():
                    self._read_timer.start(1500)

    def _scroll(self) -> None:
        if self.follow.isChecked():
            self.view.scrollToBottom()

    def _update_counts(self) -> None:
        counts = self.svc.counts_by_level
        parts = [f'{len(self.model.rows)} shown']
        for lv in (LogLevel.WARN, LogLevel.ERROR, LogLevel.FATAL):
            n = counts.get(lv, 0)
            if n:
                parts.append(f'{n} {lv.name}')
        if self._selected:
            parts.append(f'loggers: {", ".join(self._selected)}')
        if self.pause.isChecked():
            parts.append('PAUSED')
        self.counts.setText(' · '.join(parts))

    # ----------------------------------------------------------------- display options

    def _time_changed(self, _i: int) -> None:
        self.model.time_mode = self.time_fmt.currentData()
        self.page.set_setting('logs_time_format', self.model.time_mode)
        self.model.refresh_display()

    def _columns_changed(self, *_a) -> None:
        self.view.setColumnHidden(COL_LOGGER, not self.col_logger.isChecked())
        self.view.setColumnHidden(COL_LOCATION, not self.col_location.isChecked())
        self.view.setColumnHidden(COL_FUNCTION, not self.col_function.isChecked())
        self.page.set_setting('logs_col_location',
                              'true' if self.col_location.isChecked() else 'false')
        self.page.set_setting('logs_col_function',
                              'true' if self.col_function.isChecked() else 'false')

    def _colorize_changed(self, on: bool) -> None:
        self.model.colorize = on
        self.model.refresh_display()

    # ----------------------------------------------------------------- messages

    def _show_detail(self, current: QModelIndex, _prev) -> None:
        r = current.data(RecordRole) if current.isValid() else None
        if r is None:
            return
        loc = f'{r.file}:{r.line} in {r.function}' if r.file else ''
        self.detail.setPlainText(f'[{r.level.name}] [{r.logger_name}] {r.stamp:.6f}\n{loc}\n\n'
                                 f'{r.message}')

    def selected_records(self) -> list:
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        return [self.model.rows[r] for r in rows if r < len(self.model.rows)]

    def copy_selected(self, extended: bool) -> None:
        recs = self.selected_records()
        if recs:
            copy_to_clipboard('\n'.join(record_text(r, 'datetime', extended) for r in recs))

    def _msg_menu(self, pos) -> None:
        r = self.view.indexAt(pos).data(RecordRole)
        if r is None:
            return
        menu = QMenu(self)
        menu.addAction('Copy', lambda: self.copy_selected(False))
        menu.addAction('Copy with location', lambda: self.copy_selected(True))
        menu.addAction('Copy message text', lambda: copy_to_clipboard(r.message))
        menu.addSeparator()
        menu.addAction(f'Only {r.logger_name}', lambda: self.select_loggers([r.logger_name]))
        menu.addAction('Exclude messages like this…', lambda: self._exclude_like(r.message))
        self._add_level_menu(menu, [r.logger_name])
        node = self.svc.owner_node(r.logger_name)
        if node:
            menu.addAction(f'Parameters of {node}',
                           lambda: self.page.ctx.navigate('params', 'node', node))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _exclude_like(self, message: str) -> None:
        text, ok = QInputDialog.getText(self, 'Exclude messages', 'Hide messages containing:',
                                        text=message.splitlines()[0][:80] if message else '')
        if ok and text:
            self.regex.setChecked(False)
            self.exclude.setText(text)

    def select_loggers(self, names: list[str]) -> None:
        self.tree.clearSelection()
        for n in names:
            self.tree.ensure(n).setSelected(True)

    def _clear(self) -> None:
        self.svc.clear()
        self.model.set_records([])
        self.tree.update_counts({})
        self._update_counts()

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save logs', os.path.expanduser(f'~/rosout_{time.strftime("%Y%m%d_%H%M%S")}'
                                                  '.log'),
            'Text log (*.log *.txt);;JSON lines (*.jsonl)')
        if path:
            n = self.svc.export(path, list(self.model.rows))
            self.page.message(f'{n} messages saved to {path}')

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load logs', os.path.expanduser('~'),
            'Logs and bags (*.log *.txt *.jsonl *.mcap *.db3 metadata.yaml);;All files (*)')
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        if os.path.basename(path) == 'metadata.yaml':
            path = os.path.dirname(path)            # a bag folder
        self.page.call(self.svc.load(path),
                       lambda n: (self.page.message(f'{n} messages loaded from {path}'),
                                  self._refilter()),
                       error_title='Load logs')

    # ----------------------------------------------------------------- logger levels

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        names = self.tree.selected_names()
        menu = QMenu(self)
        self._add_level_menu(menu, names)
        if len(names) == 1:
            menu.addAction('Set level of a child logger…', lambda: self._child_level(names[0]))
        menu.addAction('Read levels', lambda: self.read_levels(names))
        menu.addSeparator()
        menu.addAction('Color…', lambda: self._pick_color(names))
        menu.addAction('Clear color', lambda: self._set_color(names, None))
        menu.addSeparator()
        menu.addAction('Copy name', lambda: copy_to_clipboard('\n'.join(names)))
        node = self.svc.owner_node(names[0])
        if node and len(names) == 1:
            menu.addAction(f'Parameters of {node}',
                           lambda: self.page.ctx.navigate('params', 'node', node))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _add_level_menu(self, menu: QMenu, names: list[str]) -> None:
        current = self.tree.levels.get(names[0]) if len(names) == 1 else None
        title = 'Set logger level' + (f' ({current.name})' if current is not None else '')
        sub = menu.addMenu(title)
        problems = {self.svc.level_service_problem(self.svc.owner_node(n)) for n in names}
        problems.discard('')
        if problems:
            sub.setToolTipsVisible(True)
            for p in sorted(problems):
                a = sub.addAction(p if len(p) < 90 else p[:87] + '…')
                a.setToolTip(p)
                a.setEnabled(False)
            if len(problems) == len(names) or not self.page.can_write():
                return
            sub.addSeparator()
        if not self.page.can_write():
            sub.addAction('Read-only mode').setEnabled(False)
            return
        for lv in LogLevel:
            act = QAction(lv.name, sub)
            act.setCheckable(True)
            act.setChecked(current == lv)
            act.triggered.connect(lambda _c=False, lv=lv: self.set_level(names, lv))
            sub.addAction(act)

    def _child_level(self, parent: str) -> None:
        name, ok = QInputDialog.getText(
            self, 'Child logger', 'Full logger name (e.g. a sub-logger not seen yet):',
            text=parent + '.')
        name = name.strip().strip('.')
        if not ok or not name:
            return
        levels = [lv.name for lv in LogLevel]
        lvl, ok = QInputDialog.getItem(self, 'Child logger', f'Level of {name}:', levels, 0,
                                       False)
        if ok:
            self.tree.ensure(name)
            self.set_level([name], LogLevel[lvl])

    def set_level(self, names: list[str], level: LogLevel) -> None:
        by_node: dict[str, list[str]] = defaultdict(list)
        skipped = []
        for n in names:
            node = self.svc.owner_node(n)
            if self.svc.level_service_problem(node):
                skipped.append(n)
            else:
                by_node[node].append(n)
        if skipped:
            self.page.message('Skipped (no logger service): ' + ', '.join(skipped), 8000)
        for node, loggers in by_node.items():
            def ok(res, node=node, loggers=loggers):
                failed = {k: v for k, v in res.items() if v}
                if failed:
                    self.page.error('Set logger level', '\n'.join(
                        f'{k}: {v}' for k, v in failed.items()))
                else:
                    self.page.message(f'{", ".join(loggers)} -> {level.name}')
                self.read_levels(loggers)
            self.page.call(self.svc.set_levels(node, {n: level for n in loggers}), ok,
                           error_title='Set logger level')

    def _read_new_levels(self) -> None:
        names, self._to_read = sorted(self._to_read), set()
        self.read_levels(names, quiet=True)

    def read_levels(self, names: Optional[list[str]] = None, quiet: bool = False) -> None:
        names = names or list(self.tree.items)
        by_node: dict[str, list[str]] = defaultdict(list)
        for n in names:
            node = self.svc.owner_node(n)
            if not self.svc.level_service_problem(node):
                by_node[node].append(n)
        if not by_node:
            if not quiet:
                self.page.message('No logger offers get_logger_levels (needs Iron or newer '
                                  'and nodes created with enable_logger_service)', 8000)
            return
        for node, loggers in by_node.items():
            self.page.call(self.svc.get_levels(node, loggers),
                           lambda res: [self.tree.set_level(k, v) for k, v in res.items()],
                           lambda exc, node=node: self.page.message(
                               f'{node}: cannot read logger levels: {exc}', 8000))

    # ----------------------------------------------------------------- colors

    def _pick_color(self, names: list[str]) -> None:
        c = QColorDialog.getColor(QColor('#fff3b0'), self, 'Logger color')
        if c.isValid():
            c.setAlpha(110)
            self._set_color(names, c)

    def _set_color(self, names: list[str], color: Optional[QColor]) -> None:
        for n in names:
            it = self.tree.ensure(n)
            if color is None:
                self.model.logger_colors.pop(n, None)
                it.setBackground(COL_NAME, Qt.transparent)
            else:
                self.model.logger_colors[n] = color
                it.setBackground(COL_NAME, color)
        self.model.refresh_display()

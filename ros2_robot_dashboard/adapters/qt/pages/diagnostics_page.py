"""Diagnostics & logs: /rosout viewer, /diagnostics, lifecycle nodes."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                               QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ....core.domain.events import DiagnosticsUpdated, LifecycleNodesChanged, LogRecordsBatch
from ....core.domain.logs import DiagLevel, DiagnosticStatusInfo, LifecycleNodeInfo
from .. import theme
from ..widgets.common import Column, FilterEdit, RowsModel, button, fmt_age, make_table
from .base_page import BasePage
from .logs_console import LogsTab

DIAG_COLOR = {DiagLevel.OK: 'ok', DiagLevel.WARN: 'warn', DiagLevel.ERROR: 'error',
              DiagLevel.STALE: 'stale'}
LIFECYCLE_COLOR = {'active': 'ok', 'inactive': 'warn', 'unconfigured': 'stale',
                   'finalized': 'error'}


class DiagnosticsTab(QWidget):
    def __init__(self, page: 'DiagnosticsPage'):
        super().__init__()
        self.page = page
        self.svc = page.rt.diagnostics
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Name', 'Level', 'Message', 'Hardware id', 'Age'])
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 340)
        self.tree.currentItemChanged.connect(self._show)
        self.filter = FilterEdit('Filter…')
        self.filter.textChanged.connect(lambda _t: self._fill(self.svc.items()))
        self.only_problems = QCheckBox('Only WARN/ERROR/STALE')
        self.only_problems.toggled.connect(lambda _v: self._fill(self.svc.items()))
        clear = button('Clear', lambda: (self.svc.clear(), self._fill([])))
        self.summary = QLabel('')
        self.values = QPlainTextEdit()
        self.values.setReadOnly(True)
        self.values.setObjectName('terminal')
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.values)
        split.setSizes([900, 400])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (self.filter, self.only_problems, clear):
            row.addWidget(w)
        lay.addLayout(row)
        lay.addWidget(split, 1)
        lay.addWidget(self.summary)
        self._items: dict[str, DiagnosticStatusInfo] = {}

    def on_update(self, ev: DiagnosticsUpdated) -> None:
        self._fill(ev.items)

    def _fill(self, items) -> None:
        current = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() \
            else ''
        self._items = {i.name: i for i in items}
        text = self.filter.text().strip().lower()
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        now = time.time()
        counts = {lv: 0 for lv in DiagLevel}
        for it in items:
            counts[it.level] = counts.get(it.level, 0) + 1
            if self.only_problems.isChecked() and it.level == DiagLevel.OK:
                continue
            if text and text not in it.name.lower() and text not in it.message.lower():
                continue
            # Group "/a/b/c" (diagnostic_aggregator style) or "node: name" by its prefix.
            if it.name.startswith('/') and it.name.count('/') > 1:
                group = it.name.rsplit('/', 1)[0]
            elif ': ' in it.name:
                group = it.name.split(': ', 1)[0]
            else:
                group = ''
            parent = self.tree.invisibleRootItem()
            if group:
                parent = groups.get(group)
                if parent is None:
                    parent = QTreeWidgetItem(self.tree, [group])
                    groups[group] = parent
            row = QTreeWidgetItem(parent, [it.name[len(group):].lstrip('/: ') or it.name,
                                           it.level.name, it.message, it.hardware_id,
                                           fmt_age(now - it.last_update)])
            row.setData(0, Qt.UserRole, it.name)
            c = theme.color(DIAG_COLOR.get(it.level, 'stale'))
            row.setForeground(1, c)
            if it.level != DiagLevel.OK:
                row.setForeground(0, c)
                p = row.parent()
                if p is not None:
                    p.setForeground(0, c)
            if it.name == current:
                self.tree.setCurrentItem(row)
        self.tree.expandAll()
        self.summary.setText(' · '.join(f'{counts.get(lv, 0)} {lv.name}' for lv in DiagLevel))

    def _show(self, item, _prev=None) -> None:
        if item is None:
            return
        name = item.data(0, Qt.UserRole)
        it = self._items.get(name)
        if it is None:
            return
        lines = [it.name, f'level: {it.level.name}  message: {it.message}',
                 f'hardware_id: {it.hardware_id}', '']
        lines += [f'{k}: {v}' for k, v in it.values]
        hist = self.svc.history(name)
        if hist:
            lines += ['', 'level changes:']
            lines += [f'  {time.strftime("%H:%M:%S", time.localtime(t))} {DiagLevel(a).name} -> '
                      f'{DiagLevel(b).name} {m}' for t, a, b, m in hist[-20:]]
        self.values.setPlainText('\n'.join(lines))


class LifecycleTab(QWidget):
    def __init__(self, page: 'DiagnosticsPage'):
        super().__init__()
        self.page = page
        self.svc = page.rt.lifecycle
        self.model = RowsModel([
            Column('Node', lambda n: n.node),
            Column('State', lambda n: n.state_label,
                   fg=lambda n: theme.color(LIFECYCLE_COLOR.get(n.state_label, 'stale'))),
            Column('Available transitions', lambda n: ', '.join(t.label for t in n.transitions)),
            Column('Error', lambda n: n.error),
        ], key=lambda n: n.node)
        self.table = make_table(self.model)
        self.table.setColumnWidth(0, 300)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.selectionModel().currentRowChanged.connect(self._selected)
        self.transition = QComboBox()
        self.do_btn = button('Run transition', self._run)
        page.register_write_widget(self.do_btn)
        refresh = button('Refresh', self._refresh_all)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (QLabel('Transition'), self.transition, self.do_btn, refresh):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.table, 1)

    def on_nodes(self, ev: LifecycleNodesChanged) -> None:
        self.model.set_rows(sorted(ev.nodes, key=lambda n: n.node))
        cur = self.table.currentIndex()
        if cur.isValid():
            self._selected(cur, None)

    def _refresh_all(self) -> None:
        for n in self.svc.nodes():
            self.svc.refresh(n)

    def _selected(self, current, _prev) -> None:
        n: LifecycleNodeInfo = self.model.row_at(current)
        prev = self.transition.currentText()
        self.transition.clear()
        if n is None:
            return
        for t in n.transitions:
            self.transition.addItem(f'{t.label} ({t.start_state} → {t.goal_state})', t)
        idx = self.transition.findText(prev)
        if idx >= 0:
            self.transition.setCurrentIndex(idx)

    def _run(self) -> None:
        n = self.model.row_at(self.table.currentIndex())
        t = self.transition.currentData()
        if n is None or t is None:
            return
        self._change(n.node, t)

    def _change(self, node: str, t) -> None:
        if not self.page.confirm_action('Lifecycle transition',
                                        f'Run "{t.label}" on {node}?',
                                        f'{t.start_state} → {t.goal_state}'):
            return
        self.page.call(self.svc.change_state(node, t.id, t.label),
                       lambda ok: self.page.message(f'{node}: {t.label} ' +
                                                    ('done' if ok else 'REFUSED')),
                       error_title='Lifecycle')

    def _menu(self, pos) -> None:
        n = self.model.row_at(self.table.indexAt(pos))
        if n is None:
            return
        menu = QMenu(self)
        for t in n.transitions:
            a = menu.addAction(f'{t.label} → {t.goal_state}', lambda t=t: self._change(n.node, t))
            a.setEnabled(not self.page.rt.safety.read_only)
        menu.exec(self.table.viewport().mapToGlobal(pos))


class DiagnosticsPage(BasePage):
    page_id = 'diagnostics'
    title = 'Diagnostics & logs'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tabs = QTabWidget()
        self.logs = LogsTab(self)
        self.diags = DiagnosticsTab(self)
        self.lifecycle = LifecycleTab(self)
        self.tabs.addTab(self.logs, 'Logs (/rosout)')
        self.tabs.addTab(self.diags, 'Diagnostics')
        self.tabs.addTab(self.lifecycle, 'Lifecycle nodes')
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.on_event(LogRecordsBatch, self.logs.on_batch)
        self.on_event(DiagnosticsUpdated, self.diags.on_update)
        self.on_event(LifecycleNodesChanged, self.lifecycle.on_nodes)

    def on_enter(self) -> None:
        self.logs._refilter()
        self.diags._fill(self.rt.diagnostics.items())
        self.svc_lc = self.rt.lifecycle
        self.svc_lc.watch('diagnostics_page')
        self.lifecycle.model.set_rows(sorted(self.svc_lc.info(), key=lambda n: n.node))

    def on_leave(self) -> None:
        self.rt.lifecycle.unwatch('diagnostics_page')

    def focus_item(self, kind: str, name: str) -> None:
        self.tabs.setCurrentWidget(self.logs)
        self.logs._only_node(name)

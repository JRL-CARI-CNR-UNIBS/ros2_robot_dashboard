"""Parameters page (design v1): nodes, parameter tree, typed editors, presets."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QLabel,
                               QListView, QMenu, QSplitter, QTreeView, QVBoxLayout, QWidget)

from ....core import presets
from ....core.config import user_dir
from ....core.domain.events import NodesChanged, ParamEvent, ParamNodeStatus
from ....core.domain.params import NodeStatus, ParamStatus, ParamType, format_value, validate
from ..models.param_model import (COL_NAME, COL_TYPE, COL_VALUE, ItemRole, NamespaceItem,
                                  NodeListModel, ParamFilterProxy, ParamItem, ParamTreeModel)
from ..widgets.common import FilterEdit, button, confirm, copy_to_clipboard, info, tool_button
from ..widgets.param_editors import ParamDelegate
from .base_page import BasePage


class ParamsPage(BasePage):
    page_id = 'params'
    title = 'Parameters'
    icon_name = 'SP_FileDialogDetailedView'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.params
        self.current_node = ''
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._send_pending)
        self._build()
        self.on_event(NodesChanged, self._on_nodes)
        self.on_event(ParamNodeStatus, self._on_node_status)
        self.on_event(ParamEvent, self._on_param_event)

    # ----------------------------------------------------------------- ui

    def _build(self) -> None:
        cfg = self.rt.config
        # Left: nodes
        self.node_model = NodeListModel(self)
        self.node_proxy = QSortFilterProxyModel(self)
        self.node_proxy.setSourceModel(self.node_model)
        self.node_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.node_filter = FilterEdit('Filter nodes…')
        self.node_filter.textChanged.connect(self.node_proxy.setFilterFixedString)
        self.node_view = QListView()
        self.node_view.setModel(self.node_proxy)
        self.node_view.selectionModel().currentChanged.connect(self._node_selected)
        self.show_hidden_nodes = QCheckBox('Hidden nodes')
        self.show_hidden_nodes.toggled.connect(lambda _v: self._refresh_nodes())
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('Nodes'))
        ll.addWidget(self.node_filter)
        ll.addWidget(self.node_view, 1)
        ll.addLayout(_row(self.show_hidden_nodes, None,
                          tool_button('Refresh', lambda: self.rt.graph.refresh())))

        # Right: parameters
        self.model = ParamTreeModel(self)
        self.proxy = ParamFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        hide = cfg.get('params.hide_regex', '')
        self._hide_re = re.compile(hide) if hide else None
        self.proxy.set_hidden_regex(self._hide_re)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.SelectedClicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.delegate = ParamDelegate(self.proxy, self.tree)
        self.delegate.edited.connect(self.on_param_edited)
        self.tree.setItemDelegateForColumn(COL_VALUE, self.delegate)
        self.tree.header().resizeSection(COL_NAME, 260)
        self.tree.header().resizeSection(COL_VALUE, 280)
        self.tree.header().resizeSection(COL_TYPE, 110)

        self.filter = FilterEdit('Filter parameters…')
        self.filter.textChanged.connect(self._filter_changed)
        self.only_modified = QCheckBox('Only modified')
        self.only_modified.toggled.connect(self.proxy.set_only_modified)
        self.show_hidden = QCheckBox('Hidden params')
        self.show_hidden.setToolTip('Show use_sim_time, qos_overrides.* and similar')
        self.show_hidden.toggled.connect(
            lambda v: self.proxy.set_hidden_regex(None if v else self._hide_re))

        self.apply_btn = button('Apply all', self.apply_all, 'Send all modified parameters')
        self.revert_btn = button('Revert all', self.revert_all,
                                 'Set all parameters back to the values read first')
        self.discard_btn = button('Discard', self.discard_all, 'Forget local modifications')
        self.save_btn = button('Save preset…', self.save_preset)
        self.load_btn = button('Load preset…', self.load_preset)
        self.reload_btn = button('Reload', self.reload)
        self.auto_apply = QCheckBox('Auto-apply')
        self.auto_apply.setToolTip('Send changes right away (debounced)')
        auto = bool(cfg.get('params.auto_apply', True))
        self.auto_apply.setChecked(self.setting('auto_apply', auto))
        self.auto_apply.toggled.connect(lambda v: self.set_setting('auto_apply', v))
        self.atomic = QCheckBox('Atomic')
        self.atomic.setToolTip('Apply all with set_parameters_atomically (all or nothing)')
        atomic = bool(cfg.get('params.atomic_apply', False))
        self.atomic.setChecked(self.setting('atomic', atomic))
        self.atomic.toggled.connect(lambda v: self.set_setting('atomic', v))
        self.register_write_widget(self.apply_btn, self.revert_btn, self.load_btn)

        self.node_label = QLabel('Select a node')
        self.node_label.setObjectName('pageTitle')
        self.status_label = QLabel('')
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.node_label)
        rl.addLayout(_row(self.apply_btn, self.revert_btn, self.discard_btn, 12, self.save_btn,
                          self.load_btn, self.reload_btn, None, self.auto_apply, self.atomic))
        rl.addLayout(_row(self.filter, self.only_modified, self.show_hidden))
        rl.addWidget(self.tree, 1)
        rl.addWidget(self.status_label)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 900])
        lay = QVBoxLayout(self)
        lay.addWidget(split)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self._refresh_nodes()

    def _refresh_nodes(self, nodes=None) -> None:
        snap = self.rt.graph.snapshot()
        names = [n.fqn for n in (nodes or snap.nodes)]
        if not self.show_hidden_nodes.isChecked():
            names = [n for n in names if not self.rt.graph.is_hidden_node(n)]
        self.node_model.update_nodes(names)

    def focus_item(self, kind: str, name: str) -> None:
        node = name
        param = ''
        if kind == 'parameter':
            node, param = name.split('|', 1) if '|' in name else (self.current_node, name)
        row = self.node_model.row_of(node)
        if row >= 0:
            idx = self.node_proxy.mapFromSource(self.node_model.index(row))
            self.node_view.setCurrentIndex(idx)
        if param:
            self._focus_param = param
            self.filter.setText(param)

    # ----------------------------------------------------------------- events

    def _on_nodes(self, ev: NodesChanged) -> None:
        self._refresh_nodes(ev.nodes)

    def _on_node_status(self, ev: ParamNodeStatus) -> None:
        self.node_model.set_status(ev.node, ev.status)
        if ev.node != self.current_node:
            return
        if ev.status in (NodeStatus.UNREACHABLE, NodeStatus.GONE):
            self.model.mark_stale(True)
            self._update_status(f'{ev.status.value}: {ev.message}' if ev.message
                                else ev.status.value)
        elif ev.status == NodeStatus.DISCOVERED and self.model.stale:
            self.reload()

    def _on_param_event(self, ev: ParamEvent) -> None:
        if ev.node != self.current_node:
            return
        result = self.model.apply_event(ev.param, ev.kind)
        if result == 'new':
            self.reload()
        elif result == 'external_change':
            self.message(f"{ev.param.name} changed outside the dashboard", 6000)
        self._update_status()

    # ----------------------------------------------------------------- node selection

    def _node_selected(self, current: QModelIndex, _prev=None) -> None:
        if not current.isValid():
            return
        name = current.data(Qt.UserRole)
        if name == self.current_node:
            return
        if self.model.modified_items() and not confirm(
                self, 'Unsaved changes', f'{len(self.model.modified_items())} modified '
                f'parameters of {self.current_node} were not applied. Discard them?',
                ok_text='Discard', danger=False):
            row = self.node_model.row_of(self.current_node)
            if row >= 0:
                self.node_view.selectionModel().blockSignals(True)
                self.node_view.setCurrentIndex(self.node_proxy.mapFromSource(
                    self.node_model.index(row)))
                self.node_view.selectionModel().blockSignals(False)
            return
        self.current_node = name
        self.node_label.setText(name)
        self.model.set_node(name)
        self.reload()

    def reload(self) -> None:
        node = self.current_node
        if not node:
            return
        expanded = self._expanded_prefixes()
        self._update_status('loading…')

        def ok(params):
            if node != self.current_node:
                return
            self.model.load(node, params)
            self._restore_expanded(expanded)
            focus = getattr(self, '_focus_param', '')
            if focus:
                idx = self.proxy.mapFromSource(self.model.index_of(focus))
                if idx.isValid():
                    self.tree.scrollTo(idx)
                    self.tree.setCurrentIndex(idx)
                self._focus_param = ''
            self._update_status()

        def err(exc):
            if node == self.current_node:
                self.model.mark_stale(True)
                self._update_status(f'unreachable: {exc}')

        self.bridge.call(self.svc.load(node), ok, err)

    def _expanded_prefixes(self) -> set:
        out = set()

        def walk(parent):
            for r in range(self.proxy.rowCount(parent)):
                idx = self.proxy.index(r, 0, parent)
                item = idx.data(ItemRole)
                if isinstance(item, NamespaceItem) and self.tree.isExpanded(idx):
                    out.add(item.prefix)
                    walk(idx)

        walk(QModelIndex())
        return out

    def _restore_expanded(self, prefixes: set) -> None:
        if not prefixes:
            self.tree.expandToDepth(0)
            return

        def walk(parent):
            for r in range(self.proxy.rowCount(parent)):
                idx = self.proxy.index(r, 0, parent)
                item = idx.data(ItemRole)
                if isinstance(item, NamespaceItem) and item.prefix in prefixes:
                    self.tree.setExpanded(idx, True)
                    walk(idx)

        walk(QModelIndex())

    def _filter_changed(self, text: str) -> None:
        self.proxy.set_text(text)
        if text:
            self.tree.expandAll()

    # ----------------------------------------------------------------- editing

    def on_param_edited(self, index: QModelIndex, value: Any) -> None:
        item = index.internalPointer()
        if not isinstance(item, ParamItem):
            return
        if item.info.type not in (ParamType.STRING,) and isinstance(value, str):
            pass   # parsed by the model
        ok, msg = validate(value, item.info)
        if not ok and not (isinstance(value, str) and item.info.type.is_array):
            self.message(f'{item.full_name}: {msg}', 6000)
        if not self.model.set_pending(index, value):
            self.message(f'{item.full_name}: {item.message}', 6000)
            return
        if self.auto_apply.isChecked() and item.status == ParamStatus.MODIFIED:
            ms = int(self.rt.config.get('params.debounce_ms', 200))
            self._debounce.start(ms)
        self._update_status()

    def _send_pending(self) -> None:
        items = self.model.modified_items()
        if items:
            self._send({i.full_name: i.pending_value for i in items}, atomic=False)

    def _send(self, values: dict, atomic: bool) -> None:
        if not values or not self.can_write():
            return
        node = self.current_node
        critical = [n for n in values if self.rt.safety.is_critical_param(node, n)]
        if critical:
            text = ('These parameters are marked as critical:\n  ' + '\n  '.join(critical)
                    + f'\n\nSet them on {node}?')
            if not confirm(self, 'Critical parameters', text) or not confirm(
                    self, 'Confirm again', 'Are you sure? This may change the behaviour of '
                    'a real robot.'):
                return
        elif not self.confirm_action('Set parameters', f'Set {len(values)} parameter(s) on '
                                     f'{node}?', '\n'.join(f'{k} = {v!r}' for k, v in
                                                           values.items()), low_risk=True):
            return
        self.model.mark_pending_sent(list(values))
        fut = (self.svc.set_atomic if atomic else self.svc.set)(node, values)

        def ok(results):
            if node != self.current_node:
                return
            rejected = self.model.apply_results(results)
            if rejected:
                self.message('Rejected: ' + '; '.join(f'{r.name}: {r.reason}' for r in rejected),
                             10000)
            self._update_status()

        def err(exc):
            if node == self.current_node:
                self.model.apply_error(list(values), str(exc))
                self._update_status()
            self.message(f'set failed: {exc}', 8000)

        self.bridge.call(fut, ok, err)

    def apply_all(self) -> None:
        self._debounce.stop()
        items = self.model.modified_items()
        if not items:
            self.message('Nothing to apply')
            return
        self._send({i.full_name: i.pending_value for i in items},
                   atomic=self.atomic.isChecked())

    def revert_all(self) -> None:
        values = {}
        for item in self.model.items():
            v = self.model.revert(item.full_name)
            if v is not None:
                values[item.full_name] = v
        if values:
            self._send(values, atomic=self.atomic.isChecked())
        self._update_status()

    def discard_all(self) -> None:
        self._debounce.stop()
        self.model.discard_all()
        self._update_status()

    # ----------------------------------------------------------------- presets

    def _preset_dir(self) -> str:
        d = self.setting('preset_dir', os.path.join(user_dir(), 'param_presets'))
        os.makedirs(d, exist_ok=True)
        return d

    def save_preset(self) -> None:
        if not self.current_node:
            return
        default = os.path.join(self._preset_dir(),
                               self.current_node.strip('/').replace('/', '_') + '.yaml')
        path, _ = QFileDialog.getSaveFileName(self, 'Save preset', default, 'YAML (*.yaml *.yml)')
        if not path:
            return
        presets.save(path, self.current_node, self.model.current_params())
        self.set_setting('preset_dir', os.path.dirname(path))
        self.message(f'Preset saved to {path}')

    def load_preset(self) -> None:
        if not self.current_node:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Load preset', self._preset_dir(),
                                              'YAML (*.yaml *.yml);;All files (*)')
        if not path:
            return
        try:
            data = presets.load(path)
        except Exception as exc:  # noqa: BLE001
            self.error('Load preset', exc)
            return
        values = presets.values_for_node(data, self.current_node)
        if not values:
            info(self, 'Load preset', f'The preset has no values for {self.current_node}.\n'
                 f'Nodes in the file: {", ".join(data) or "none"}')
            return
        diffs = presets.diff(self.model.current_params(), values)
        changed, missing, errors = 0, [], []
        for d in diffs:
            if d.missing:
                missing.append(d.name)
            elif d.error:
                errors.append(f'{d.name}: {d.error}')
            else:
                item = self.model.item(d.name)
                if item is not None and item.info.descriptor.read_only:
                    errors.append(f'{d.name}: read-only')
                    continue
                self.model.set_pending(self.model.index_of(d.name), d.preset)
                changed += 1
        self.only_modified.setChecked(changed > 0)
        msg = f'{changed} parameter(s) differ from the preset and are now marked as modified.\n' \
              f'Press "Apply all" to send them.'
        if missing:
            msg += f'\n\nNot on the node ({len(missing)}): ' + ', '.join(missing[:20])
        if errors:
            msg += f'\n\nErrors ({len(errors)}):\n' + '\n'.join(errors[:20])
        info(self, 'Load preset', msg)
        self._update_status()

    # ----------------------------------------------------------------- context menu

    def _context_menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        item = idx.data(ItemRole)
        if not isinstance(item, ParamItem):
            return
        menu = QMenu(self)
        menu.addAction('Copy name', lambda: copy_to_clipboard(item.full_name))
        menu.addAction('Copy value', lambda: copy_to_clipboard(
            format_value(item.info.value, item.info.type)))
        menu.addAction('Copy "ros2 param set" command', lambda: copy_to_clipboard(
            'ros2 param set {} {} {}'.format(self.current_node, item.full_name, shlex.quote(
                format_value(item.display_value, item.info.type)))))
        menu.addSeparator()
        reset = menu.addAction('Reset to initial value', lambda: self._reset_one(item))
        reset.setEnabled(not self.rt.safety.read_only and not item.info.descriptor.read_only)
        if item.status == ParamStatus.EXTERNAL_CHANGE:
            menu.addAction('Keep my value', lambda: (self.model.resolve_external(
                item.full_name, True), self._update_status()))
            menu.addAction('Accept the external value', lambda: (self.model.resolve_external(
                item.full_name, False), self._update_status()))
        if item.status in (ParamStatus.MODIFIED, ParamStatus.REJECTED):
            menu.addAction('Apply this one', lambda: self._send(
                {item.full_name: item.pending_value}, atomic=False))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _reset_one(self, item: ParamItem) -> None:
        v = self.model.revert(item.full_name)
        if v is not None:
            self._send({item.full_name: v}, atomic=False)

    # ----------------------------------------------------------------- status

    def _update_status(self, extra: str = '') -> None:
        n = len(self.model.items())
        mod = len(self.model.modified_items())
        rejected = sum(1 for i in self.model.items() if i.status == ParamStatus.REJECTED)
        parts = [f'{n} parameters']
        if mod:
            parts.append(f'{mod} modified')
        if rejected:
            parts.append(f'{rejected} rejected')
        if self.model.stale:
            parts.append('STALE')
        if extra:
            parts.append(extra)
        self.status_label.setText(' · '.join(parts))


def _row(*widgets) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        else:
            lay.addWidget(w)
    return lay

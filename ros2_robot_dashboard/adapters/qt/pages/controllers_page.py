"""ros2_control page: controllers, switch with conflict preview, hardware interfaces/components."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QTabWidget,
                               QVBoxLayout, QWidget)

from ....core.domain.control import ControllerInfo, HwComponentInfo, SwitchPlan
from ....core.domain.events import ControllerManagersChanged, ControllerStateChanged
from .. import theme
from ..widgets.common import (Column, FilterEdit, RowsModel, TextFilterProxy, button,
                              make_table, selected_rows)
from .base_page import BasePage

STATE_COLOR = {'active': 'ok', 'inactive': 'warn', 'unconfigured': 'stale', 'finalized': 'error'}


def plan_text(plan: SwitchPlan) -> str:
    lines = []
    if plan.deactivate:
        lines.append('deactivate: ' + ', '.join(plan.deactivate))
    if plan.activate:
        lines.append('activate:   ' + ', '.join(plan.activate))
    if plan.released:
        lines.append('')
        lines.append('interfaces released:')
        lines += [f'  {i}  (from {o})' for i, o in plan.released]
    if plan.acquired:
        lines.append('')
        lines.append('interfaces claimed:')
        lines += [f'  {i}  (by {o})' for i, o in plan.acquired]
    if plan.conflicts:
        lines.append('')
        lines.append('CONFLICTS:')
        lines += [f'  {c.message}' for c in plan.conflicts]
    if plan.warnings:
        lines.append('')
        lines += [f'warning: {w}' for w in plan.warnings]
    return '\n'.join(lines)


class SwitchDialog(QDialog):
    """Shows the switch plan; lets the user pick strict/best effort and the timeout."""

    def __init__(self, plan: SwitchPlan, timeout: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Switch controllers')
        text = QPlainTextEdit(plan_text(plan))
        text.setReadOnly(True)
        text.setObjectName('terminal')
        self.strict = QCheckBox('Strict (fail if any controller cannot be switched)')
        self.strict.setChecked(True)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.5, 120)
        self.timeout.setValue(timeout)
        self.timeout.setSuffix(' s')
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok_btn = bb.addButton('Switch', QDialogButtonBox.AcceptRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        if plan.conflicts:
            warn = QLabel('The controller manager will probably refuse this switch.')
            warn.setStyleSheet(f'color: {theme.COLORS["error"]}; font-weight: bold;')
            lay.addWidget(warn)
        lay.addWidget(text, 1)
        row = QHBoxLayout()
        row.addWidget(self.strict)
        row.addStretch(1)
        row.addWidget(QLabel('Timeout'))
        row.addWidget(self.timeout)
        lay.addLayout(row)
        lay.addWidget(bb)
        self.resize(620, 420)


class ControllersPage(BasePage):
    page_id = 'controllers'
    title = 'Controllers'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.controllers
        self.manager = ''
        self._state: Optional[ControllerStateChanged] = None
        self._build()
        self.on_event(ControllerManagersChanged, self._on_managers)
        self.on_event(ControllerStateChanged, self._on_state)

    def _build(self) -> None:
        self.manager_combo = QComboBox()
        self.manager_combo.setMinimumWidth(260)
        self.manager_combo.currentTextChanged.connect(self._manager_changed)
        refresh = button('Refresh', lambda: self.manager and self.svc.refresh(self.manager))
        self.error_label = QLabel('')
        self.error_label.setStyleSheet(f'color: {theme.COLORS["error"]};')

        def st_fg(c: ControllerInfo):
            return theme.color(STATE_COLOR.get(c.state, 'stale'))

        self.ctrl_model = RowsModel([
            Column('Controller', lambda c: c.name),
            Column('State', lambda c: c.state, fg=st_fg),
            Column('Type', lambda c: c.type),
            Column('Update rate', lambda c: '' if c.update_rate is None else f'{c.update_rate} Hz',
                   sort=lambda c: c.update_rate or 0, align=Qt.AlignRight),
            Column('Claimed interfaces', lambda c: ', '.join(c.claimed_interfaces),
                   tooltip=lambda c: '\n'.join(c.claimed_interfaces)),
            Column('Required command interfaces',
                   lambda c: ', '.join(c.required_command_interfaces),
                   tooltip=lambda c: '\n'.join(c.required_command_interfaces)),
            Column('Chained', lambda c: 'chained' if c.is_chained else
                   ('chainable' if c.is_chainable else '')),
        ], key=lambda c: c.name)
        self.ctrl_proxy = TextFilterProxy(self)
        self.ctrl_proxy.setSourceModel(self.ctrl_model)
        self.ctrl_table = make_table(self.ctrl_model, self.ctrl_proxy)
        self.ctrl_table.setColumnWidth(0, 260)
        self.ctrl_table.setColumnWidth(2, 300)
        self.ctrl_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ctrl_table.customContextMenuRequested.connect(self._ctrl_menu)
        self.ctrl_filter = FilterEdit('Filter controllers…')
        self.ctrl_filter.textChanged.connect(self.ctrl_proxy.set_text)
        self.activate_btn = button('Activate', lambda: self._switch(activate=True))
        self.deactivate_btn = button('Deactivate', lambda: self._switch(activate=False))
        self.swap_btn = button('Switch…', self._switch_dialog,
                               'Activate the selected inactive controllers and deactivate the '
                               'selected active ones in one call')
        self.register_write_widget(self.activate_btn, self.deactivate_btn, self.swap_btn)
        ctrl_w = QWidget()
        cl = QVBoxLayout(ctrl_w)
        cl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (self.ctrl_filter, self.activate_btn, self.deactivate_btn, self.swap_btn):
            row.addWidget(w)
        cl.addLayout(row)
        cl.addWidget(self.ctrl_table, 1)
        self.plan_preview = QPlainTextEdit()
        self.plan_preview.setReadOnly(True)
        self.plan_preview.setObjectName('terminal')
        self.plan_preview.setMaximumHeight(120)
        self.plan_preview.setPlaceholderText('Select controllers to see what a switch would do')
        cl.addWidget(self.plan_preview)
        self.ctrl_table.selectionModel().selectionChanged.connect(self._preview)

        self.itf_model = RowsModel([
            Column('Interface', lambda i: i.name),
            Column('Kind', lambda i: i.kind),
            Column('Available', lambda i: 'yes' if i.is_available else 'no',
                   fg=lambda i: None if i.is_available else theme.color('error')),
            Column('Claimed', lambda i: 'yes' if i.is_claimed else ''),
            Column('Claimed by', lambda i: i.claimed_by),
            Column('Data type', lambda i: i.data_type),
        ], key=lambda i: (i.kind, i.name))
        self.itf_proxy = TextFilterProxy(self)
        self.itf_proxy.setSourceModel(self.itf_model)
        self.itf_table = make_table(self.itf_model, self.itf_proxy)
        self.itf_table.setColumnWidth(0, 320)
        self.itf_filter = FilterEdit('Filter interfaces…')
        self.itf_filter.textChanged.connect(self.itf_proxy.set_text)
        itf_w = QWidget()
        il = QVBoxLayout(itf_w)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(self.itf_filter)
        il.addWidget(self.itf_table, 1)

        self.comp_model = RowsModel([
            Column('Component', lambda c: c.name),
            Column('State', lambda c: c.state_label,
                   fg=lambda c: theme.color(STATE_COLOR.get(c.state_label, 'stale'))),
            Column('Type', lambda c: c.type),
            Column('Plugin', lambda c: c.plugin),
            Column('Command itf', lambda c: len(c.command_interfaces), align=Qt.AlignRight),
            Column('State itf', lambda c: len(c.state_interfaces), align=Qt.AlignRight),
        ], key=lambda c: c.name)
        self.comp_table = make_table(self.comp_model)
        self.comp_table.setColumnWidth(0, 240)
        self.comp_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.comp_table.customContextMenuRequested.connect(self._comp_menu)

        self.tabs = QTabWidget()
        self.tabs.addTab(ctrl_w, 'Controllers')
        self.tabs.addTab(itf_w, 'Hardware interfaces')
        self.tabs.addTab(self.comp_table, 'Hardware components')
        top = QHBoxLayout()
        top.addWidget(QLabel('Controller manager'))
        top.addWidget(self.manager_combo)
        top.addWidget(refresh)
        top.addWidget(self.error_label, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self._set_managers(self.svc.managers())
        if self.manager:
            self.svc.watch(self.manager, 'controllers_page')

    def on_leave(self) -> None:
        if self.manager:
            self.svc.unwatch(self.manager, 'controllers_page')

    def _on_managers(self, ev: ControllerManagersChanged) -> None:
        self._set_managers(ev.managers)

    def _set_managers(self, managers) -> None:
        cur = self.manager_combo.currentText()
        if [self.manager_combo.itemText(i) for i in range(self.manager_combo.count())] == \
                list(managers):
            return
        self.manager_combo.blockSignals(True)
        self.manager_combo.clear()
        self.manager_combo.addItems(list(managers))
        preferred = cur or self.setting('manager', '')
        if preferred in managers:
            self.manager_combo.setCurrentText(preferred)
        self.manager_combo.blockSignals(False)
        self._manager_changed(self.manager_combo.currentText())
        if not managers:
            self.error_label.setText('No controller manager found')

    def _manager_changed(self, name: str) -> None:
        if name == self.manager:
            return
        if self.manager and self.active:
            self.svc.unwatch(self.manager, 'controllers_page')
        self.manager = name
        self._state = None
        self.ctrl_model.set_rows([])
        self.itf_model.set_rows([])
        self.comp_model.set_rows([])
        if name:
            self.set_setting('manager', name)
            last = self.svc.last_state(name)
            if last is not None:
                self._on_state(last)
            if self.active:
                self.svc.watch(name, 'controllers_page')

    def _on_state(self, ev: ControllerStateChanged) -> None:
        if ev.manager != self.manager:
            return
        self._state = ev
        self.ctrl_model.set_rows(sorted(ev.controllers, key=lambda c: c.name))
        self.itf_model.set_rows(sorted(ev.interfaces, key=lambda i: (i.kind, i.name)))
        self.comp_model.set_rows(sorted(ev.components, key=lambda c: c.name))
        n_active = sum(1 for c in ev.controllers if c.state == 'active')
        self.tabs.setTabText(0, f'Controllers ({n_active}/{len(ev.controllers)} active)')
        self.tabs.setTabText(1, f'Hardware interfaces ({len(ev.interfaces)})')
        self.tabs.setTabText(2, f'Hardware components ({len(ev.components)})')
        self.error_label.setText(f'error: {ev.error}' if ev.error else '')
        self._preview()

    # ----------------------------------------------------------------- switch

    def _selected(self) -> list[ControllerInfo]:
        return selected_rows(self.ctrl_table, self.ctrl_model)

    def _preview(self, *_a) -> None:
        sel = self._selected()
        if not sel or not self.manager:
            self.plan_preview.clear()
            return
        act = [c.name for c in sel if c.state == 'inactive']
        deact = [c.name for c in sel if c.state == 'active']
        plan = self.svc.plan_switch(self.manager, act, deact)
        self.plan_preview.setPlainText(plan_text(plan) or 'Nothing to switch '
                                       '(only active/inactive controllers can be switched)')

    def _switch(self, activate: bool) -> None:
        sel = self._selected()
        names = [c.name for c in sel if c.state == ('inactive' if activate else 'active')]
        if not names:
            self.message('Select ' + ('inactive' if activate else 'active') + ' controllers')
            return
        self._do_switch(names if activate else [], [] if activate else names)

    def _switch_dialog(self) -> None:
        sel = self._selected()
        act = [c.name for c in sel if c.state == 'inactive']
        deact = [c.name for c in sel if c.state == 'active']
        if not act and not deact:
            self.message('Select active and/or inactive controllers')
            return
        self._do_switch(act, deact)

    def _do_switch(self, act: list[str], deact: list[str]) -> None:
        if not self.can_write():
            return
        plan = self.svc.plan_switch(self.manager, act, deact)
        dlg = SwitchDialog(plan, float(self.rt.config.get('timeouts.switch_controller_s', 5.0)),
                           self)
        if dlg.exec() != QDialog.Accepted:
            return
        manager = self.manager

        def ok(res):
            success, msg = res
            if success:
                self.message(f'Switch done on {manager}' + (f': {msg}' if msg else ''))
            else:
                self.error('Switch refused', msg or 'the controller manager refused the switch '
                           '(see its log in Diagnostics & logs)')

        self.call(self.svc.switch(manager, act, deact, dlg.strict.isChecked(),
                                  dlg.timeout.value()), ok, error_title='Switch controllers')

    # ----------------------------------------------------------------- menus

    def _ctrl_menu(self, pos) -> None:
        idx = self.ctrl_table.indexAt(pos)
        c = self.ctrl_model.row_at(idx)
        if c is None:
            return
        ro = self.rt.safety.read_only
        menu = QMenu(self)
        acts = []
        if c.state == 'inactive':
            acts.append(menu.addAction('Activate', lambda: self._do_switch([c.name], [])))
        if c.state == 'active':
            acts.append(menu.addAction('Deactivate', lambda: self._do_switch([], [c.name])))
        if c.state == 'unconfigured':
            acts.append(menu.addAction('Configure', lambda: self._simple('configure', c.name)))
        if c.state in ('inactive', 'unconfigured', 'finalized'):
            acts.append(menu.addAction('Unload', lambda: self._simple('unload', c.name)))
        if c.state == 'inactive' and self.rt.compat.capabilities().cleanup_controller:
            acts.append(menu.addAction('Cleanup', lambda: self._simple('cleanup', c.name)))
        menu.addSeparator()
        menu.addAction('Show parameters', lambda: self.ctx.navigate(
            'params', 'node', '/' + c.name if not c.name.startswith('/') else c.name))
        for a in acts:
            a.setEnabled(not ro)
        menu.exec(self.ctrl_table.viewport().mapToGlobal(pos))

    def _simple(self, action: str, name: str) -> None:
        if not self.confirm_action(action.capitalize(), f'{action} controller {name} on '
                                   f'{self.manager}?'):
            return
        fn = getattr(self.svc, action)

        def ok(res):
            success, msg = res
            if success:
                self.message(f'{action} {name}: ok')
            else:
                self.error(action, f'{action} {name} refused {msg}')

        self.call(fn(self.manager, name), ok, error_title=action)

    def _comp_menu(self, pos) -> None:
        c: Optional[HwComponentInfo] = self.comp_model.row_at(self.comp_table.indexAt(pos))
        if c is None:
            return
        menu = QMenu(self)
        for state in ('active', 'inactive', 'unconfigured'):
            a = menu.addAction(f'Set {state}', lambda s=state: self._set_hw(c.name, s))
            a.setEnabled(not self.rt.safety.read_only and c.state_label != state)
        menu.exec(self.comp_table.viewport().mapToGlobal(pos))

    def _set_hw(self, comp: str, state: str) -> None:
        if not self.confirm_action('Hardware component', f'Set {comp} to {state}?',
                                   'This changes the state of the hardware interface and may '
                                   'stop or start a real robot.'):
            return

        def ok(res):
            success, msg = res
            (self.message if success else lambda t: self.error('Hardware component', t))(
                f'{comp}: {msg}')

        self.call(self.svc.set_hardware_component_state(self.manager, comp, state), ok,
                  error_title='Hardware component')

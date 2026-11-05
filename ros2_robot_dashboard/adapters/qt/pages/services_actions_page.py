"""Services and actions: lists, schemas, call form, goals with live feedback."""

from __future__ import annotations

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)

from ....core.domain.events import GraphChanged
from ....core.msgs.introspection import (TypeUnavailable, get_action_class, get_service_class)
from ....core.services.interface_service import ActionFeedback, ActionStatusChanged
from ..widgets.common import Column, FilterEdit, RowsModel, TextFilterProxy, button, make_table
from ..widgets.message_form import MessageForm
from .base_page import BasePage


def _yaml(d) -> str:
    return yaml.safe_dump(d, default_flow_style=None, sort_keys=False, allow_unicode=True)


class _InterfacePanel(QWidget):
    """List + detail + form, shared by the services and actions tabs."""

    def __init__(self, page: 'ServicesActionsPage', kind: str):
        super().__init__()
        self.page = page
        self.kind = kind
        cols = [Column('Name', lambda s: s.name), Column('Type', lambda s: s.type),
                Column('Server', lambda s: ', '.join(s.servers) or '?')]
        self.model = RowsModel(cols, key=lambda s: s.name)
        self.proxy = TextFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = make_table(self.model, self.proxy)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 280)
        self.table.selectionModel().currentRowChanged.connect(self._selected)
        self.filter = FilterEdit('Filter by name, type or server…')
        self.filter.textChanged.connect(self.proxy.set_text)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.filter)
        ll.addWidget(self.table, 1)

        self.title = QLabel('Select an item')
        self.title.setObjectName('pageTitle')
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.schema = QPlainTextEdit()
        self.schema.setReadOnly(True)
        self.schema.setObjectName('terminal')
        self.schema.setMaximumHeight(140)
        self.form = MessageForm()
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 600)
        self.timeout.setValue(5.0)
        self.timeout.setSuffix(' s')
        verb = 'Call' if kind == 'service' else 'Send goal'
        self.call_btn = button(verb, self._call)
        self.cancel_btn = button('Cancel goal', self._cancel)
        self.cancel_btn.setVisible(kind == 'action')
        self.cancel_btn.setEnabled(False)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setObjectName('terminal')
        self.status = QLabel('')
        page.register_write_widget(self.call_btn)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.title)
        rl.addWidget(QLabel('Interface definition'))
        rl.addWidget(self.schema)
        rl.addWidget(QLabel('Request' if kind == 'service' else 'Goal'))
        rl.addWidget(self.form, 2)
        row = QHBoxLayout()
        if kind == 'service':
            row.addWidget(QLabel('Timeout'))
            row.addWidget(self.timeout)
        row.addWidget(self.call_btn)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        row.addWidget(self.status)
        rl.addLayout(row)
        rl.addWidget(QLabel('Response' if kind == 'service' else 'Feedback and result'))
        rl.addWidget(self.result, 2)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([600, 700])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)
        self.current = None
        self.goal_id = ''

    def set_items(self, items) -> None:
        self.model.set_rows(items)

    def select(self, name: str) -> None:
        self.filter.setText(name)
        for r in range(self.proxy.rowCount()):
            idx = self.proxy.index(r, 0)
            if idx.data() == name:
                self.table.setCurrentIndex(idx)
                return

    def _selected(self, current, _prev) -> None:
        item = self.model.row_at(current)
        if item is None:
            return
        self.current = item
        self.title.setText(f'{item.name}  [{item.type}]')
        self.result.clear()
        self.status.setText('')
        try:
            if self.kind == 'service':
                cls = get_service_class(item.type)
                self.form.set_type(cls.Request)
                self.schema.setPlainText(_definition(cls, ('Request', 'Response')))
            else:
                cls = get_action_class(item.type)
                self.form.set_type(cls.Goal)
                self.schema.setPlainText(_definition(cls, ('Goal', 'Result', 'Feedback')))
            self.call_btn.setEnabled(not self.page.rt.safety.read_only)
        except TypeUnavailable as exc:
            self.form.set_type(None)
            self.schema.setPlainText(str(exc))
            self.call_btn.setEnabled(False)

    def _call(self) -> None:
        item = self.current
        if item is None:
            return
        try:
            values = self.form.values()
        except Exception as exc:  # noqa: BLE001
            self.page.error('Invalid request', exc)
            return
        safety = self.page.rt.safety
        if safety.is_risky_service(item.name):
            if not self.page.confirm_action(
                    'Confirm call', f'"{item.name}" may have physical effects. Call it?',
                    _yaml(values)):
                return
        elif not self.page.can_write():
            return
        svc = self.page.rt.interfaces
        if self.kind == 'service':
            self.status.setText('calling…')

            def ok(res):
                self.status.setText('done')
                self.result.setPlainText(_yaml(res))

            def err(exc):
                self.status.setText(f'error: {exc}')
                self.result.setPlainText(f'# {type(exc).__name__}: {exc}')

            self.page.call(svc.call(item.name, item.type, values, timeout=self.timeout.value()),
                           ok, err)
        else:
            try:
                self.goal_id = svc.send_goal(item.name, item.type, values)
            except Exception as exc:  # noqa: BLE001
                self.page.error('Send goal', exc)
                return
            self.result.clear()
            self.cancel_btn.setEnabled(True)
            self.status.setText('sending…')

    def _cancel(self) -> None:
        if self.goal_id:
            self.page.call(self.page.rt.interfaces.cancel_goal(self.goal_id),
                           lambda ok: self.status.setText('cancel requested' if ok
                                                          else 'cancel refused'))

    def on_feedback(self, ev: ActionFeedback) -> None:
        if ev.goal_id == self.goal_id:
            self.result.appendPlainText('# feedback\n' + _yaml(ev.feedback))

    def on_status(self, ev: ActionStatusChanged) -> None:
        if ev.goal_id != self.goal_id:
            return
        self.status.setText(ev.status + (f': {ev.message}' if ev.message else ''))
        if ev.result is not None:
            self.result.appendPlainText(f'# result ({ev.status})\n' + _yaml(ev.result))
        if ev.status in ('succeeded', 'aborted', 'canceled', 'rejected', 'error'):
            self.cancel_btn.setEnabled(False)


def _definition(cls, parts) -> str:
    out = []
    for part in parts:
        sub = getattr(cls, part)
        out.append(f'# {part}')
        for name, t in sub.get_fields_and_field_types().items():
            out.append(f'{t} {name}')
    return '\n'.join(out)


class ServicesActionsPage(BasePage):
    page_id = 'services'
    title = 'Services / Actions'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tabs = QTabWidget()
        self.services = _InterfacePanel(self, 'service')
        self.actions = _InterfacePanel(self, 'action')
        self.tabs.addTab(self.services, 'Services')
        self.tabs.addTab(self.actions, 'Actions')
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.on_event(GraphChanged, self._on_graph)
        self.on_event(ActionFeedback, self.actions.on_feedback)
        self.on_event(ActionStatusChanged, self.actions.on_status)

    def on_enter(self) -> None:
        self._fill(self.rt.graph.snapshot())

    def _on_graph(self, ev: GraphChanged) -> None:
        if self.active:
            self._fill(ev.graph)

    def _fill(self, snap) -> None:
        hide = self.rt.graph.is_hidden_node
        self.services.set_items([s for s in snap.services
                                 if not s.servers or not all(hide(n) for n in s.servers)])
        self.actions.set_items(snap.actions)
        self.tabs.setTabText(0, f'Services ({len(self.services.model.rows)})')
        self.tabs.setTabText(1, f'Actions ({len(snap.actions)})')

    def focus_item(self, kind: str, name: str) -> None:
        if kind == 'action':
            self.tabs.setCurrentWidget(self.actions)
            self.actions.select(name)
        else:
            self.tabs.setCurrentWidget(self.services)
            self.services.select(name)

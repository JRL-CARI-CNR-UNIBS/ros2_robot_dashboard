"""Form generated from a message type, with a free YAML mode for experts."""

from __future__ import annotations

from typing import Any, Optional

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ....core.msgs.introspection import (INTEGER_TYPES, FieldSpec, default_request_dict,
                                         message_schema)

SPEC_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2     # 'field' | 'element'


class MessageForm(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.msg_cls = None
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(['Field', 'Value', 'Type'])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 260)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._edit)
        self.yaml_edit = QPlainTextEdit()
        self.yaml_edit.setObjectName('terminal')
        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.yaml_edit)
        self.yaml_mode = QCheckBox('YAML')
        self.yaml_mode.setToolTip('Edit the request as free YAML')
        self.yaml_mode.toggled.connect(self._toggle_yaml)
        self.hint = QLabel('Double-click a value to edit it; right-click an array to add or '
                           'remove elements.')
        self.hint.setStyleSheet('color: gray;')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(self.hint, 1)
        top.addWidget(self.yaml_mode)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)

    # ----------------------------------------------------------------- content

    def set_type(self, msg_cls, values: Optional[dict] = None) -> None:
        self.msg_cls = msg_cls
        self.tree.clear()
        if msg_cls is None:
            self.yaml_edit.setPlainText('')
            return
        values = values if values is not None else default_request_dict(msg_cls)
        schema = message_schema(msg_cls)
        for spec in schema:
            self._add_field(self.tree.invisibleRootItem(), spec, values.get(spec.name))
        if not schema:
            QTreeWidgetItem(self.tree, ['(empty message: nothing to fill in)'])
        self.tree.expandToDepth(1)
        if self.yaml_mode.isChecked():
            self.yaml_edit.setPlainText(_dump(values))

    def _add_field(self, parent, spec: FieldSpec, value: Any, label: str = '',
                   kind: str = 'field') -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent)
        it.setText(0, label or spec.name)
        it.setText(2, spec.label() if kind == 'field' else spec.type_name)
        it.setData(0, SPEC_ROLE, spec)
        it.setData(0, KIND_ROLE, kind)
        is_array = (spec.is_sequence or spec.is_fixed_array) and kind == 'field'
        if is_array:
            items = list(value or [])
            for i, v in enumerate(items):
                self._add_element(it, spec, v, i)
            it.setText(1, f'[{len(items)} elements]')
        elif spec.is_message:
            for child in spec.children:
                self._add_field(it, child, (value or {}).get(child.name) if isinstance(
                    value, dict) else None)
        else:
            it.setText(1, _fmt(value, spec))
            it.setFlags(it.flags() | Qt.ItemIsEditable)
        return it

    def _add_element(self, parent: QTreeWidgetItem, spec: FieldSpec, value: Any, i: int) -> None:
        elem_spec = FieldSpec(spec.name, spec.type_name, is_message=spec.is_message,
                              children=spec.children)
        self._add_field(parent, elem_spec, value, label=f'[{i}]', kind='element')

    def _edit(self, item: QTreeWidgetItem, column: int) -> None:
        if item.flags() & Qt.ItemIsEditable:
            self.tree.editItem(item, 1)

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        spec: FieldSpec = item.data(0, SPEC_ROLE)
        kind = item.data(0, KIND_ROLE)
        menu = QMenu(self)
        if kind == 'field' and spec.is_sequence:
            menu.addAction('Add element', lambda: self._append(item, spec))
        if kind == 'element':
            parent = item.parent()
            pspec = parent.data(0, SPEC_ROLE)
            if pspec.is_sequence:
                menu.addAction('Remove element', lambda: self._remove(parent, item))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _append(self, item, spec) -> None:
        if spec.size and item.childCount() >= spec.size:
            return
        default: Any = {} if spec.is_message else _default(spec)
        if spec.is_message and self.msg_cls is not None:
            from ....core.msgs.introspection import get_message_class
            try:
                default = default_request_dict(get_message_class(spec.type_name))
            except Exception:  # noqa: BLE001
                default = {}
        self._add_element(item, spec, default, item.childCount())
        item.setText(1, f'[{item.childCount()} elements]')
        item.setExpanded(True)

    def _remove(self, parent, item) -> None:
        parent.removeChild(item)
        for i in range(parent.childCount()):
            parent.child(i).setText(0, f'[{i}]')
        parent.setText(1, f'[{parent.childCount()} elements]')

    # ----------------------------------------------------------------- values

    def values(self) -> dict:
        """The request as a dict (raises ValueError on parse errors)."""
        if self.yaml_mode.isChecked():
            data = yaml.safe_load(self.yaml_edit.toPlainText()) or {}
            if not isinstance(data, dict):
                raise ValueError('YAML must be a mapping of field names')
            return data
        return self._values_from_tree()

    def _value(self, item: QTreeWidgetItem) -> Any:
        spec: FieldSpec = item.data(0, SPEC_ROLE)
        kind = item.data(0, KIND_ROLE)
        if (spec.is_sequence or spec.is_fixed_array) and kind == 'field':
            return [self._value(item.child(i)) for i in range(item.childCount())]
        if spec.is_message:
            return {item.child(i).text(0): self._value(item.child(i))
                    for i in range(item.childCount())}
        return _parse(item.text(1), spec, item.text(0))

    def _toggle_yaml(self, on: bool) -> None:
        if on:
            try:
                self.yaml_edit.setPlainText(_dump(self._values_from_tree()))
            except ValueError as exc:
                self.yaml_edit.setPlainText(f'# {exc}\n')
            self.stack.setCurrentWidget(self.yaml_edit)
        else:
            try:
                data = yaml.safe_load(self.yaml_edit.toPlainText()) or {}
                if self.msg_cls is not None and isinstance(data, dict):
                    self.set_type(self.msg_cls, _merge(default_request_dict(self.msg_cls), data))
            except yaml.YAMLError:
                pass
            self.stack.setCurrentWidget(self.tree)

    def _values_from_tree(self) -> dict:
        root = self.tree.invisibleRootItem()
        return {root.child(i).text(0): self._value(root.child(i)) for i in range(
            root.childCount()) if root.child(i).data(0, SPEC_ROLE) is not None}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _dump(values: dict) -> str:
    return yaml.safe_dump(values, default_flow_style=None, sort_keys=False, allow_unicode=True)


def _default(spec: FieldSpec) -> Any:
    if spec.type_name in ('bool', 'boolean'):
        return False
    if spec.type_name in INTEGER_TYPES:
        return 0
    if spec.is_numeric:
        return 0.0
    return ''


def _fmt(value: Any, spec: FieldSpec) -> str:
    if value is None:
        value = _default(spec)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (bytes, bytearray)):
        return str(value[0]) if value else '0'
    return str(value)


def _parse(text: str, spec: FieldSpec, name: str) -> Any:
    t = spec.type_name
    s = text.strip()
    try:
        if t in ('bool', 'boolean'):
            if s.lower() in ('true', '1', 'yes', 'on'):
                return True
            if s.lower() in ('false', '0', 'no', 'off', ''):
                return False
            raise ValueError
        if t in INTEGER_TYPES:
            return int(s or '0', 0)
        if spec.is_numeric:
            return float(s or '0')
    except ValueError:
        raise ValueError(f"field '{name}': '{text}' is not a valid {t}") from None
    return text

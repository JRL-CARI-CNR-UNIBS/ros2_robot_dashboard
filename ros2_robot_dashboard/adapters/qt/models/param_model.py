"""Parameter tree model, filter proxy and node list model (design v1, section 5.6)."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import (QAbstractItemModel, QAbstractListModel, QModelIndex,
                            QSortFilterProxyModel, Qt)
from PySide6.QtGui import QBrush

from ....core.domain.params import (NodeStatus, ParamInfo, ParamStatus, ParamType, SetResult,
                                    coerce_value, describe_range, format_value, values_equal)
from .. import theme
from ..widgets.common import refilter

ParamInfoRole = Qt.UserRole + 10
StatusRole = Qt.UserRole + 11
PendingRole = Qt.UserRole + 12
DescriptorRole = Qt.UserRole + 13
ItemRole = Qt.UserRole + 14

COL_NAME, COL_VALUE, COL_TYPE, COL_STATUS = range(4)
HEADERS = ['Name', 'Value', 'Type', 'Status']


class TreeItem:
    def __init__(self, label: str, parent: Optional['TreeItem'] = None):
        self.label = label
        self.parent = parent
        self.children: list[TreeItem] = []

    def child(self, row: int) -> Optional['TreeItem']:
        return self.children[row] if 0 <= row < len(self.children) else None

    def row(self) -> int:
        return self.parent.children.index(self) if self.parent else 0

    def append(self, child: 'TreeItem') -> None:
        child.parent = self
        self.children.append(child)


class NamespaceItem(TreeItem):
    def __init__(self, label: str, prefix: str, parent=None):
        super().__init__(label, parent)
        self.prefix = prefix


class ParamItem(TreeItem):
    def __init__(self, info: ParamInfo, parent=None):
        super().__init__(info.name.rsplit('.', 1)[-1], parent)
        self.full_name = info.name
        self.info = info
        self.initial_value = info.value
        self.pending_value: Any = None
        self.external_value: Any = None
        self.status = ParamStatus.OK
        self.message = ''

    def is_modified(self) -> bool:
        return self.status == ParamStatus.MODIFIED

    @property
    def display_value(self) -> Any:
        if self.status in (ParamStatus.MODIFIED, ParamStatus.PENDING,
                           ParamStatus.EXTERNAL_CHANGE) and self.pending_value is not None:
            return self.pending_value
        return self.info.value


class ParamTreeModel(QAbstractItemModel):
    """Parameters of the selected node, grouped by the '.' namespace separator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = TreeItem('')
        self.node_name = ''
        self._items: dict[str, ParamItem] = {}
        self.stale = False

    # ----------------------------------------------------------------- Qt API

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        p = parent.internalPointer() if parent.isValid() else self.root
        c = p.child(row)
        return self.createIndex(row, column, c) if c else QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        item = index.internalPointer()
        p = item.parent
        if p is None or p is self.root:
            return QModelIndex()
        return self.createIndex(p.row(), 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        p = parent.internalPointer() if parent.isValid() else self.root
        return len(p.children)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        item = index.internalPointer()
        if (isinstance(item, ParamItem) and index.column() == COL_VALUE
                and not item.info.descriptor.read_only and item.status != ParamStatus.STALE
                and not self.stale):
            f |= Qt.ItemIsEditable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = index.internalPointer()
        col = index.column()
        if role == ItemRole:
            return item
        if isinstance(item, NamespaceItem):
            if role == Qt.DisplayRole and col == COL_NAME:
                return item.label
            return None
        if not isinstance(item, ParamItem):
            return None
        info = item.info
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return item.label
            if col == COL_VALUE:
                v = item.display_value
                ptype = info.type
                if ptype == ParamType.STRING and isinstance(v, str) and len(v) > 200:
                    return v[:200].replace('\n', ' ') + '…'
                return format_value(v, ptype) if v is not None else ''
            if col == COL_TYPE:
                return info.type.label + (' (ro)' if info.descriptor.read_only else '')
            if col == COL_STATUS:
                s = item.status.value.replace('_', ' ')
                return s + (f': {item.message}' if item.message else '')
        if role == Qt.EditRole and col == COL_VALUE:
            return item.display_value
        if role == Qt.BackgroundRole:
            return _status_brush(item.status if not self.stale else ParamStatus.STALE)
        if role == Qt.ForegroundRole and (self.stale or item.status == ParamStatus.STALE
                                          or info.descriptor.read_only):
            return QBrush(theme.color('stale'))
        if role == Qt.ToolTipRole:
            lines = [f'<b>{item.full_name}</b> ({info.type.label})']
            if info.descriptor.description:
                lines.append(info.descriptor.description)
            rng = describe_range(info.descriptor)
            if rng:
                lines.append(f'range: {rng}')
            if info.descriptor.additional_constraints:
                lines.append(f'constraints: {info.descriptor.additional_constraints}')
            if info.descriptor.read_only:
                lines.append('read-only')
            if item.status == ParamStatus.EXTERNAL_CHANGE:
                lines.append(f'changed outside the dashboard to: '
                             f'{format_value(item.external_value, info.type)}')
            if item.message:
                lines.append(item.message)
            return '<br>'.join(lines)
        if role == ParamInfoRole:
            return info
        if role == StatusRole:
            return item.status
        if role == PendingRole:
            return item.pending_value
        if role == DescriptorRole:
            return info.descriptor
        return None

    def setData(self, index, value, role=Qt.EditRole):
        item = index.internalPointer() if index.isValid() else None
        if not isinstance(item, ParamItem):
            return False
        if role == Qt.EditRole:
            return self.set_pending(index, value)
        return False

    # ----------------------------------------------------------------- content

    def set_node(self, node_name: str) -> None:
        self.beginResetModel()
        self.node_name = node_name
        self.root = TreeItem('')
        self._items.clear()
        self.stale = False
        self.endResetModel()

    def load(self, node_name: str, params: list[ParamInfo]) -> None:
        self.beginResetModel()
        self.node_name = node_name
        self.root = TreeItem('')
        self._items.clear()
        self.stale = False
        namespaces: dict[str, TreeItem] = {'': self.root}
        for info in sorted(params, key=lambda p: p.name):
            parts = info.name.split('.')
            parent = self.root
            prefix = ''
            for part in parts[:-1]:
                prefix = f'{prefix}.{part}' if prefix else part
                ns = namespaces.get(prefix)
                if ns is None:
                    ns = NamespaceItem(part, prefix)
                    parent.append(ns)
                    namespaces[prefix] = ns
                parent = ns
            item = ParamItem(info)
            parent.append(item)
            self._items[info.name] = item
        self.endResetModel()

    def item(self, name: str) -> Optional[ParamItem]:
        return self._items.get(name)

    def items(self) -> list[ParamItem]:
        return list(self._items.values())

    def index_of(self, name: str, column: int = 0) -> QModelIndex:
        item = self._items.get(name)
        if item is None:
            return QModelIndex()
        return self.createIndex(item.row(), column, item)

    def _changed(self, item: ParamItem) -> None:
        left = self.createIndex(item.row(), 0, item)
        right = self.createIndex(item.row(), 3, item)
        self.dataChanged.emit(left, right)

    def set_pending(self, index: QModelIndex, value: Any) -> bool:
        item = index.internalPointer()
        if not isinstance(item, ParamItem):
            return False
        ptype = item.info.type
        try:
            if isinstance(value, str) and ptype not in (ParamType.STRING,):
                value = coerce_value(value, ptype)
            elif ptype != ParamType.NOT_SET:
                value = coerce_value(value, ptype)
        except ValueError as exc:
            item.message = str(exc)
            item.status = ParamStatus.REJECTED
            self._changed(item)
            return False
        if values_equal(value, item.info.value) and item.status in (ParamStatus.MODIFIED,
                                                                    ParamStatus.REJECTED):
            item.pending_value = None
            item.status = ParamStatus.OK
            item.message = ''
        elif not values_equal(value, item.info.value):
            item.pending_value = value
            item.status = ParamStatus.MODIFIED
            item.message = ''
        self._changed(item)
        return True

    def mark_pending_sent(self, names) -> None:
        for n in names:
            item = self._items.get(n)
            if item is not None and item.status == ParamStatus.MODIFIED:
                item.status = ParamStatus.PENDING
                self._changed(item)

    def apply_results(self, results: list[SetResult]) -> list[SetResult]:
        rejected = []
        for r in results:
            item = self._items.get(r.name)
            if item is None:
                continue
            if r.successful:
                if item.pending_value is not None:
                    item.info = ParamInfo(item.info.name, item.info.type, item.pending_value,
                                          item.info.descriptor)
                item.pending_value = None
                item.status = ParamStatus.OK
                item.message = ''
            else:
                item.status = ParamStatus.REJECTED
                item.message = r.reason or 'rejected'
                rejected.append(r)
            self._changed(item)
        return rejected

    def apply_error(self, names, message: str) -> None:
        for n in names:
            item = self._items.get(n)
            if item is not None:
                item.status = ParamStatus.REJECTED
                item.message = message
                self._changed(item)

    def apply_event(self, info: ParamInfo, kind: str) -> str:
        """Apply a /parameter_events update. Returns what happened (for tests/logging)."""
        item = self._items.get(info.name)
        if kind == 'deleted':
            if item is not None:
                item.status = ParamStatus.STALE
                item.message = 'deleted'
                self._changed(item)
            return 'deleted'
        if item is None:
            if kind == 'new':
                # New parameter: reload is simpler than inserting into the tree.
                return 'new'
            return 'ignored'
        # Echo of our own set (same value while PENDING): ignore (v1, 5.7).
        if item.status == ParamStatus.PENDING and values_equal(info.value, item.pending_value):
            return 'echo'
        if item.status == ParamStatus.MODIFIED:
            item.external_value = info.value
            item.status = ParamStatus.EXTERNAL_CHANGE
            item.message = 'changed outside the dashboard'
            self._changed(item)
            return 'external_change'
        item.info = ParamInfo(item.info.name, info.type if info.type != ParamType.NOT_SET
                              else item.info.type, info.value, item.info.descriptor)
        if item.status != ParamStatus.PENDING:
            item.status = ParamStatus.OK
            item.message = ''
        self._changed(item)
        return 'updated'

    def resolve_external(self, name: str, keep_mine: bool) -> None:
        item = self._items.get(name)
        if item is None or item.status != ParamStatus.EXTERNAL_CHANGE:
            return
        item.info = ParamInfo(item.info.name, item.info.type, item.external_value,
                              item.info.descriptor)
        if keep_mine:
            item.status = ParamStatus.MODIFIED
        else:
            item.pending_value = None
            item.status = ParamStatus.OK
        item.message = ''
        self._changed(item)

    def mark_stale(self, stale: bool = True) -> None:
        self.stale = stale
        if self._items:
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 3))
        self.layoutChanged.emit()

    def modified_items(self) -> list[ParamItem]:
        return [i for i in self._items.values() if i.status == ParamStatus.MODIFIED]

    def revert(self, name: str) -> Any:
        """Back to the initial value; returns the value to send (or None)."""
        item = self._items.get(name)
        if item is None:
            return None
        if item.status in (ParamStatus.MODIFIED, ParamStatus.REJECTED):
            item.pending_value = None
            item.status = ParamStatus.OK
            item.message = ''
            self._changed(item)
        if not values_equal(item.info.value, item.initial_value):
            item.pending_value = item.initial_value
            item.status = ParamStatus.MODIFIED
            self._changed(item)
            return item.initial_value
        return None

    def discard_all(self) -> None:
        for item in self._items.values():
            if item.status in (ParamStatus.MODIFIED, ParamStatus.REJECTED,
                               ParamStatus.EXTERNAL_CHANGE):
                if item.status == ParamStatus.EXTERNAL_CHANGE:
                    item.info = ParamInfo(item.info.name, item.info.type, item.external_value,
                                          item.info.descriptor)
                item.pending_value = None
                item.status = ParamStatus.OK
                item.message = ''
                self._changed(item)

    def current_params(self) -> list[ParamInfo]:
        return [i.info for i in self._items.values()]


def _status_brush(status: ParamStatus) -> Optional[QBrush]:
    key = {
        ParamStatus.MODIFIED: 'modified_bg', ParamStatus.PENDING: 'pending_bg',
        ParamStatus.REJECTED: 'rejected_bg', ParamStatus.EXTERNAL_CHANGE: 'external_bg',
        ParamStatus.STALE: 'stale_bg',
    }.get(status)
    return QBrush(theme.color(key)) if key else None


class ParamFilterProxy(QSortFilterProxyModel):
    """Recursive filter: a namespace stays visible if a child matches. 'Only modified'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ''
        self._only_modified = False
        self._hidden_regex = None
        self.setRecursiveFilteringEnabled(True)

    def set_text(self, text: str) -> None:
        refilter(self, lambda: setattr(self, '_text', text.strip().lower()))

    def set_only_modified(self, value: bool) -> None:
        refilter(self, lambda: setattr(self, '_only_modified', value))

    def set_hidden_regex(self, pattern) -> None:
        refilter(self, lambda: setattr(self, '_hidden_regex', pattern))

    def filterAcceptsRow(self, row, parent) -> bool:
        idx = self.sourceModel().index(row, 0, parent)
        item = idx.data(ItemRole)
        if not isinstance(item, ParamItem):
            return False if (self._text or self._only_modified) else True
        if self._hidden_regex is not None and self._hidden_regex.match(item.full_name):
            return False
        if self._only_modified and item.status not in (ParamStatus.MODIFIED, ParamStatus.PENDING,
                                                       ParamStatus.REJECTED,
                                                       ParamStatus.EXTERNAL_CHANGE):
            return False
        if self._text and self._text not in item.full_name.lower():
            return False
        return True


_NODE_ICON = {
    NodeStatus.DISCOVERED: 'stale', NodeStatus.LOADING: 'warn', NodeStatus.READY: 'ok',
    NodeStatus.UNREACHABLE: 'error', NodeStatus.GONE: 'stale',
}


class NodeListModel(QAbstractListModel):
    """Nodes with a status color; removed nodes stay as GONE until they come back."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes: list[str] = []
        self.status: dict[str, NodeStatus] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.nodes)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        name = self.nodes[index.row()]
        st = self.status.get(name, NodeStatus.DISCOVERED)
        if role == Qt.DisplayRole:
            return name + ('  (gone)' if st == NodeStatus.GONE else '')
        if role == Qt.DecorationRole:
            from PySide6.QtGui import QColor, QPixmap, QPainter, QIcon
            pm = QPixmap(12, 12)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(theme.color(_NODE_ICON[st]))
            p.setPen(theme.color(_NODE_ICON[st]))
            p.drawEllipse(1, 1, 10, 10)
            p.end()
            return QIcon(pm)
        if role == Qt.ForegroundRole and st == NodeStatus.GONE:
            return QBrush(theme.color('stale'))
        if role == Qt.ToolTipRole:
            return f'{name}: {st.value}'
        if role == Qt.UserRole:
            return name
        return None

    def update_nodes(self, names: list[str]) -> None:
        """Insert/remove rows without a reset (keeps the selection)."""
        current = set(names)
        for name in list(self.nodes):
            if name not in current and self.status.get(name) != NodeStatus.GONE:
                if self.status.get(name) in (NodeStatus.READY, NodeStatus.LOADING,
                                             NodeStatus.UNREACHABLE):
                    self.set_status(name, NodeStatus.GONE)
                else:
                    row = self.nodes.index(name)
                    self.beginRemoveRows(QModelIndex(), row, row)
                    self.nodes.pop(row)
                    self.endRemoveRows()
        for name in sorted(current):
            if name in self.nodes:
                if self.status.get(name) == NodeStatus.GONE:
                    self.set_status(name, NodeStatus.DISCOVERED)
                continue
            row = 0
            while row < len(self.nodes) and self.nodes[row] < name:
                row += 1
            self.beginInsertRows(QModelIndex(), row, row)
            self.nodes.insert(row, name)
            self.endInsertRows()

    def remove(self, name: str) -> None:
        if name in self.nodes:
            row = self.nodes.index(name)
            self.beginRemoveRows(QModelIndex(), row, row)
            self.nodes.pop(row)
            self.endRemoveRows()
            self.status.pop(name, None)

    def set_status(self, name: str, status: NodeStatus) -> None:
        self.status[name] = status
        if name in self.nodes:
            idx = self.index(self.nodes.index(name))
            self.dataChanged.emit(idx, idx)

    def row_of(self, name: str) -> int:
        return self.nodes.index(name) if name in self.nodes else -1

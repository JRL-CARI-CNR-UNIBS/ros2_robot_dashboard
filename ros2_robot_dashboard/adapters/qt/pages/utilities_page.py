"""Utilities: live plot of numeric fields, parameter comparison (node vs node / preset)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from ....core import presets
from ....core.domain.events import PlotSample
from .. import theme
from ..widgets.common import Column, FilterEdit, RowsModel, TextFilterProxy, button, make_table
from ..widgets.plot import HAS_PG, TimePlot
from .base_page import BasePage


def _fmt(v, present: bool) -> str:
    if not present:
        return '—'
    if isinstance(v, (list, tuple)):
        return '[' + ', '.join(str(x) for x in v) + ']'
    return str(v)


class PlotTab(QWidget):
    def __init__(self, page: 'UtilitiesPage'):
        super().__init__()
        self.page = page
        self.sources: dict[str, str] = {}      # sid -> series key
        self.topic = QComboBox()
        self.topic.setEditable(True)
        self.topic.setMinimumWidth(260)
        self.topic.currentTextChanged.connect(self._topic_changed)
        self.field = QComboBox()
        self.field.setEditable(True)
        self.field.setMinimumWidth(260)
        self.field.setToolTip('Field path, e.g. position[0] or pose.position.x')
        self.rate = QDoubleSpinBox()
        self.rate.setRange(1, 500)
        self.rate.setValue(50)
        self.rate.setSuffix(' Hz max')
        add = button('Add', self._add)
        remove = button('Remove selected', self._remove)
        self.plot = TimePlot(20.0)
        self.series = QListWidget()
        self.series.setMaximumWidth(320)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (QLabel('Topic'), self.topic, QLabel('Field'), self.field, self.rate, add,
                  remove):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        split = QSplitter()
        split.addWidget(self.plot)
        split.addWidget(self.series)
        split.setSizes([1000, 260])
        lay.addWidget(split, 1)
        if not HAS_PG:
            lay.addWidget(QLabel('pyqtgraph is not installed: plots are disabled.'))

    def refresh_topics(self) -> None:
        cur = self.topic.currentText()
        names = sorted(t.name for t in self.page.rt.graph.snapshot().topics if t.publishers)
        if [self.topic.itemText(i) for i in range(self.topic.count())] != names:
            self.topic.blockSignals(True)
            self.topic.clear()
            self.topic.addItems(names)
            self.topic.setEditText(cur)
            self.topic.blockSignals(False)

    def _topic_changed(self, topic: str) -> None:
        self.field.clear()
        if not topic:
            return
        try:
            fields = self.page.rt.utility.numeric_fields(topic)
        except Exception:  # noqa: BLE001
            fields = []
        self.field.addItems(fields)

    def _add(self) -> None:
        topic, path = self.topic.currentText().strip(), self.field.currentText().strip()
        if not topic or not path:
            return
        key = f'{topic}/{path}'
        if key in self.sources.values():
            return
        try:
            sid = self.page.rt.utility.start_plot(topic, path, self.rate.value())
        except Exception as exc:  # noqa: BLE001
            self.page.error('Plot', exc)
            return
        self.sources[sid] = key
        self.plot.add_series(key, key)
        it = QListWidgetItem(key)
        it.setData(Qt.UserRole, sid)
        self.series.addItem(it)

    def _remove(self) -> None:
        for it in self.series.selectedItems():
            sid = it.data(Qt.UserRole)
            self.page.rt.utility.stop_plot(sid)
            self.plot.remove_series(self.sources.pop(sid))
            self.series.takeItem(self.series.row(it))

    def stop_all(self) -> None:
        for sid in list(self.sources):
            self.page.rt.utility.stop_plot(sid)
        self.sources.clear()
        self.series.clear()
        self.plot.clear()

    def on_sample(self, ev: PlotSample) -> None:
        key = self.sources.get(ev.source_id)
        if key is not None:
            self.plot.add_point(key, ev.t, ev.value)


class CompareTab(QWidget):
    def __init__(self, page: 'UtilitiesPage'):
        super().__init__()
        self.page = page
        self.left = QComboBox()
        self.left.setMinimumWidth(260)
        self.right = QComboBox()
        self.right.setMinimumWidth(260)
        self.right.setEditable(False)
        cmp_btn = button('Compare nodes', self._compare_nodes)
        preset_btn = button('Compare left node with a preset file…', self._compare_preset)
        self.only_diff = QCheckBox('Only differences')
        self.only_diff.setChecked(True)
        self.only_diff.toggled.connect(lambda v: self.proxy.set_predicate(
            (lambda c: not c.equal) if v else None))
        self.filter = FilterEdit('Filter parameters…')
        self.right_label = 'right'

        def diff_bg(c):
            if c.equal:
                return None
            if not (c.left_present and c.right_present):
                return theme.color('stale_bg')
            return theme.color('modified_bg')

        self.model = RowsModel([
            Column('Parameter', lambda c: c.name),
            Column('Left', lambda c: _fmt(c.left, c.left_present), bg=diff_bg),
            Column('Right', lambda c: _fmt(c.right, c.right_present), bg=diff_bg),
        ], key=lambda c: c.name)
        self.proxy = TextFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.set_predicate(lambda c: not c.equal)
        self.filter.textChanged.connect(self.proxy.set_text)
        self.table = make_table(self.model, self.proxy)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 300)
        self.summary = QLabel('')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (QLabel('Left'), self.left, QLabel('Right'), self.right, cmp_btn, preset_btn):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.only_diff)
        lay.addLayout(row)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.summary)

    def refresh_nodes(self) -> None:
        names = sorted(n.fqn for n in self.page.rt.graph.snapshot().nodes
                       if not self.page.rt.graph.is_hidden_node(n.fqn))
        for c in (self.left, self.right):
            if [c.itemText(i) for i in range(c.count())] != names:
                cur = c.currentText()
                c.clear()
                c.addItems(names)
                if cur in names:
                    c.setCurrentText(cur)

    def _show(self, rows, right_label: str) -> None:
        self.model.columns[2].header = right_label
        self.model.set_rows(rows)
        self.model.headerDataChanged.emit(Qt.Horizontal, 0, 2)
        diff = sum(1 for c in rows if not c.equal)
        self.summary.setText(f'{len(rows)} parameters, {diff} different')

    def _compare_nodes(self) -> None:
        left, right = self.left.currentText(), self.right.currentText()
        if not left or not right:
            return
        self.summary.setText('loading…')
        self.page.call(self.page.rt.utility.compare_params(left, right),
                       lambda rows: self._show(rows, right), error_title='Compare')

    def _compare_preset(self) -> None:
        left = self.left.currentText()
        if not left:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Preset', os.path.expanduser('~'),
                                              'YAML (*.yaml *.yml)')
        if not path:
            return
        try:
            values = presets.values_for_node(presets.load(path), left)
        except Exception as exc:  # noqa: BLE001
            self.page.error('Preset', exc)
            return
        self.page.call(self.page.rt.utility.compare_with_values(left, values),
                       lambda rows: self._show(rows, os.path.basename(path)),
                       error_title='Compare')


class UtilitiesPage(BasePage):
    page_id = 'utilities'
    title = 'Utilities'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tabs = QTabWidget()
        self.plot = PlotTab(self)
        self.compare = CompareTab(self)
        self.tabs.addTab(self.plot, 'Plot')
        self.tabs.addTab(self.compare, 'Compare parameters')
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.on_event(PlotSample, self.plot.on_sample)

    def on_enter(self) -> None:
        self.plot.refresh_topics()
        self.compare.refresh_nodes()

    def shutdown(self) -> None:
        self.plot.stop_all()
        super().shutdown()

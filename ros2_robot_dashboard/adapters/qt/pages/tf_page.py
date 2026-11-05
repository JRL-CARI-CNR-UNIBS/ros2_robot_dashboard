"""TF page: frame tree with rates and problems, transform echo between two frames."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ....core.domain.events import TfTreeChanged, TransformSampleEvent
from ....core.domain.tf import RotationFormat, TfTreeSnapshot, TransformSample, format_rotation
from .. import theme
from ..widgets.common import FilterEdit, button, copy_to_clipboard, fmt_age, fmt_hz
from .base_page import BasePage

FORMAT_LABELS = [
    (RotationFormat.QUATERNION, 'Quaternion (x y z w)'),
    (RotationFormat.RPY_RAD, 'Roll-pitch-yaw (rad)'),
    (RotationFormat.RPY_DEG, 'Roll-pitch-yaw (deg)'),
    (RotationFormat.MATRIX, 'SE(3) matrix 4x4'),
]


def format_sample(s: TransformSample, fmt: RotationFormat, precision: int = 4) -> str:
    if s.error:
        return f'error: {s.error}'
    p = precision
    x, y, z = s.translation
    lines = [f'{s.target} <- {s.source}   stamp {s.stamp:.3f}']
    if fmt == RotationFormat.MATRIX:
        lines.append(format_rotation(s, fmt, p))
    else:
        lines.append(f'translation  x {x:.{p}f}  y {y:.{p}f}  z {z:.{p}f}')
        lines.append(f'rotation     {format_rotation(s, fmt, p)}')
    return '\n'.join(lines)


class TfPage(BasePage):
    page_id = 'tf'
    title = 'TF'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.tf
        self._echo_id = ''
        self._last_sample: TransformSample | None = None
        self._snap = TfTreeSnapshot()
        self._build()
        self.on_event(TfTreeChanged, self._on_tree)
        self.on_event(TransformSampleEvent, self._on_sample)

    def _build(self) -> None:
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Frame', 'Rate', 'Last update', 'Kind'])
        self.tree.setColumnWidth(0, 300)
        self.tree.itemClicked.connect(self._frame_clicked)
        self.filter = FilterEdit('Filter frames…')
        self.filter.textChanged.connect(lambda _t: self._fill())
        self.issues = QLabel('')
        self.issues.setWordWrap(True)
        self.count = QLabel('')
        clear = button('Forget frames', self._clear,
                       'Forget the frames seen so far (after a robot restart)')
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(clear)
        ll.addLayout(row)
        ll.addWidget(self.tree, 1)
        ll.addWidget(self.issues)
        ll.addWidget(self.count)

        box = QGroupBox('Transform echo (tf2_echo)')
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setToolTip('Target (reference) frame')
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setToolTip('Source frame')
        self.fmt = QComboBox()
        for f, label in FORMAT_LABELS:
            self.fmt.addItem(label, f)
        saved = self.setting('format', RotationFormat.RPY_DEG.value)
        for i, (f, _l) in enumerate(FORMAT_LABELS):
            if f.value == saved:
                self.fmt.setCurrentIndex(i)
        self.fmt.currentIndexChanged.connect(self._fmt_changed)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.5, 20.0)
        self.rate.setValue(5.0)
        self.rate.setSuffix(' Hz')
        self.start_btn = button('Start', self._start_echo)
        self.stop_btn = button('Stop', self._stop_echo)
        self.stop_btn.setEnabled(False)
        swap = button('Swap', self._swap)
        copy = button('Copy', lambda: copy_to_clipboard(self.out.toPlainText()))
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setObjectName('terminal')
        bl = QVBoxLayout(box)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel('Target'))
        r1.addWidget(self.target, 1)
        r1.addWidget(QLabel('Source'))
        r1.addWidget(self.source, 1)
        r1.addWidget(swap)
        bl.addLayout(r1)
        r2 = QHBoxLayout()
        for w in (QLabel('Format'), self.fmt, self.rate, self.start_btn, self.stop_btn, copy):
            r2.addWidget(w)
        r2.addStretch(1)
        bl.addLayout(r2)
        bl.addWidget(self.out, 1)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(box)
        split.setSizes([650, 650])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        self._age_timer = QTimer(self)
        self._age_timer.timeout.connect(self._fill)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self.svc.acquire('tf_page')
        self._snap = self.svc.snapshot()
        self._fill()
        self._age_timer.start(1000)

    def on_leave(self) -> None:
        self._age_timer.stop()
        self.svc.release('tf_page')

    def shutdown(self) -> None:
        self._stop_echo()
        super().shutdown()

    def focus_item(self, kind: str, name: str) -> None:
        self.filter.setText(name)
        self.source.setEditText(name)

    # ----------------------------------------------------------------- tree

    def _on_tree(self, ev: TfTreeChanged) -> None:
        self._snap = ev.tree
        if self.active:
            self._fill()

    def _fill(self) -> None:
        snap = self._snap
        names = snap.names()
        for combo in (self.target, self.source):
            cur = combo.currentText()
            if [combo.itemText(i) for i in range(combo.count())] != names:
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(names)
                combo.setEditText(cur)
                combo.blockSignals(False)
        if not self.target.currentText() and snap.roots():
            self.target.setEditText(snap.roots()[0])
        expanded = {self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())}
        selected = self.tree.currentItem().text(0) if self.tree.currentItem() else ''
        self.tree.clear()
        text = self.filter.text().strip().lower()
        problem = {f for i in snap.issues for f in i.frames}
        now = time.time()
        by_name = {f.name: f for f in snap.frames}

        def matches(name: str) -> bool:
            if not text or text in name.lower():
                return True
            return any(matches(c.name) for c in snap.children(name))

        def add(parent, name: str, depth: int = 0):
            if depth > 64 or not matches(name):
                return
            f = by_name.get(name)
            it = QTreeWidgetItem(parent)
            it.setText(0, name)
            if f is not None:
                it.setText(1, 'static' if f.is_static else fmt_hz(f.rate_hz))
                it.setText(2, '' if f.is_static else fmt_age(now - f.last_update))
                it.setText(3, 'static' if f.is_static else 'dynamic')
                if len(f.parents_seen) > 1:
                    it.setToolTip(0, 'parents: ' + ', '.join(f.parents_seen))
            if name in problem:
                it.setForeground(0, QBrush(theme.color('error')))
            if name == selected:
                self.tree.setCurrentItem(it)
            for c in snap.children(name):
                add(it, c.name, depth + 1)

        roots = snap.roots()
        # frames with a parent never received as a frame are also roots of the display
        known = set(by_name)
        missing_parents = sorted({f.parent for f in snap.frames if f.parent and
                                  f.parent not in known})
        for r in sorted(set(roots) | set(missing_parents)):
            add(self.tree, r)
        self.tree.expandAll()
        del expanded
        if snap.issues:
            c = theme.color('error').name()
            self.issues.setText(f'<span style="color:{c}"><b>Problems</b></span><br>' +
                                '<br>'.join(f'{i.kind}: {i.message}' for i in snap.issues))
        else:
            self.issues.setText('No problems detected' if snap.frames else
                                'No TF received yet')
        n_static = sum(1 for f in snap.frames if f.is_static)
        self.count.setText(f'{len(snap.frames)} frames ({n_static} static), '
                           f'{len(roots)} root(s)')

    def _frame_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        self.source.setEditText(item.text(0))
        if self._echo_id:
            self.svc.update_echo(self._echo_id, self.target.currentText(),
                                 self.source.currentText())

    def _clear(self) -> None:
        self.svc.clear()
        self._snap = TfTreeSnapshot()
        self._fill()

    # ----------------------------------------------------------------- echo

    def _start_echo(self) -> None:
        target, source = self.target.currentText().strip(), self.source.currentText().strip()
        if not target or not source:
            self.message('Choose the target and source frames')
            return
        self._stop_echo()
        self._echo_id = self.svc.start_echo(target, source, self.rate.value())
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.out.setPlainText(f'waiting for {target} <- {source}…')

    def _stop_echo(self) -> None:
        if self._echo_id:
            self.svc.stop_echo(self._echo_id)
        self._echo_id = ''
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _swap(self) -> None:
        t, s = self.target.currentText(), self.source.currentText()
        self.target.setEditText(s)
        self.source.setEditText(t)
        if self._echo_id:
            self.svc.update_echo(self._echo_id, s, t)

    def _fmt_changed(self, _i: int) -> None:
        self.set_setting('format', self.fmt.currentData().value)
        if self._last_sample is not None:
            self.out.setPlainText(format_sample(self._last_sample, self.fmt.currentData()))

    def _on_sample(self, ev: TransformSampleEvent) -> None:
        if ev.echo_id != self._echo_id:
            return
        self._last_sample = ev.sample
        self.out.setPlainText(format_sample(ev.sample, self.fmt.currentData()))

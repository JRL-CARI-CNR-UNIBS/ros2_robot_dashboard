"""Robot page: joint states with limits, robot model, pose between two frames."""

from __future__ import annotations

import math
import os
from typing import Optional

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QSplitter, QStyledItemDelegate, QVBoxLayout)

from ....core.domain.events import JointStatesUpdated, RobotModelChanged
from ....core.domain.robot import JointInfo, JointState, RobotModel, limit_ratio, near_limit
from ....core.domain.tf import RotationFormat
from .. import theme
from ..widgets.common import Column, FilterEdit, RowsModel, TextFilterProxy, button, make_table
from .base_page import BasePage
from .tf_page import FORMAT_LABELS, format_sample

RATIO_ROLE = Qt.UserRole + 50


class _Row:
    __slots__ = ('state', 'info')

    def __init__(self, state: JointState, info: Optional[JointInfo]):
        self.state = state
        self.info = info


class LimitBarDelegate(QStyledItemDelegate):
    """Draws the position between the lower and upper limit."""

    def __init__(self, model: RowsModel, margin: float, parent=None):
        super().__init__(parent)
        self._model = model
        self.margin = margin

    def paint(self, painter: QPainter, option, index) -> None:
        row = index.data(Qt.UserRole + 2)          # ROW_ROLE
        ratio = None
        if row is not None and row.info is not None:
            ratio = limit_ratio(row.info, row.state.position)
        if ratio is None:
            super().paint(painter, option, index)
            return
        r: QRect = option.rect.adjusted(4, 5, -4, -5)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor('#9a9a9a'))
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawRoundedRect(r, 3, 3)
        near = ratio <= self.margin or ratio >= 1 - self.margin
        c = theme.color('error' if near else 'ok')
        x = r.left() + int(max(0.0, min(1.0, ratio)) * r.width())
        painter.setBrush(c)
        painter.setPen(c)
        painter.drawRect(QRect(x - 2, r.top(), 4, r.height()))
        painter.restore()


def _deg(v: Optional[float], info: Optional[JointInfo], show_deg: bool) -> str:
    if v is None:
        return ''
    if show_deg and (info is None or info.is_rotational):
        return f'{math.degrees(v):.2f}°'
    return f'{v:.4f}'


class RobotPage(BasePage):
    page_id = 'robot'
    title = 'Robot'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.robot
        self._model: Optional[RobotModel] = self.svc.model()
        self._pose_echo = ''
        self._last_pose = None
        self._build()
        self.on_event(JointStatesUpdated, self._on_joints)
        self.on_event(RobotModelChanged, self._on_model)
        from ....core.domain.events import TransformSampleEvent
        self.on_event(TransformSampleEvent, self._on_pose)

    def _build(self) -> None:
        margin = float(self.rt.config.get('robot.near_limit_margin', 0.05))
        self.margin = margin
        self.deg = QCheckBox('Degrees')
        self.deg.setChecked(self.setting('degrees', True))
        self.deg.toggled.connect(lambda v: (self.set_setting('degrees', v),
                                            self.model.set_rows(self.model.rows)))

        def lim(r: _Row, which: str) -> str:
            if r.info is None:
                return ''
            v = getattr(r.info.limits, which)
            return _deg(v, r.info, self.deg.isChecked()) if v is not None else ''

        def pos_fg(r: _Row):
            if r.info is not None and near_limit(r.info, r.state.position, margin):
                return theme.color('error')
            return None

        self.model = RowsModel([
            Column('Joint', lambda r: r.state.name),
            Column('Position', lambda r: _deg(r.state.position, r.info, self.deg.isChecked()),
                   fg=pos_fg, sort=lambda r: r.state.position or 0.0, align=Qt.AlignRight),
            Column('Limits', lambda r: '', sort=lambda r: (limit_ratio(r.info, r.state.position)
                                                           if r.info else -1) or -1),
            Column('Lower', lambda r: lim(r, 'lower'), align=Qt.AlignRight),
            Column('Upper', lambda r: lim(r, 'upper'), align=Qt.AlignRight),
            Column('Velocity', lambda r: '' if r.state.velocity is None
                   else f'{r.state.velocity:.4f}', sort=lambda r: r.state.velocity or 0.0,
                   align=Qt.AlignRight),
            Column('Effort', lambda r: '' if r.state.effort is None
                   else f'{r.state.effort:.3f}', sort=lambda r: r.state.effort or 0.0,
                   align=Qt.AlignRight),
            Column('Type', lambda r: r.info.type if r.info else '(not in URDF)'),
        ], key=lambda r: r.state.name)
        self.proxy = TextFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = make_table(self.model, self.proxy)
        self.table.setItemDelegateForColumn(2, LimitBarDelegate(self.model, margin, self.table))
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(2, 180)
        self.topic = QComboBox()
        self.topic.setEditable(True)
        self.topic.addItem(self.svc.joint_states_topic)
        self.topic.setToolTip('sensor_msgs/JointState topic')
        self.topic.activated.connect(lambda _i: self.svc.set_joint_states_topic(
            self.topic.currentText()))
        self.topic.lineEdit().editingFinished.connect(
            lambda: self.svc.set_joint_states_topic(self.topic.currentText()))
        self.filter = FilterEdit('Filter joints…')
        self.filter.textChanged.connect(self.proxy.set_text)
        self.joint_status = QLabel('waiting for joint states…')
        jl_box = QGroupBox('Joint states')
        jl = QVBoxLayout(jl_box)
        r = QHBoxLayout()
        for w in (QLabel('Topic'), self.topic, self.filter, self.deg):
            r.addWidget(w)
        jl.addLayout(r)
        jl.addWidget(self.table, 1)
        jl.addWidget(self.joint_status)

        # Model
        self.model_info = QPlainTextEdit()
        self.model_info.setReadOnly(True)
        self.model_info.setObjectName('terminal')
        self.model_info.setLineWrapMode(QPlainTextEdit.NoWrap)
        reload_btn = button('Reload from parameter', self._reload_description,
                            'Read robot_description from the configured node')
        file_btn = button('Load URDF file…', self._load_file)
        mb = QGroupBox('Robot model')
        ml = QVBoxLayout(mb)
        r = QHBoxLayout()
        r.addWidget(reload_btn)
        r.addWidget(file_btn)
        r.addStretch(1)
        ml.addLayout(r)
        ml.addWidget(self.model_info, 1)

        # Pose
        self.parent_frame = QComboBox()
        self.parent_frame.setEditable(True)
        self.child_frame = QComboBox()
        self.child_frame.setEditable(True)
        self.pose_fmt = QComboBox()
        for f, label in FORMAT_LABELS:
            self.pose_fmt.addItem(label, f)
        self.pose_fmt.setCurrentIndex(2)
        self.pose_fmt.currentIndexChanged.connect(self._show_pose)
        for c in (self.parent_frame, self.child_frame):
            c.currentTextChanged.connect(self._pose_frames_changed)
        self.pose_out = QPlainTextEdit()
        self.pose_out.setReadOnly(True)
        self.pose_out.setObjectName('terminal')
        self.pose_out.setMaximumHeight(130)
        pb = QGroupBox('Pose')
        pl = QVBoxLayout(pb)
        r = QHBoxLayout()
        for w in (QLabel('Parent'), self.parent_frame, QLabel('Child'), self.child_frame,
                  self.pose_fmt):
            r.addWidget(w, 1 if isinstance(w, QComboBox) else 0)
        pl.addLayout(r)
        pl.addWidget(self.pose_out)

        right = QSplitter(Qt.Vertical)
        right.addWidget(pb)
        right.addWidget(mb)
        right.setSizes([200, 500])
        split = QSplitter()
        split.addWidget(jl_box)
        split.addWidget(right)
        split.setSizes([800, 500])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        self._frames_timer = QTimer(self)
        self._frames_timer.timeout.connect(self._refresh_frames)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self.svc.acquire('robot_page')
        self.rt.tf.acquire('robot_page')
        if self._model is None:
            self.bridge.call(self.svc.load_description(), lambda _m: None, lambda _e: None)
        self._show_model()
        self._on_joints_list(self.svc.joints())
        self._refresh_frames()
        self._frames_timer.start(2000)
        self._start_pose()
        topics = [t.name for t in self.rt.graph.snapshot().topics
                  if t.type == 'sensor_msgs/msg/JointState']
        cur = self.topic.currentText()
        self.topic.blockSignals(True)
        self.topic.clear()
        self.topic.addItems(sorted(set(topics) | {cur}))
        self.topic.setCurrentText(cur)
        self.topic.blockSignals(False)

    def on_leave(self) -> None:
        self._frames_timer.stop()
        self._stop_pose()
        self.svc.release('robot_page')
        self.rt.tf.release('robot_page')

    # ----------------------------------------------------------------- joints

    def _on_joints(self, ev: JointStatesUpdated) -> None:
        if self.active:
            self._on_joints_list(ev.joints)

    def _on_joints_list(self, joints) -> None:
        m = self._model
        rows = [_Row(j, m.joint(j.name) if m else None) for j in joints]
        self.model.set_rows(rows)
        near = [r.state.name for r in rows
                if r.info and near_limit(r.info, r.state.position, self.margin)]
        txt = f'{len(rows)} joints'
        if near:
            txt += f' — near the limits: {", ".join(near)}'
        if rows or self.joint_status.text().startswith('waiting') is False:
            self.joint_status.setText(txt)

    # ----------------------------------------------------------------- model

    def _on_model(self, ev: RobotModelChanged) -> None:
        if ev.model is not None:
            self._model = ev.model
        self._show_model(ev.error)
        self._on_joints_list([r.state for r in self.model.rows])

    def _show_model(self, error: str = '') -> None:
        m = self._model
        if m is None:
            self.model_info.setPlainText(error or 'No robot description yet (waiting for '
                                         '/robot_description).')
            return
        movable = [j for j in m.joints if j.is_movable]
        lines = [f'robot: {m.name}', f'source: {m.source}',
                 f'{len(m.links)} links, {len(m.joints)} joints ({len(movable)} movable)',
                 f'root link(s): {", ".join(m.root_links())}',
                 f'tip link(s): {", ".join(m.tip_links())}', '', 'joints:']
        for j in m.joints:
            lim = j.limits
            extra = ''
            if j.is_movable and lim.lower is not None and lim.upper is not None:
                extra = f'  [{lim.lower:.4g}, {lim.upper:.4g}]'
            if lim.velocity:
                extra += f'  vmax {lim.velocity:.4g}'
            lines.append(f'  {j.name} ({j.type}) {j.parent} -> {j.child}{extra}')
        if error:
            lines.insert(0, f'warning: {error}')
        self.model_info.setPlainText('\n'.join(lines))
        links = list(m.links)
        if not self.parent_frame.currentText() and m.root_links():
            self.parent_frame.setEditText(m.root_links()[0])
        if not self.child_frame.currentText() and m.tip_links():
            self.child_frame.setEditText(m.tip_links()[0])
        del links

    def _reload_description(self) -> None:
        self.call(self.svc.load_description(), lambda m: self.message(
            f'Robot description loaded ({m.name})' if m else 'No description'),
            error_title='robot_description')

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'URDF file', os.path.expanduser('~'),
                                              'URDF (*.urdf *.xml);;All files (*)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                self.svc.set_description_xml(f.read(), f'file {path}')
        except OSError as exc:
            self.error('URDF', exc)

    # ----------------------------------------------------------------- pose

    def _refresh_frames(self) -> None:
        frames = self.rt.tf.frames()
        if self._model is not None:
            frames = sorted(set(frames) | set(self._model.links))
        for c in (self.parent_frame, self.child_frame):
            if [c.itemText(i) for i in range(c.count())] == frames:
                continue
            cur = c.currentText()
            c.blockSignals(True)
            c.clear()
            c.addItems(frames)
            c.setEditText(cur)
            c.blockSignals(False)

    def _start_pose(self) -> None:
        p, c = self.parent_frame.currentText().strip(), self.child_frame.currentText().strip()
        if p and c and not self._pose_echo:
            self._pose_echo = self.rt.tf.start_echo(p, c, 5.0)

    def _stop_pose(self) -> None:
        if self._pose_echo:
            self.rt.tf.stop_echo(self._pose_echo)
        self._pose_echo = ''

    def _pose_frames_changed(self, _t: str) -> None:
        p, c = self.parent_frame.currentText().strip(), self.child_frame.currentText().strip()
        if not self.active or not p or not c:
            return
        if self._pose_echo:
            self.rt.tf.update_echo(self._pose_echo, p, c)
        else:
            self._start_pose()

    def _on_pose(self, ev) -> None:
        if ev.echo_id != self._pose_echo:
            return
        self._last_pose = ev.sample
        self._show_pose()

    def _show_pose(self, *_a) -> None:
        if self._last_pose is not None:
            fmt: RotationFormat = self.pose_fmt.currentData()
            self.pose_out.setPlainText(format_sample(self._last_pose, fmt))

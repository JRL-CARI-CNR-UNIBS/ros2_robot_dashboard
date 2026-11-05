"""Bags page: record (topic selection, splitting, compression), inspect, play."""

from __future__ import annotations

import os
import shlex
import time
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ....core.domain.bag import BagInfo, BagPlayConfig, BagRecordConfig, RecordingStatus
from ....core.domain.events import (GraphChanged, ProcessOutput, ProcessStateChanged,
                                    RecordingStatusChanged)
from ....core.domain.process import ProcessState
from .. import theme
from ..widgets.common import (Column, FilterEdit, RowsModel, TextFilterProxy, button, fmt_bytes,
                              make_table, selected_rows)
from ..widgets.terminal import TerminalWidget
from .base_page import BasePage


def _dur(s: float) -> str:
    s = int(s)
    return f'{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}'


def bag_info_text(info: BagInfo) -> str:
    lines = [f'path:      {info.path}', f'storage:   {info.storage}',
             f'duration:  {info.duration_s:.3f} s',
             f'start:     {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.start_s))}',
             f'messages:  {info.message_count}', f'size:      {fmt_bytes(info.size_bytes)}']
    if info.compression:
        lines.append(f'compression: {info.compression}')
    if info.files:
        lines.append(f'files:     {", ".join(os.path.basename(f) for f in info.files)}')
    lines.append('')
    lines.append(f'topics ({len(info.topics)}):')
    w = max((len(t.name) for t in info.topics), default=10)
    for t in sorted(info.topics, key=lambda t: t.name):
        lines.append(f'  {t.name:<{w}}  {t.count:>9}  {t.type}')
    return '\n'.join(lines)


class RecordTab(QWidget):
    def __init__(self, page: 'BagPage'):
        super().__init__()
        self.page = page
        self.svc = page.rt.bags
        caps = page.rt.compat.capabilities()
        self.rid = ''
        self.topic_model = RowsModel([
            Column('Topic', lambda t: t.name), Column('Type', lambda t: t.type),
            Column('Pub', lambda t: len(t.publishers), align=Qt.AlignRight),
            Column('Note', lambda t: 'heavy' if page.rt.topics.is_heavy(t.name) else ''),
        ], key=lambda t: t.name)
        self.topic_proxy = TextFilterProxy(self)
        self.topic_proxy.setSourceModel(self.topic_model)
        self.topics = make_table(self.topic_model, self.topic_proxy)
        self.topics.setColumnWidth(0, 300)
        self.topics.setColumnWidth(1, 240)
        self.topics.selectionModel().selectionChanged.connect(lambda *_: self._preview())
        self.filter = FilterEdit('Filter topics…')
        self.filter.textChanged.connect(self.topic_proxy.set_text)
        self.all_topics = QCheckBox('All topics')
        self.all_topics.toggled.connect(lambda v: (self.topics.setEnabled(not v),
                                                   self._preview()))
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.all_topics)
        ll.addLayout(row)
        ll.addWidget(self.topics, 1)
        ll.addWidget(QLabel('Select the topics to record (Ctrl/Shift for several).'))

        self.out_dir = QLineEdit(page.setting('out_dir', os.path.expanduser(
            page.rt.config.get('bag.default_dir', '~/bags'))))
        browse = button('…', self._browse)
        self.name = QLineEdit('rosbag')
        self.stamp = QCheckBox('Append date/time')
        self.stamp.setChecked(True)
        self.exclude = QLineEdit()
        self.exclude.setPlaceholderText('regex, e.g. .*image.*')
        self.storage = QComboBox()
        self.storage.addItem(f'default ({caps.bag_default_storage})', '')
        for s in caps.bag_storages:
            self.storage.addItem(s, s)
        self.split_s = QSpinBox()
        self.split_s.setRange(0, 86400)
        self.split_s.setSuffix(' s')
        self.split_s.setSpecialValueText('off')
        self.split_mb = QSpinBox()
        self.split_mb.setRange(0, 1_000_000)
        self.split_mb.setSuffix(' MB')
        self.split_mb.setSpecialValueText('off')
        self.compression = QComboBox()
        self.compression.addItems(['none', 'zstd (file)', 'zstd (message)'])
        self.stop_after = QDoubleSpinBox()
        self.stop_after.setRange(0, 1e6)
        self.stop_after.setSuffix(' s')
        self.stop_after.setSpecialValueText('manual')
        for w in (self.out_dir, self.name, self.exclude):
            w.textChanged.connect(self._preview)
        for w in (self.stamp,):
            w.toggled.connect(self._preview)
        for w in (self.storage, self.compression):
            w.currentIndexChanged.connect(self._preview)
        for w in (self.split_s, self.split_mb):
            w.valueChanged.connect(self._preview)
        form = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(self.out_dir, 1)
        row.addWidget(browse)
        form.addRow('Folder', row)
        row = QHBoxLayout()
        row.addWidget(self.name, 1)
        row.addWidget(self.stamp)
        form.addRow('Name', row)
        form.addRow('Exclude', self.exclude)
        form.addRow('Storage', self.storage)
        form.addRow('Split every', self.split_s)
        form.addRow('Split at size', self.split_mb)
        form.addRow('Compression', self.compression)
        form.addRow('Stop after', self.stop_after)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setObjectName('terminal')
        self.preview.setMaximumHeight(80)
        self.rec_btn = button('● Record', self._record)
        self.stop_btn = button('■ Stop', self._stop)
        self.stop_btn.setEnabled(False)
        page.register_write_widget(self.rec_btn)
        self.indicator = QLabel('')
        self.indicator.setObjectName('recIndicator')
        self.status = QLabel('')
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setObjectName('terminal')
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(form)
        rl.addWidget(QLabel('Command'))
        rl.addWidget(self.preview)
        row = QHBoxLayout()
        row.addWidget(self.rec_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.indicator)
        row.addStretch(1)
        rl.addLayout(row)
        rl.addWidget(self.status)
        rl.addWidget(self.result, 1)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([650, 600])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

    def set_topics(self, topics) -> None:
        self.topic_model.set_rows([t for t in topics if t.publishers])

    def config(self) -> BagRecordConfig:
        comp = self.compression.currentIndex()
        topics = tuple(t.name for t in selected_rows(self.topics, self.topic_model))
        return BagRecordConfig(
            output_dir=self.out_dir.text().strip(), name=self.name.text().strip(),
            add_timestamp=self.stamp.isChecked(), topics=topics,
            all_topics=self.all_topics.isChecked(), exclude_regex=self.exclude.text().strip(),
            storage=self.storage.currentData() or '', max_duration_s=self.split_s.value(),
            max_size_bytes=self.split_mb.value() * 1024 * 1024,
            compression='zstd' if comp else '', compression_mode='message' if comp == 2
            else 'file', stop_after_s=self.stop_after.value())

    def _preview(self, *_a) -> None:
        try:
            self.preview.setPlainText(shlex.join(self.svc.record_command(self.config())))
        except Exception as exc:  # noqa: BLE001
            self.preview.setPlainText(f'error: {exc}')

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, 'Bag folder', self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _record(self) -> None:
        cfg = self.config()
        if not cfg.all_topics and not cfg.topics:
            self.page.message('Select at least one topic or "All topics"')
            return
        if not self.page.can_write():
            return
        heavy = [t for t in cfg.topics if self.page.rt.topics.is_heavy(t)]
        if (cfg.all_topics or heavy) and not self.page.confirm_action(
                'Record', 'The recording may be large:\n' + (
                    'all topics' if cfg.all_topics else '\n'.join(heavy)),
                low_risk=False):
            return
        self.page.set_setting('out_dir', cfg.output_dir)
        try:
            self.rid = self.svc.record(cfg)
        except Exception as exc:  # noqa: BLE001
            self.page.error('Record', exc)
            return
        self.page.terminal.clear()
        self.rec_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.result.clear()
        self.indicator.setText('● REC')
        self.indicator.setStyleSheet(f'color: {theme.COLORS["error"]}; font-weight: bold;')

    def _stop(self) -> None:
        if not self.rid:
            return
        self.status.setText('stopping (closing the bag)…')
        self.stop_btn.setEnabled(False)
        self.page.call(self.svc.stop_record(self.rid), self._finished, error_title='Stop recording')

    def _finished(self, st: RecordingStatus) -> None:
        self.on_status(st)

    def on_status(self, st: RecordingStatus) -> None:
        if st.process_id != self.rid:
            return
        txt = f'{st.output} — {_dur(st.elapsed_s)} — {fmt_bytes(st.size_bytes)}'
        if st.message:
            txt += f' — {st.message}'
        self.status.setText(txt)
        if not st.recording:
            self.indicator.setText('')
            self.rec_btn.setEnabled(not self.page.rt.safety.read_only)
            self.stop_btn.setEnabled(False)
            if st.info is not None:
                self.result.setPlainText(bag_info_text(st.info))
                self.page.play.path.setText(st.output)

    def on_state(self, ev: ProcessStateChanged) -> None:
        if ev.status.process_id == self.rid and not ev.status.state.is_active:
            self.indicator.setText('')
            self.rec_btn.setEnabled(not self.page.rt.safety.read_only)
            self.stop_btn.setEnabled(False)
            if ev.status.state == ProcessState.FAILED or (ev.status.exit_code not in (0, None)):
                self.status.setText(f'recording ended: {ev.status.message or ev.status.state.value}'
                                    f' (exit code {ev.status.exit_code})')


class PlayTab(QWidget):
    def __init__(self, page: 'BagPage'):
        super().__init__()
        self.page = page
        self.svc = page.rt.bags
        self.pid = ''
        self.info: Optional[BagInfo] = None
        self.path = QLineEdit()
        self.path.setPlaceholderText('bag folder')
        self.path.editingFinished.connect(self._load_info)
        browse = button('Open…', self._browse)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.01, 100)
        self.rate.setValue(1.0)
        self.rate.setSuffix(' x')
        self.loop = QCheckBox('Loop')
        self.clock = QCheckBox('Publish /clock')
        self.paused = QCheckBox('Start paused')
        self.offset = QDoubleSpinBox()
        self.offset.setRange(0, 1e6)
        self.offset.setSuffix(' s')
        if not page.rt.compat.capabilities().bag_play_delay:
            pass
        self.topic_model = RowsModel([
            Column('Topic', lambda t: t.name), Column('Messages', lambda t: t.count,
                                                      align=Qt.AlignRight),
            Column('Type', lambda t: t.type)], key=lambda t: t.name)
        self.topics = make_table(self.topic_model)
        self.topics.setColumnWidth(0, 300)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setObjectName('terminal')
        self.info_text.setMaximumHeight(140)
        self.play_btn = button('▶ Play', self._play)
        self.stop_btn = button('■ Stop', self._stop)
        self.stop_btn.setEnabled(False)
        self.pause_btn = button('Pause', lambda: self.page.call(self.svc.pause(),
                                                                error_title='Pause'))
        self.resume_btn = button('Resume', lambda: self.page.call(self.svc.resume(),
                                                                  error_title='Resume'))
        page.register_write_widget(self.play_btn)
        self.preview = QLineEdit()
        self.preview.setReadOnly(True)
        self.preview.setObjectName('terminal')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        lay.addWidget(self.info_text)
        lay.addWidget(QLabel('Topics to play (none selected = all)'))
        lay.addWidget(self.topics, 1)
        row = QHBoxLayout()
        for w in (QLabel('Rate'), self.rate, QLabel('Start at'), self.offset, self.loop,
                  self.clock, self.paused):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        for w in (self.play_btn, self.stop_btn, self.pause_btn, self.resume_btn):
            row.addWidget(w)
        row.addWidget(self.preview, 1)
        lay.addLayout(row)
        for w in (self.loop, self.clock, self.paused):
            w.toggled.connect(self._preview)
        for w in (self.rate, self.offset):
            w.valueChanged.connect(self._preview)
        self.topics.selectionModel().selectionChanged.connect(lambda *_: self._preview())

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, 'Bag folder', self.path.text() or
                                             os.path.expanduser(self.page.rt.config.get(
                                                 'bag.default_dir', '~/bags')))
        if d:
            self.path.setText(d)
            self._load_info()

    def _load_info(self) -> None:
        p = self.path.text().strip()
        if not p:
            return

        def ok(info: BagInfo):
            self.info = info
            self.info_text.setPlainText(bag_info_text(info).split('\n\ntopics')[0])
            self.topic_model.set_rows(sorted(info.topics, key=lambda t: t.name))
            self._preview()

        def err(exc):
            self.info = None
            self.info_text.setPlainText(f'cannot read the bag: {exc}')
            self.topic_model.set_rows([])

        self.page.call(self.svc.info(p), ok, err)

    def config(self) -> BagPlayConfig:
        topics = tuple(t.name for t in selected_rows(self.topics, self.topic_model))
        return BagPlayConfig(path=self.path.text().strip(), rate=self.rate.value(),
                             loop=self.loop.isChecked(), start_offset_s=self.offset.value(),
                             topics=topics, clock=self.clock.isChecked(),
                             start_paused=self.paused.isChecked(),
                             storage=self.info.storage if self.info else '')

    def _preview(self, *_a) -> None:
        if self.path.text().strip():
            try:
                self.preview.setText(shlex.join(self.svc.play_command(self.config())))
            except Exception as exc:  # noqa: BLE001
                self.preview.setText(f'error: {exc}')

    def _play(self) -> None:
        cfg = self.config()
        if not cfg.path:
            return
        if not self.page.confirm_action('Play bag', f'Play {cfg.path}?',
                                        'The bag topics will be published on the live system '
                                        f'(ROS_DOMAIN_ID {self.page.rt.domain_id()}).'):
            return
        try:
            self.pid = self.svc.play(cfg)
        except Exception as exc:  # noqa: BLE001
            self.page.error('Play', exc)
            return
        self.page.terminal.clear()
        self.play_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _stop(self) -> None:
        if self.pid:
            self.page.call(self.svc.stop_play(self.pid), error_title='Stop playback')

    def on_state(self, ev: ProcessStateChanged) -> None:
        if ev.status.process_id == self.pid and not ev.status.state.is_active:
            self.play_btn.setEnabled(not self.page.rt.safety.read_only)
            self.stop_btn.setEnabled(False)
            self.page.message(f'Playback ended ({ev.status.state.value})')


class BagPage(BasePage):
    page_id = 'bags'
    title = 'Bags'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tabs = QTabWidget()
        self.record = RecordTab(self)
        self.play = PlayTab(self)
        self.tabs.addTab(self.record, 'Record')
        self.tabs.addTab(self.play, 'Play / inspect')
        self.terminal = TerminalWidget(5000)
        self.terminal.setMaximumHeight(200)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(QLabel('ros2 bag output'))
        lay.addWidget(self.terminal)
        self.on_event(GraphChanged, self._on_graph)
        self.on_event(RecordingStatusChanged, lambda ev: self.record.on_status(ev.status))
        self.on_event(ProcessStateChanged, self._on_state)
        self.on_event(ProcessOutput, self._on_output)

    def on_enter(self) -> None:
        self.record.set_topics(self.rt.graph.snapshot().topics)
        self.record._preview()

    def _on_graph(self, ev: GraphChanged) -> None:
        if self.active:
            self.record.set_topics(ev.graph.topics)

    def _on_state(self, ev: ProcessStateChanged) -> None:
        self.record.on_state(ev)
        self.play.on_state(ev)

    def _on_output(self, ev: ProcessOutput) -> None:
        if ev.process_id in (self.record.rid, self.play.pid):
            self.terminal.append_lines([line for _s, line in ev.lines])

"""Topics page: extended ``ros2 topic info`` and an echo terminal with text recording."""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from ....core.domain.events import (EchoFileStatus, EchoStopped, GraphChanged, TopicSample,
                                    TopicStats)
from ....core.domain.graph import TopicInfo, qos_issues
from ....core.services.topic_service import EchoOptions, FileOptions, QosChoice
from .. import theme
from ..widgets.common import (Column, FilterEdit, RowsModel, TextFilterProxy, button, confirm,
                              fmt_hz, fmt_rate, make_table, selected_rows)
from ..widgets.terminal import TerminalWidget
from .base_page import BasePage


def _fmt_s(v: Optional[float]) -> str:
    return '∞' if v is None else f'{v:g} s'


class FileOptionsDialog(QDialog):
    def __init__(self, topic: str, parent=None, last_dir: str = ''):
        super().__init__(parent)
        self.setWindowTitle(f'Record {topic} to a text file')
        safe = topic.strip('/').replace('/', '_') or 'topic'
        self.path = QLineEdit(os.path.join(last_dir or os.path.expanduser('~'),
                                           f'{safe}_{time.strftime("%Y%m%d_%H%M%S")}.yaml'))
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._browse)
        self.append = QCheckBox('Append if the file exists')
        self.receive = QCheckBox('Receive time')
        self.receive.setChecked(True)
        self.header = QCheckBox('header.stamp (if present)')
        self.header.setChecked(True)
        self.full = QCheckBox('Full text (no array truncation)')
        self.full.setChecked(True)
        self.every = QCheckBox('Every message (not only the ones shown on screen)')
        self.every.setChecked(True)
        self.max_msgs = QSpinBox()
        self.max_msgs.setRange(0, 10_000_000)
        self.max_msgs.setSpecialValueText('no limit')
        self.max_s = QDoubleSpinBox()
        self.max_s.setRange(0, 1e6)
        self.max_s.setSuffix(' s')
        self.max_s.setSpecialValueText('no limit')
        form = QFormLayout(self)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow('File', row)
        form.addRow('', self.append)
        form.addRow('Timestamps', self.receive)
        form.addRow('', self.header)
        form.addRow('', self.full)
        form.addRow('', self.every)
        form.addRow('Stop after messages', self.max_msgs)
        form.addRow('Stop after time', self.max_s)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.resize(560, 300)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, 'Echo file', self.path.text(),
                                           'YAML/text (*.yaml *.txt);;All files (*)')
        if p:
            self.path.setText(p)

    def options(self) -> FileOptions:
        return FileOptions(path=self.path.text(), append=self.append.isChecked(),
                           receive_stamp=self.receive.isChecked(),
                           header_stamp=self.header.isChecked(), full_text=self.full.isChecked(),
                           max_messages=self.max_msgs.value(), max_seconds=self.max_s.value(),
                           every_message=self.every.isChecked())


class TopicsPage(BasePage):
    page_id = 'topics'
    title = 'Topics'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.topics
        self._rates: dict[str, float] = {}
        self._bw: dict[str, float] = {}
        self._measuring: set[str] = set()
        self._echo_id: Optional[str] = None
        self._echo_topic = ''
        self._selected = ''
        self._build()
        self.on_event(GraphChanged, self._on_graph)
        self.on_event(TopicStats, self._on_stats)
        self.on_event(TopicSample, self._on_sample)
        self.on_event(EchoStopped, self._on_echo_stopped)
        self.on_event(EchoFileStatus, self._on_file_status)

    # ----------------------------------------------------------------- ui

    def _build(self) -> None:
        def no_pub(t: TopicInfo):
            return theme.color('warn') if not t.publishers else None

        self.model = RowsModel([
            Column('Topic', lambda t: t.name),
            Column('Type', lambda t: t.type),
            Column('Pub', lambda t: len(t.publishers), fg=no_pub, align=Qt.AlignRight),
            Column('Sub', lambda t: len(t.subscribers),
                   fg=lambda t: theme.color('stale') if not t.subscribers else None,
                   align=Qt.AlignRight),
            Column('Rate', lambda t: fmt_hz(self._rates.get(t.name)) if t.name in self._rates
                   else '', sort=lambda t: self._rates.get(t.name, -1.0), align=Qt.AlignRight),
            Column('Bandwidth', lambda t: fmt_rate(self._bw[t.name]) if t.name in self._bw
                   else '', sort=lambda t: self._bw.get(t.name, -1.0), align=Qt.AlignRight),
            Column('Notes', lambda t: _notes(t)),
        ], key=lambda t: t.name)
        self.proxy = TextFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = make_table(self.model, self.proxy)
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        self.table.doubleClicked.connect(lambda _i: self.echo())
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 230)
        for c, w in ((2, 45), (3, 45), (4, 80), (5, 90)):
            self.table.setColumnWidth(c, w)
        self.filter = FilterEdit('Filter by name or type…')
        self.filter.textChanged.connect(self.proxy.set_text)
        self.only_problems = QCheckBox('Without publisher or subscriber')
        self.only_problems.toggled.connect(lambda v: self.proxy.set_predicate(
            (lambda t: not t.publishers or not t.subscribers) if v else None))
        self.measure_btn = button('Measure rate', self._toggle_measure,
                                  'Measure rate and bandwidth of the selected topics')
        self.count_label = QLabel('')

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in (self.filter, self.only_problems, self.measure_btn, self.count_label):
            row.addWidget(w)
        tl.addLayout(row)
        tl.addWidget(self.table, 1)

        # Detail panel
        self.detail_title = QLabel('Select a topic')
        self.detail_title.setObjectName('pageTitle')
        self.detail_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.ep_model = RowsModel([
            Column('Kind', lambda e: 'pub' if e.endpoint_type == 'publisher' else 'sub'),
            Column('Node', lambda e: e.node_fqn),
            Column('Reliability', lambda e: e.qos.reliability),
            Column('Durability', lambda e: e.qos.durability),
            Column('History', lambda e: e.qos.history + (
                f' ({e.qos.depth})' if e.qos.depth else '')),
            Column('Deadline', lambda e: _fmt_s(e.qos.deadline_s)),
            Column('Lifespan', lambda e: _fmt_s(e.qos.lifespan_s)),
            Column('Liveliness', lambda e: e.qos.liveliness),
            Column('GID', lambda e: e.gid),
        ], key=lambda e: (e.endpoint_type, e.node_fqn, e.gid))
        self.ep_table = make_table(self.ep_model)
        self.qos_label = QLabel('')
        self.qos_label.setWordWrap(True)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.detail_title)
        dl.addWidget(self.ep_table, 1)
        dl.addWidget(self.qos_label)

        # Echo terminal
        cfg = self.rt.config
        self.terminal = TerminalWidget(int(cfg.get('topics.terminal_max_lines', 5000)))
        self.echo_btn = button('Echo', self.echo, 'Continuous echo of the selected topic')
        self.once_btn = button('Echo once', lambda: self.echo(once=True))
        self.stop_btn = button('Stop', self.stop_echo)
        self.stop_btn.setEnabled(False)
        self.record_btn = button('Record to file…', self._toggle_file)
        self.record_btn.setEnabled(False)
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(0.1, 200.0)
        self.rate_spin.setValue(float(cfg.get('topics.echo_max_rate_hz', 15.0)))
        self.rate_spin.setSuffix(' msg/s')
        self.rate_spin.setToolTip('Maximum messages per second shown on screen')
        self.trunc_spin = QSpinBox()
        self.trunc_spin.setRange(0, 100000)
        self.trunc_spin.setValue(int(cfg.get('topics.echo_truncate_length', 64)))
        self.trunc_spin.setSpecialValueText('no limit')
        self.trunc_spin.setToolTip('Truncate arrays and strings longer than this')
        self.meta_only = QCheckBox('Header only')
        self.meta_only.setToolTip('Show header and sizes instead of large arrays')
        self.separator = QCheckBox('---')
        self.separator.setChecked(True)
        self.separator.setToolTip('Separator between messages')
        self.rel_combo = QComboBox()
        self.rel_combo.addItems(['auto', 'reliable', 'best_effort'])
        self.rel_combo.setToolTip('Reliability (auto = compatible with the publishers)')
        self.dur_combo = QComboBox()
        self.dur_combo.addItems(['auto', 'volatile', 'transient_local'])
        self.dur_combo.setToolTip('Durability (auto = compatible with the publishers)')
        self.echo_status = QLabel('')
        self.file_status = QLabel('')
        echo_box = QWidget()
        el = QVBoxLayout(echo_box)
        el.setContentsMargins(0, 0, 0, 0)
        r1 = QHBoxLayout()
        for w in (self.echo_btn, self.once_btn, self.stop_btn, self.record_btn, QLabel('max'),
                  self.rate_spin, QLabel('truncate'), self.trunc_spin, self.meta_only,
                  self.separator, QLabel('QoS'), self.rel_combo, self.dur_combo):
            r1.addWidget(w)
        r1.addStretch(1)
        el.addLayout(r1)
        el.addWidget(self.terminal, 1)
        r2 = QHBoxLayout()
        r2.addWidget(self.echo_status)
        r2.addStretch(1)
        r2.addWidget(self.file_status)
        el.addLayout(r2)

        upper = QSplitter(Qt.Horizontal)
        upper.addWidget(top)
        upper.addWidget(detail)
        upper.setSizes([950, 450])
        main = QSplitter(Qt.Vertical)
        main.addWidget(upper)
        main.addWidget(echo_box)
        main.setSizes([420, 400])
        lay = QVBoxLayout(self)
        lay.addWidget(main)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self._set_topics(self.rt.graph.snapshot().topics)

    def on_leave(self) -> None:
        # Stop measuring when the page is hidden; the echo keeps running if started.
        for t in list(self._measuring):
            self.svc.stop_stats(t, 'topics_page')
        self._measuring.clear()
        self.measure_btn.setText('Measure rate')

    def shutdown(self) -> None:
        self.stop_echo()
        super().shutdown()

    def focus_item(self, kind: str, name: str) -> None:
        self.filter.setText(name)
        for r in range(self.proxy.rowCount()):
            idx = self.proxy.index(r, 0)
            if idx.data() == name:
                self.table.setCurrentIndex(idx)
                break

    # ----------------------------------------------------------------- graph

    def _on_graph(self, ev: GraphChanged) -> None:
        if self.active:
            self._set_topics(ev.graph.topics)

    def _set_topics(self, topics) -> None:
        self.model.set_rows(topics)
        self.count_label.setText(f'{len(topics)} topics')
        if self._selected:
            t = next((t for t in topics if t.name == self._selected), None)
            if t is not None:
                self._show_detail(t)

    def _row_changed(self, current, _prev) -> None:
        t = self.model.row_at(current)
        if t is None:
            return
        self._selected = t.name
        self._show_detail(t)

    def _show_detail(self, t: TopicInfo) -> None:
        self.detail_title.setText(f'{t.name}  [{t.type}]')
        self.ep_model.set_rows(list(t.publishers) + list(t.subscribers))
        issues = qos_issues(t)
        if issues:
            c = theme.color('error').name()
            self.qos_label.setText(f'<span style="color:{c}"><b>QoS incompatibilities</b></span>'
                                   '<br>' + '<br>'.join(f'{i.publisher} → {i.subscriber}: '
                                                        f'{i.reason}' for i in issues))
        else:
            self.qos_label.setText('QoS: no incompatibilities detected' if t.publishers and
                                   t.subscribers else '')

    # ----------------------------------------------------------------- rate

    def _toggle_measure(self) -> None:
        if self._measuring:
            for t in list(self._measuring):
                self.svc.stop_stats(t, 'topics_page')
            self._measuring.clear()
            self.measure_btn.setText('Measure rate')
            return
        topics = [t.name for t in selected_rows(self.table, self.model)]
        if not topics and self._selected:
            topics = [self._selected]
        heavy = [t for t in topics if self.svc.is_heavy(t)]
        if heavy and not confirm(self, 'High-rate topics', 'Measuring subscribes to the topics. '
                                 'These may be heavy:\n  ' + '\n  '.join(heavy), ok_text='Measure',
                                 danger=False):
            return
        for t in topics[:20]:
            try:
                self.svc.start_stats(t, 'topics_page')
                self._measuring.add(t)
            except Exception as exc:  # noqa: BLE001
                self.message(f'{t}: {exc}')
        if self._measuring:
            self.measure_btn.setText(f'Stop measuring ({len(self._measuring)})')

    def _on_stats(self, ev: TopicStats) -> None:
        s = ev.stats
        if s.topic in self._measuring:
            self._rates[s.topic] = s.rate_hz
            self._bw[s.topic] = s.bandwidth_bps
            self.model.set_rows(self.model.rows)

    # ----------------------------------------------------------------- echo

    def echo(self, once: bool = False) -> None:
        topic = self._selected
        if not topic:
            self.message('Select a topic first')
            return
        self.stop_echo()
        if self.svc.is_heavy(topic) and not self.meta_only.isChecked() and not confirm(
                self, 'Heavy topic', f'{topic} carries large messages (images, point clouds…). '
                'Echo it anyway? Tip: enable "Header only".', ok_text='Echo', danger=False):
            return
        opts = EchoOptions(
            max_rate_hz=self.rate_spin.value(),
            truncate_length=self.trunc_spin.value() or None,
            metadata_only=self.meta_only.isChecked(), once=once,
            qos=QosChoice(self.rel_combo.currentText(), self.dur_combo.currentText()))
        try:
            self._echo_id = self.svc.start_echo(topic, opts)
        except Exception as exc:  # noqa: BLE001
            self.error('Echo', exc)
            return
        self._echo_topic = topic
        self._received = 0
        self._dropped = 0
        self.stop_btn.setEnabled(True)
        self.record_btn.setEnabled(True)
        self.echo_status.setText(f'echo {topic}' + (' (once)' if once else '') + ' — waiting…')

    def stop_echo(self) -> None:
        if self._echo_id:
            self.svc.stop_echo(self._echo_id)
        self._echo_id = None
        self.stop_btn.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.record_btn.setText('Record to file…')

    def _on_sample(self, ev: TopicSample) -> None:
        if ev.echo_id != self._echo_id:
            return
        self._received += 1
        self._dropped += ev.dropped
        self.terminal.append(ev.text.rstrip('\n'))
        if self.separator.isChecked():
            self.terminal.append('---')
        self.echo_status.setText(f'echo {ev.topic}: {self._received} shown, '
                                 f'{self._dropped} skipped by sampling')

    def _on_echo_stopped(self, ev: EchoStopped) -> None:
        if ev.echo_id == self._echo_id:
            self._echo_id = None
            self.stop_btn.setEnabled(False)
            self.record_btn.setEnabled(False)
            self.echo_status.setText(f'echo {ev.topic} stopped ({ev.reason})')

    def _toggle_file(self) -> None:
        if not self._echo_id:
            return
        if self.record_btn.text().startswith('Stop'):
            self.svc.stop_file(self._echo_id)
            self.record_btn.setText('Record to file…')
            return
        dlg = FileOptionsDialog(self._echo_topic, self, self.setting('echo_dir', ''))
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.options()
        self.set_setting('echo_dir', os.path.dirname(opts.path))
        try:
            self.svc.start_file(self._echo_id, opts)
        except Exception as exc:  # noqa: BLE001
            self.error('Record to file', exc)
            return
        self.record_btn.setText('Stop recording')

    def _on_file_status(self, ev: EchoFileStatus) -> None:
        if ev.echo_id != self._echo_id:
            return
        state = 'recording' if ev.active else 'file closed'
        txt = f'{state}: {ev.path} — {ev.written} written'
        if ev.dropped:
            txt += f', {ev.dropped} DROPPED (disk too slow)'
        if ev.message:
            txt += f' — {ev.message}'
        self.file_status.setText(txt)
        if not ev.active:
            self.record_btn.setText('Record to file…')


def _notes(t: TopicInfo) -> str:
    notes = []
    if not t.publishers:
        notes.append('no publisher')
    if not t.subscribers:
        notes.append('no subscriber')
    if len(t.types) > 1:
        notes.append('multiple types!')
    if qos_issues(t):
        notes.append('QoS mismatch')
    return ', '.join(notes)

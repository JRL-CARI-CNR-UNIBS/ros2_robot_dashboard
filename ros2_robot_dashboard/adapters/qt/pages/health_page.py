"""Health page: critical topics (rate, latency, timeouts), host metrics, discovery issues."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QLabel, QSplitter, QVBoxLayout

from ....core.domain.events import (DiscoveryIssues, HealthAlert, HealthUpdated,
                                    HostMetricsUpdated)
from ....core.domain.health import HealthLevel, TopicHealth
from .. import theme
from ..widgets.common import Column, RowsModel, button, fmt_age, fmt_bytes, fmt_hz, fmt_rate, \
    make_table
from ..widgets.terminal import TerminalWidget
from .base_page import BasePage

LEVEL_COLOR = {HealthLevel.OK: 'ok', HealthLevel.WARN: 'warn', HealthLevel.ERROR: 'error',
               HealthLevel.UNKNOWN: 'stale'}


def _pct_color(v: float):
    return theme.color('error' if v >= 90 else 'warn' if v >= 75 else 'ok')


class HealthPage(BasePage):
    page_id = 'health'
    title = 'Health'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.health
        self._build()
        self.on_event(HealthUpdated, self._on_health)
        self.on_event(HealthAlert, self._on_alert)
        self.on_event(HostMetricsUpdated, self._on_hosts)
        self.on_event(DiscoveryIssues, self._on_discovery)

    def _build(self) -> None:
        self.profile = QComboBox()
        self.profile.setMinimumWidth(240)
        self.profile.activated.connect(self._profile_changed)
        reload_btn = button('Reload profiles', self._reload)
        self.summary = QLabel('')
        self.summary.setObjectName('bigValue')

        def lvl_fg(h: TopicHealth):
            return theme.color(LEVEL_COLOR.get(h.level, 'stale'))

        self.topic_model = RowsModel([
            Column('Topic', lambda h: h.spec.topic),
            Column('Status', lambda h: h.level.name,
                   fg=lvl_fg),
            Column('Rate', lambda h: fmt_hz(h.stats.rate_hz) if h.stats else '',
                   align=Qt.AlignRight),
            Column('Expected', lambda h: f'{h.spec.expected_hz:g} Hz ±{h.spec.tolerance:.0%}',
                   align=Qt.AlignRight),
            Column('Latency', lambda h: f'{h.stats.latency_ms:.1f} ms' if h.stats and
                   h.stats.latency_ms is not None else '', align=Qt.AlignRight),
            Column('Last message', lambda h: fmt_age(h.stats.last_message_age_s) if h.stats
                   else '', align=Qt.AlignRight),
            Column('Bandwidth', lambda h: fmt_rate(h.stats.bandwidth_bps) if h.stats else '',
                   align=Qt.AlignRight),
            Column('Reason', lambda h: h.reason),
        ], key=lambda h: h.spec.topic)
        self.topic_table = make_table(self.topic_model)
        self.topic_table.setColumnWidth(0, 260)
        tb = QGroupBox('Critical topics')
        tl = QVBoxLayout(tb)
        row = QHBoxLayout()
        for w in (QLabel('Profile'), self.profile, reload_btn):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.summary)
        tl.addLayout(row)
        tl.addWidget(self.topic_table, 1)

        self.host_model = RowsModel([
            Column('Host', lambda h: h.host + (' (this PC)' if h.local else '')),
            Column('CPU', lambda h: f'{h.cpu_percent:.0f} %', fg=lambda h: _pct_color(
                h.cpu_percent), align=Qt.AlignRight, sort=lambda h: h.cpu_percent),
            Column('Memory', lambda h: f'{h.memory_percent:.0f} %  '
                   f'({fmt_bytes(h.memory_used_bytes)}'
                   f' / {fmt_bytes(h.memory_total_bytes)})', fg=lambda h: _pct_color(
                       h.memory_percent), sort=lambda h: h.memory_percent),
            Column('Load', lambda h: ' '.join(f'{v:.2f}' for v in h.load_avg)),
            Column('Net rx', lambda h: fmt_rate(h.net_rx_bps), align=Qt.AlignRight),
            Column('Net tx', lambda h: fmt_rate(h.net_tx_bps), align=Qt.AlignRight),
            Column('Temp', lambda h: f'{h.temperature_c:.0f} °C' if h.temperature_c is not None
                   else '', align=Qt.AlignRight),
            Column('Age', lambda h: fmt_age(time.time() - h.stamp) if h.stamp else ''),
        ], key=lambda h: h.host)
        self.host_table = make_table(self.host_model)
        self.host_table.setColumnWidth(0, 200)
        self.host_table.setColumnWidth(2, 240)
        hb = QGroupBox('Hosts (this PC + robots running host_monitor)')
        hl = QVBoxLayout(hb)
        hl.addWidget(self.host_table)
        hint = QLabel('Run "ros2 run ros2_robot_dashboard host_monitor" on each robot PC to see '
                      'it here.')
        hint.setStyleSheet('color: gray;')
        hl.addWidget(hint)

        self.discovery = QLabel('')
        self.discovery.setWordWrap(True)
        self.alerts = TerminalWidget(2000, show_toolbar=False)
        ab = QGroupBox('Alerts and discovery')
        al = QVBoxLayout(ab)
        al.addWidget(self.discovery)
        al.addWidget(self.alerts, 1)

        split = QSplitter(Qt.Vertical)
        split.addWidget(tb)
        split.addWidget(hb)
        split.addWidget(ab)
        split.setSizes([380, 220, 200])
        lay = QVBoxLayout(self)
        lay.addWidget(split)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self._fill_profiles()
        self.svc.watch('health_page')
        self.topic_model.set_rows(self.svc.evaluate())
        self.host_model.set_rows(self.svc.hosts())
        self._update_summary()

    def on_leave(self) -> None:
        self.svc.unwatch('health_page')

    def _fill_profiles(self) -> None:
        active = self.svc.active_profile()
        self.profile.clear()
        self.profile.addItem('(none)', '')
        for p in self.svc.profiles():
            self.profile.addItem(p.name, p.id)
        if active is not None:
            idx = self.profile.findData(active.id)
            if idx >= 0:
                self.profile.setCurrentIndex(idx)
        errs = self.svc.errors()
        if errs:
            self.alerts.append_lines([f'profile error: {e}' for e in errs])

    def _profile_changed(self, _i: int) -> None:
        pid = self.profile.currentData() or None
        self.svc.set_profile(pid)
        self.topic_model.set_rows(self.svc.evaluate())
        self._update_summary()

    def _reload(self) -> None:
        self.svc.reload()
        self._fill_profiles()

    # ----------------------------------------------------------------- events

    def _on_health(self, ev: HealthUpdated) -> None:
        self.topic_model.set_rows(sorted(ev.topics, key=lambda h: h.spec.topic))
        self._update_summary()

    def _update_summary(self) -> None:
        rows = self.topic_model.rows
        if not rows:
            self.summary.setText('')
            return
        bad = sum(1 for h in rows if h.level == HealthLevel.ERROR)
        warn = sum(1 for h in rows if h.level == HealthLevel.WARN)
        state = 'error' if bad else 'warn' if warn else 'ok'
        c = theme.color(state).name()
        text = {'ok': 'ALL OK', 'warn': f'{warn} WARNING', 'error': f'{bad} ERROR'}[state]
        self.summary.setText(f'<span style="color:{c}">● {text}</span>')

    def _on_alert(self, ev: HealthAlert) -> None:
        self.alerts.append(f'{time.strftime("%H:%M:%S", time.localtime(ev.stamp))} '
                           f'[{ev.level}] {ev.topic}: {ev.reason}')

    def _on_hosts(self, ev: HostMetricsUpdated) -> None:
        self.host_model.set_rows(sorted(ev.hosts, key=lambda h: (not h.local, h.host)))

    def _on_discovery(self, ev: DiscoveryIssues) -> None:
        if ev.unreachable_nodes:
            c = theme.COLORS['warn']
            self.discovery.setText(f'<span style="color:{c}"><b>Discovery</b></span>: '
                                   f'{len(ev.unreachable_nodes)} node(s) visible in the graph '
                                   f'but not answering: {", ".join(ev.unreachable_nodes[:10])}'
                                   + (f'<br>{ev.message}' if ev.message else ''))
        else:
            self.discovery.setText(ev.message or 'Discovery: no problems detected')

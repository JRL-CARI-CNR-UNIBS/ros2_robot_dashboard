"""Time plot based on pyqtgraph (optional dependency): series, window, pause, CSV export."""

from __future__ import annotations

import collections
import csv
import time
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QToolButton, QVBoxLayout, QWidget)

try:
    import pyqtgraph as pg
    HAS_PG = True
except Exception:  # noqa: BLE001 - ImportError or Qt binding mismatch
    pg = None
    HAS_PG = False

PALETTE = ['#3a7bd5', '#d64541', '#2e9d4f', '#d99a00', '#8e44ad', '#16a085', '#7f8c8d',
           '#c0392b']


class TimePlot(QWidget):
    def __init__(self, window_s: float = 20.0, parent: Optional[QWidget] = None,
                 max_points: int = 20000, title: str = ''):
        super().__init__(parent)
        self._series: dict[str, dict] = {}
        self._max_points = max_points
        self._t0 = time.time()
        self._paused = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(1.0, 3600.0)
        self.window_spin.setValue(window_s)
        self.window_spin.setSuffix(' s')
        self.window_spin.setToolTip('Time window')
        self.pause_box = QCheckBox('Pause')
        self.pause_box.toggled.connect(self._set_paused)
        clear = QToolButton()
        clear.setText('Clear')
        clear.clicked.connect(self.clear)
        export = QToolButton()
        export.setText('Export CSV…')
        export.clicked.connect(self._export_dialog)
        bar.addWidget(QLabel('Window'))
        bar.addWidget(self.window_spin)
        bar.addWidget(self.pause_box)
        bar.addStretch(1)
        bar.addWidget(clear)
        bar.addWidget(export)
        lay.addLayout(bar)
        if HAS_PG:
            self.plot = pg.PlotWidget(title=title)
            self.plot.showGrid(x=True, y=True, alpha=0.3)
            self.plot.addLegend()
            self.plot.setLabel('bottom', 'time', 's')
            lay.addWidget(self.plot, 1)
        else:
            self.plot = None
            lay.addWidget(QLabel('pyqtgraph is not installed: plots are disabled.\n'
                                 'Install it with "pip install pyqtgraph" or conda.'), 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(100)

    def add_series(self, key: str, label: str = '') -> None:
        if key in self._series:
            return
        color = PALETTE[len(self._series) % len(PALETTE)]
        curve = None
        if self.plot is not None:
            curve = self.plot.plot([], [], name=label or key, pen=pg.mkPen(color, width=2))
        self._series[key] = {'label': label or key, 't': collections.deque(
            maxlen=self._max_points), 'v': collections.deque(maxlen=self._max_points),
            'curve': curve, 'dirty': False}

    def remove_series(self, key: str) -> None:
        s = self._series.pop(key, None)
        if s is not None and s['curve'] is not None and self.plot is not None:
            self.plot.removeItem(s['curve'])
            legend = self.plot.plotItem.legend
            if legend is not None:
                legend.removeItem(s['curve'])

    def series_keys(self) -> list[str]:
        return list(self._series)

    def add_point(self, key: str, t: float, value: float) -> None:
        s = self._series.get(key)
        if s is None:
            self.add_series(key)
            s = self._series[key]
        s['t'].append(t)
        s['v'].append(value)
        s['dirty'] = True

    def clear(self) -> None:
        self._t0 = time.time()
        for s in self._series.values():
            s['t'].clear()
            s['v'].clear()
            s['dirty'] = True

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused

    def _redraw(self) -> None:
        if self._paused or self.plot is None or not self.isVisible():
            return
        now = time.time()
        window = self.window_spin.value()
        for s in self._series.values():
            if not s['dirty']:
                continue
            s['dirty'] = False
            ts, vs = s['t'], s['v']
            # Trim to the window (keeps memory bounded by time too).
            while ts and now - ts[0] > window * 2:
                ts.popleft()
                vs.popleft()
            s['curve'].setData([t - self._t0 for t in ts], list(vs))
        self.plot.setXRange(max(0.0, now - self._t0 - window), now - self._t0, padding=0)

    def export_csv(self, path: str) -> None:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['series', 'time', 'value'])
            for key, s in self._series.items():
                for t, v in zip(s['t'], s['v']):
                    w.writerow([s['label'], f'{t:.6f}', repr(v)])

    def _export_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, 'Export CSV', 'plot.csv', 'CSV (*.csv)')
        if path:
            self.export_csv(path)

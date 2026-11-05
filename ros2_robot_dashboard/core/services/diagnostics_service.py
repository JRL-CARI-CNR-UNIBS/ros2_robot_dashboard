"""DiagnosticsService: aggregates /diagnostics (and /diagnostics_agg when present)."""

from __future__ import annotations

import collections
import threading
import time

from ..domain.events import DiagnosticsUpdated
from ..domain.logs import DiagLevel, DiagnosticStatusInfo
from ..runtime import BaseService


class DiagnosticsService(BaseService):
    name = 'diagnostics'

    def __init__(self, rt):
        super().__init__(rt)
        self._items: dict[str, DiagnosticStatusInfo] = {}
        self._history: dict[str, collections.deque] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._subs = []
        self._timer = None

    def start(self) -> None:
        from diagnostic_msgs.msg import DiagnosticArray
        for topic in ('/diagnostics', '/diagnostics_agg'):
            self._subs.append(self.rt.create_subscription(
                DiagnosticArray, topic, self._on_msg, 50, source=f'diagnostics {topic}'))
        self._timer = self.rt.create_timer(0.5, self._tick, 'diagnostics.tick')

    def stop(self) -> None:
        for s in self._subs:
            self.rt.destroy_subscription(s)
        self.rt.destroy_timer(self._timer)

    def _on_msg(self, msg) -> None:
        now = time.time()
        for st in msg.status:
            self.update(DiagnosticStatusInfo(
                name=st.name, level=DiagLevel.from_value(st.level), message=st.message,
                hardware_id=st.hardware_id,
                values=tuple((kv.key, kv.value) for kv in st.values), last_update=now))

    def update(self, info: DiagnosticStatusInfo) -> None:
        hist_len = int(self.config.get('diagnostics.history', 50))
        with self._lock:
            prev = self._items.get(info.name)
            if prev is not None and prev.level != info.level:
                info = DiagnosticStatusInfo(info.name, info.level, info.message, info.hardware_id,
                                            info.values, info.last_update, prev.level,
                                            info.last_update)
                self._history.setdefault(info.name, collections.deque(maxlen=hist_len)).append(
                    (info.last_update, prev.level, info.level, info.message))
            elif prev is not None:
                info = DiagnosticStatusInfo(info.name, info.level, info.message, info.hardware_id,
                                            info.values, info.last_update, prev.previous_level,
                                            prev.changed_at)
            else:
                info = DiagnosticStatusInfo(info.name, info.level, info.message, info.hardware_id,
                                            info.values, info.last_update, None, info.last_update)
            self._items[info.name] = info
            self._dirty = True

    def _tick(self) -> None:
        """Mark items that stopped updating as STALE and publish changes (2 Hz max)."""
        stale_s = float(self.config.get('diagnostics.stale_s', 5.0))
        now = time.time()
        with self._lock:
            for name, it in list(self._items.items()):
                if it.level != DiagLevel.STALE and now - it.last_update > stale_s:
                    self._items[name] = DiagnosticStatusInfo(
                        it.name, DiagLevel.STALE, f'no update for {now - it.last_update:.0f} s',
                        it.hardware_id, it.values, it.last_update, it.level, now)
                    self._history.setdefault(name, collections.deque(maxlen=50)).append(
                        (now, it.level, DiagLevel.STALE, 'stale'))
                    self._dirty = True
            if not self._dirty:
                return
            self._dirty = False
            items = tuple(sorted(self._items.values(), key=lambda i: i.name))
        self.publish(DiagnosticsUpdated(items=items))

    def items(self) -> list[DiagnosticStatusInfo]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: i.name)

    def history(self, name: str) -> list:
        with self._lock:
            return list(self._history.get(name, ()))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._history.clear()
            self._dirty = True

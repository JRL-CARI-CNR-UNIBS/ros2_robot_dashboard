"""LogService: collects /rosout in a circular buffer and publishes batches."""

from __future__ import annotations

import collections
import itertools
import threading
from typing import Iterable, Optional

from ..domain.events import LogRecordsBatch
from ..domain.logs import LogLevel, LogRecord
from ..runtime import BaseService


class LogService(BaseService):
    name = 'logs'

    def __init__(self, rt):
        super().__init__(rt)
        size = int(self.config.get('logs.buffer_size', 20000))
        self._buffer: collections.deque = collections.deque(maxlen=size)
        self._pending: list[LogRecord] = []
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._sub = None
        self._timer = None
        self.counts_by_level: collections.Counter = collections.Counter()
        self.counts_by_node: collections.Counter = collections.Counter()

    def start(self) -> None:
        from rcl_interfaces.msg import Log
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

        # Volatile subscriber: compatible with both volatile and transient_local publishers.
        qos = QoSProfile(depth=1000, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        self._sub = self.rt.create_subscription(Log, '/rosout', self._on_log, qos,
                                                source='logs.rosout')
        self._timer = self.rt.create_timer(0.1, self._flush, 'logs.flush')

    def stop(self) -> None:
        self.rt.destroy_subscription(self._sub)
        self.rt.destroy_timer(self._timer)

    def _on_log(self, msg) -> None:
        name = msg.name if msg.name.startswith('/') else '/' + msg.name.replace('.', '/')
        if name.lstrip('/').startswith(self.rt.node_name):
            return
        rec = LogRecord(
            seq=next(self._seq),
            stamp=msg.stamp.sec + msg.stamp.nanosec * 1e-9,
            level=LogLevel.from_value(int.from_bytes(msg.level, 'little')
                                      if isinstance(msg.level, (bytes, bytearray))
                                      else int(msg.level)),
            node=name,
            message=msg.msg,
            file=msg.file,
            function=msg.function,
            line=int(msg.line),
        )
        self.add(rec)

    def add(self, rec: LogRecord) -> None:
        with self._lock:
            self._buffer.append(rec)
            self._pending.append(rec)
            self.counts_by_level[rec.level] += 1
            self.counts_by_node[rec.node] += 1

    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch, self._pending = self._pending, []
        self.publish(LogRecordsBatch(records=tuple(batch)))

    # ----------------------------------------------------------------- queries

    def records(self, min_level: LogLevel = LogLevel.DEBUG, nodes: Optional[Iterable[str]] = None,
                text: str = '', regex: bool = False) -> list[LogRecord]:
        import re
        node_set = set(nodes) if nodes else None
        pattern = re.compile(text, re.IGNORECASE) if (text and regex) else None
        low = text.lower()
        with self._lock:
            items = list(self._buffer)
        out = []
        for r in items:
            if r.level < min_level:
                continue
            if node_set is not None and r.node not in node_set:
                continue
            if text:
                if pattern is not None:
                    if not pattern.search(r.message):
                        continue
                elif low not in r.message.lower():
                    continue
            out.append(r)
        return out

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self.counts_by_level.clear()
            self.counts_by_node.clear()

    def export(self, path: str, records: Optional[Iterable[LogRecord]] = None) -> int:
        import datetime
        recs = list(records) if records is not None else self.records()
        with open(path, 'w', encoding='utf-8') as f:
            for r in recs:
                t = datetime.datetime.fromtimestamp(r.stamp).isoformat(timespec='milliseconds')
                loc = f' ({r.location})' if r.location else ''
                f.write(f'{t} [{r.level.name}] {r.node}: {r.message}{loc}\n')
        return len(recs)

"""LogService: /rosout buffer, logger levels, save and load (text, JSONL, rosbag2)."""

from __future__ import annotations

import collections
import datetime
import itertools
import json
import os
import re
import threading
from concurrent.futures import Future
from typing import Iterable, Optional

from ..domain.events import LogRecordsBatch
from ..domain.logs import LogLevel, LogRecord, logger_matches, owner_node
from ..futures import try_set_exception, try_set_result
from ..runtime import BaseService

# "2026-11-05T11:55:54.123 [WARN] logger.name: message (file.cpp:42)"
_TEXT_RE = re.compile(r'^(?P<t>\d{4}-\d\d-\d\dT[\d:.]+) \[(?P<lvl>[A-Z]+)\] (?P<name>\S+?): '
                      r'(?P<msg>.*?)(?: \((?P<file>[^()\s]+):(?P<line>\d+)\))?$')


def level_from_msg(value) -> LogLevel:
    if isinstance(value, (bytes, bytearray)):
        value = int.from_bytes(value, 'little')
    return LogLevel.from_value(int(value))


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
        self.counts_by_logger: collections.Counter = collections.Counter()

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
        if msg.name.lstrip('/').startswith(self.rt.node_name):
            return
        self.add(self._make(msg.stamp.sec + msg.stamp.nanosec * 1e-9, level_from_msg(msg.level),
                            msg.name, msg.msg, msg.file, msg.function, int(msg.line)))

    def add(self, rec: LogRecord) -> None:
        with self._lock:
            self._buffer.append(rec)
            self._pending.append(rec)
            self.counts_by_level[rec.level] += 1
            self.counts_by_node[rec.node] += 1
            self.counts_by_logger[rec.logger_name] += 1

    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch, self._pending = self._pending, []
        self.publish(LogRecordsBatch(records=tuple(batch)))

    # ----------------------------------------------------------------- queries

    def records(self, min_level: LogLevel = LogLevel.DEBUG, nodes: Optional[Iterable[str]] = None,
                text: str = '', regex: bool = False,
                loggers: Optional[Iterable[str]] = None) -> list[LogRecord]:
        node_set = set(nodes) if nodes else None
        logger_set = set(loggers) if loggers else None
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
            if logger_set is not None and not logger_matches(r.logger_name, logger_set):
                continue
            if text:
                if pattern is not None:
                    if not pattern.search(r.message):
                        continue
                elif low not in r.message.lower():
                    continue
            out.append(r)
        return out

    def loggers(self) -> dict[str, int]:
        """Logger names seen so far with their message count."""
        with self._lock:
            return dict(self.counts_by_logger)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self.counts_by_level.clear()
            self.counts_by_node.clear()
            self.counts_by_logger.clear()

    # ----------------------------------------------------------------- save / load

    def export(self, path: str, records: Optional[Iterable[LogRecord]] = None) -> int:
        """Write records as text (``.log``/``.txt``) or JSON lines (``.jsonl``)."""
        recs = list(records) if records is not None else self.records()
        with open(path, 'w', encoding='utf-8') as f:
            if path.endswith('.jsonl'):
                for r in recs:
                    f.write(json.dumps({'stamp': r.stamp, 'level': r.level.name,
                                        'logger': r.logger_name, 'message': r.message,
                                        'file': r.file, 'function': r.function,
                                        'line': r.line}) + '\n')
            else:
                for r in recs:
                    t = datetime.datetime.fromtimestamp(r.stamp).isoformat(
                        timespec='milliseconds')
                    loc = f' ({r.location})' if r.location else ''
                    msg = r.message.replace('\n', '\\n')
                    f.write(f'{t} [{r.level.name}] {r.logger_name}: {msg}{loc}\n')
        return len(recs)

    def load(self, path: str) -> Future:
        """Add records from a text/JSONL file or a rosbag2 (``Log`` topics); Future of count."""
        def run() -> int:
            if os.path.isdir(path) or path.endswith(('.mcap', '.db3')):
                recs = self._read_bag(path)
            else:
                recs = self._read_file(path)
            recs.sort(key=lambda r: r.stamp)
            for r in recs:
                self.add(r)
            return len(recs)
        return self.rt.submit(run)

    def _make(self, stamp: float, level: LogLevel, logger: str, message: str,
              file: str = '', function: str = '', line: int = 0) -> LogRecord:
        logger = logger.lstrip('/')
        return LogRecord(seq=next(self._seq), stamp=stamp, level=level,
                         node='/' + logger.replace('.', '/'), message=message, file=file,
                         function=function, line=line, logger=logger)

    def _read_file(self, path: str) -> list[LogRecord]:
        out: list[LogRecord] = []
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line.strip():
                    continue
                if path.endswith('.jsonl'):
                    d = json.loads(line)
                    out.append(self._make(float(d.get('stamp', 0.0)),
                                          LogLevel[d.get('level', 'INFO')],
                                          d.get('logger') or d.get('node', ''),
                                          d.get('message', ''), d.get('file', ''),
                                          d.get('function', ''), int(d.get('line', 0))))
                    continue
                m = _TEXT_RE.match(line)
                if m is None:
                    if out:       # continuation of a multi-line message
                        last = out[-1]
                        out[-1] = self._make(last.stamp, last.level, last.logger,
                                             last.message + '\n' + line, last.file,
                                             last.function, last.line)
                    continue
                stamp = datetime.datetime.fromisoformat(m['t']).timestamp()
                level = LogLevel[m['lvl']] if m['lvl'] in LogLevel.__members__ \
                    else LogLevel.INFO
                out.append(self._make(stamp, level, m['name'], m['msg'].replace('\\n', '\n'),
                                      m['file'] or '', '', int(m['line'] or 0)))
        return out

    def _read_bag(self, path: str) -> list[LogRecord]:
        import rosbag2_py
        from rcl_interfaces.msg import Log
        from rclpy.serialization import deserialize_message

        storage_id = ''
        meta = os.path.join(path, 'metadata.yaml') if os.path.isdir(path) else ''
        if meta and os.path.isfile(meta):
            import yaml
            with open(meta, encoding='utf-8') as f:
                info = (yaml.safe_load(f) or {}).get('rosbag2_bagfile_information', {})
            storage_id = info.get('storage_identifier', '')
        elif path.endswith('.mcap'):
            storage_id = 'mcap'
        elif path.endswith('.db3'):
            storage_id = 'sqlite3'
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=path, storage_id=storage_id),
                    rosbag2_py.ConverterOptions('', ''))
        topics = {t.name for t in reader.get_all_topics_and_types()
                  if t.type == 'rcl_interfaces/msg/Log'}
        if not topics:
            raise ValueError(f'{path}: no rcl_interfaces/msg/Log topic in this bag')
        try:
            reader.set_filter(rosbag2_py.StorageFilter(topics=sorted(topics)))
        except Exception:  # noqa: BLE001 - topics are filtered below anyway
            pass
        out = []
        # read_next_ext (Lyrical and newer) returns (topic, data, recv_time, send_time).
        read = getattr(reader, 'read_next_ext', None) or reader.read_next
        while reader.has_next():
            item = read()
            if item[0] not in topics:
                continue
            msg = deserialize_message(item[1], Log)
            out.append(self._make(msg.stamp.sec + msg.stamp.nanosec * 1e-9,
                                  level_from_msg(msg.level), msg.name, msg.msg, msg.file,
                                  msg.function, int(msg.line)))
        return out

    # ----------------------------------------------------------------- logger levels

    @staticmethod
    def logger_levels_supported() -> bool:
        """``get/set_logger_levels`` services exist from ROS 2 Iron."""
        try:
            from rcl_interfaces.srv import GetLoggerLevels, SetLoggerLevels  # noqa: F401
            return True
        except ImportError:
            return False

    def owner_node(self, logger: str) -> str:
        return owner_node(logger, [n.fqn for n in self.rt.graph.snapshot().nodes])

    def level_service_problem(self, node: str) -> str:
        """'' if ``node`` offers ``set_logger_levels``, otherwise the reason why not."""
        if not self.logger_levels_supported():
            return 'Changing logger levels needs ROS 2 Iron or newer'
        if not node:
            return 'No running node owns this logger'
        names = {s.name for s in self.rt.graph.snapshot().services}
        if f'{node}/set_logger_levels' not in names:
            return (f'{node} has no logger service: the node must be created with '
                    'enable_logger_service (NodeOptions in C++, Node(...) in Python)')
        return ''

    def get_levels(self, node: str, loggers: Iterable[str]) -> Future:
        """Future of ``{logger: LogLevel or None}`` (None: not set, inherited)."""
        from rcl_interfaces.srv import GetLoggerLevels
        req = GetLoggerLevels.Request()
        req.names = list(loggers)
        out: Future = Future()

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                try_set_exception(out, exc)
                return
            try_set_result(out, {i.name: (LogLevel.from_value(i.level) if i.level else None)
                                 for i in f.result().levels})

        self.rt.call_service(GetLoggerLevels, f'{node}/get_logger_levels', req) \
            .add_done_callback(done)
        return out

    def set_levels(self, node: str, levels: dict) -> Future:
        """Set ``{logger: LogLevel}`` on ``node``; Future of ``{logger: error or ''}``."""
        from rcl_interfaces.msg import LoggerLevel
        from rcl_interfaces.srv import SetLoggerLevels
        out: Future = Future()
        action = 'set_logger_levels'
        detail = ', '.join(f'{k}={LogLevel(v).name}' for k, v in levels.items())
        try:
            self.rt.safety.check_write(action, node)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        req = SetLoggerLevels.Request()
        for name, lvl in levels.items():
            req.levels.append(LoggerLevel(name=name, level=int(lvl)))

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                self.rt.safety.log(action, node, False, f'{detail} -> {exc}')
                try_set_exception(out, exc)
                return
            res = {name: ('' if r.successful else (r.reason or 'rejected'))
                   for name, r in zip(levels, f.result().results)}
            self.rt.safety.log(action, node, not any(res.values()), detail)
            try_set_result(out, res)

        self.rt.call_service(SetLoggerLevels, f'{node}/set_logger_levels', req) \
            .add_done_callback(done)
        return out

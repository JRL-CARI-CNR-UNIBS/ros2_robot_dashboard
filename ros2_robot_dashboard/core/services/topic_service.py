"""TopicService: shared subscriptions, echo (continuous/once), file recording, stats.

Subscriptions are created with ``raw=True``: the serialized size gives the bandwidth
for free and messages are deserialized only when needed (sampled echo, latency,
file writer), which keeps high-rate topics cheap (design v2, section 7).
"""

from __future__ import annotations

import collections
import itertools
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..domain.events import EchoFileStatus, EchoStopped, TopicSample, TopicStats
from ..domain.health import TopicStatsInfo
from ..msgs.introspection import TypeUnavailable, get_message_class, header_stamp
from ..msgs.yaml_format import format_message, metadata_only
from ..runtime import BaseService


@dataclass(frozen=True)
class QosChoice:
    reliability: str = 'auto'     # auto | reliable | best_effort
    durability: str = 'auto'      # auto | volatile | transient_local
    depth: int = 10

    def key(self) -> tuple:
        return (self.reliability, self.durability, self.depth)


class RawSample:
    """A received serialized message with lazy, cached deserialization."""

    __slots__ = ('data', 'receive_time', '_cls', '_msg', '_lock')

    def __init__(self, data: bytes, receive_time: float, cls):
        self.data = data
        self.receive_time = receive_time
        self._cls = cls
        self._msg = None
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def msg(self):
        if self._msg is None:
            with self._lock:
                if self._msg is None:
                    from rclpy.serialization import deserialize_message
                    self._msg = deserialize_message(self.data, self._cls)
        return self._msg


class SharedSubscription:
    def __init__(self, topic: str, type_name: str, cls, qos_key: tuple):
        self.topic = topic
        self.type_name = type_name
        self.cls = cls
        self.qos_key = qos_key
        self.sub = None
        self.consumers: dict[str, Callable[[RawSample], None]] = {}
        self.lock = threading.Lock()
        # stats
        self.window: collections.deque = collections.deque()    # (t, size)
        self.latencies: collections.deque = collections.deque()  # (t, ms)
        self.count = 0
        self.last_time: Optional[float] = None
        self.last_latency_sample = 0.0


@dataclass
class EchoOptions:
    max_rate_hz: float = 15.0
    truncate_length: Optional[int] = 64
    no_arr: bool = False
    metadata_only: bool = False
    once: bool = False
    qos: QosChoice = field(default_factory=QosChoice)


@dataclass
class FileOptions:
    path: str
    append: bool = False
    receive_stamp: bool = True
    header_stamp: bool = True
    full_text: bool = True            # no truncation in the file
    max_messages: int = 0             # 0 = unlimited
    max_seconds: float = 0.0          # 0 = unlimited
    every_message: bool = True        # False = only the sampled messages shown on screen


class _FileWriter(threading.Thread):
    """Writes echo output in a worker thread with a bounded queue."""

    def __init__(self, service: 'TopicService', echo_id: str, topic: str,
                 opts: FileOptions, max_queue: int):
        super().__init__(name=f'echo-file-{echo_id}', daemon=True)
        self.service = service
        self.echo_id = echo_id
        self.topic = topic
        self.opts = opts
        self.q: queue.Queue = queue.Queue(maxsize=max_queue)
        self.written = 0
        self.dropped = 0
        self.started_at = time.time()
        self.active = True
        self.error = ''
        self._last_status = 0.0

    def offer(self, sample: RawSample) -> None:
        if not self.active:
            return
        try:
            self.q.put_nowait(sample)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self.active = False
        self.q.put(None)

    def run(self) -> None:
        compat = self.service.rt.compat
        path = os.path.expanduser(self.opts.path)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            f = open(path, 'a' if self.opts.append else 'w', encoding='utf-8')
        except OSError as exc:
            self.error = str(exc)
            self.active = False
            self._status(force=True)
            return
        with f:
            f.write(f'# topic: {self.topic}\n# started: '
                    f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")}\n')
            while True:
                item = self.q.get()
                if item is None:
                    break
                try:
                    msg = item.msg
                    trunc = None if self.opts.full_text else 64
                    if self.opts.receive_stamp:
                        f.write(f'# receive_time: {item.receive_time:.9f}\n')
                    if self.opts.header_stamp:
                        hs = header_stamp(msg)
                        if hs is not None:
                            f.write(f'# header_stamp: {hs:.9f}\n')
                    f.write(format_message(msg, compat, truncate_length=trunc))
                    f.write('---\n')
                    self.written += 1
                except Exception as exc:  # noqa: BLE001
                    f.write(f'# error: {exc}\n---\n')
                if self.opts.max_messages and self.written >= self.opts.max_messages:
                    self.active = False
                    self.service._file_limit_reached(self.echo_id, 'message limit reached')
                    break
                if self.opts.max_seconds and time.time() - self.started_at >= self.opts.max_seconds:
                    self.active = False
                    self.service._file_limit_reached(self.echo_id, 'time limit reached')
                    break
                self._status()
            f.write(f'# stopped: {time.strftime("%Y-%m-%dT%H:%M:%S%z")}, written {self.written}, '
                    f'dropped {self.dropped}\n')
        self.active = False
        self._status(force=True)

    def _status(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_status > 0.5:
            self._last_status = now
            self.service.publish(EchoFileStatus(
                echo_id=self.echo_id, path=self.opts.path, written=self.written,
                dropped=self.dropped, active=self.active, message=self.error))


class _Echo:
    def __init__(self, echo_id: str, topic: str, opts: EchoOptions):
        self.id = echo_id
        self.topic = topic
        self.opts = opts
        self.last_emit = 0.0
        self.dropped = 0
        self.lock = threading.Lock()
        self.writer: Optional[_FileWriter] = None
        self.done = False
        self.paused = False


class TopicService(BaseService):
    name = 'topics'

    def __init__(self, rt):
        super().__init__(rt)
        self._subs: dict[tuple, SharedSubscription] = {}
        self._lock = threading.RLock()
        self._echoes: dict[str, _Echo] = {}
        self._stats_consumers: dict[str, set] = {}          # topic -> consumer ids
        self._ids = itertools.count(1)
        self._stats_timer = None

    def start(self) -> None:
        self._stats_timer = self.rt.create_timer(1.0, self._publish_stats, 'topics.stats')

    def stop(self) -> None:
        self.rt.destroy_timer(self._stats_timer)
        for eid in list(self._echoes):
            self.stop_echo(eid, 'shutdown')
        with self._lock:
            for s in self._subs.values():
                self.rt.destroy_subscription(s.sub)
            self._subs.clear()

    # ----------------------------------------------------------------- helpers

    def topic_type(self, topic: str) -> str:
        snap = self.rt.graph.snapshot() if self.rt.has_service('graph') else None
        t = snap.topic(topic) if snap is not None else None
        if t is not None and t.types:
            return t.types[0]
        for name, types in self.rt.node.get_topic_names_and_types():
            if name == topic and types:
                return types[0]
        raise LookupError(f"topic '{topic}' not found (no publisher or subscriber)")

    def is_heavy(self, topic: str, type_name: Optional[str] = None) -> bool:
        try:
            t = type_name or self.topic_type(topic)
        except LookupError:
            return False
        return t in set(self.config.get('topics.heavy_types', []))

    def auto_qos(self, topic: str, choice: Optional[QosChoice] = None):
        """QoS compatible with the known publishers; manual override per policy."""
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

        choice = choice or QosChoice()
        pubs = []
        try:
            pubs = self.rt.compat.publishers_info(self.rt.node, topic)
        except Exception:  # noqa: BLE001
            pass
        if choice.reliability == 'auto':
            reliable = bool(pubs) and all(p.qos.reliability == 'reliable' for p in pubs)
            if not pubs:
                reliable = True
        else:
            reliable = choice.reliability == 'reliable'
        if choice.durability == 'auto':
            transient = bool(pubs) and all(p.qos.durability == 'transient_local' for p in pubs)
        else:
            transient = choice.durability == 'transient_local'
        depth = max([choice.depth] + [p.qos.depth for p in pubs if transient]) or 10
        return QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=depth,
            reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
            durability=(DurabilityPolicy.TRANSIENT_LOCAL if transient
                        else DurabilityPolicy.VOLATILE))

    # ----------------------------------------------------------------- shared subscriptions

    def acquire(self, topic: str, consumer_id: str, callback: Callable[[RawSample], None],
                qos: Optional[QosChoice] = None,
                type_name: Optional[str] = None) -> SharedSubscription:
        """Register a consumer; one ROS subscription per (topic, QoS) is shared."""
        type_name = type_name or self.topic_type(topic)
        cls = get_message_class(type_name)
        qos = qos or QosChoice()
        key = (topic, type_name, qos.key())
        with self._lock:
            shared = self._subs.get(key)
            if shared is None:
                shared = SharedSubscription(topic, type_name, cls, qos.key())
                profile = self.auto_qos(topic, qos)
                shared.sub = self.rt.create_subscription(
                    cls, topic, lambda data, s=shared: self._on_raw(s, data), profile,
                    source=f'topic {topic}', raw=True)
                self._subs[key] = shared
            with shared.lock:
                shared.consumers[consumer_id] = callback
        return shared

    def release(self, consumer_id: str) -> None:
        with self._lock:
            for key, shared in list(self._subs.items()):
                with shared.lock:
                    shared.consumers.pop(consumer_id, None)
                    empty = not shared.consumers
                if empty:
                    self.rt.destroy_subscription(shared.sub)
                    del self._subs[key]

    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def _on_raw(self, shared: SharedSubscription, data: bytes) -> None:
        now = time.time()
        sample = RawSample(bytes(data), now, shared.cls)
        window_s = float(self.config.get('topics.stats_window_s', 2.0))
        with shared.lock:
            shared.count += 1
            shared.last_time = now
            shared.window.append((now, sample.size))
            while shared.window and now - shared.window[0][0] > window_s:
                shared.window.popleft()
            consumers = list(shared.consumers.values())
            want_latency = (shared.topic in self._stats_consumers
                            and now - shared.last_latency_sample > 0.1)
            if want_latency:
                shared.last_latency_sample = now
        if want_latency:
            try:
                hs = header_stamp(sample.msg)
                if hs is not None:
                    with shared.lock:
                        shared.latencies.append((now, (now - hs) * 1000.0))
                        while shared.latencies and now - shared.latencies[0][0] > window_s:
                            shared.latencies.popleft()
            except Exception:  # noqa: BLE001
                pass
        for cb in consumers:
            cb(sample)

    # ----------------------------------------------------------------- statistics

    def start_stats(self, topic: str, consumer_id: str, type_name: Optional[str] = None,
                    qos: Optional[QosChoice] = None) -> None:
        self.acquire(topic, f'stats:{consumer_id}', lambda s: None, qos=qos, type_name=type_name)
        with self._lock:
            self._stats_consumers.setdefault(topic, set()).add(consumer_id)

    def stop_stats(self, topic: str, consumer_id: str) -> None:
        self.release(f'stats:{consumer_id}')
        with self._lock:
            ids = self._stats_consumers.get(topic)
            if ids is not None:
                ids.discard(consumer_id)
                if not ids:
                    del self._stats_consumers[topic]

    def stats(self, topic: str) -> Optional[TopicStatsInfo]:
        with self._lock:
            shared = [s for (t, _, _), s in self._subs.items() if t == topic]
        if not shared:
            return None
        s = shared[0]
        now = time.time()
        with s.lock:
            window = list(s.window)
            lat = [v for _, v in s.latencies]
            count = s.count
            last = s.last_time
        window_s = float(self.config.get('topics.stats_window_s', 2.0))
        window = [w for w in window if now - w[0] <= window_s]
        if len(window) >= 2:
            span = max(window[-1][0] - window[0][0], 1e-6)
            rate = (len(window) - 1) / span
            # Use the full window for bandwidth so a burst does not look like a high rate.
            bw = sum(sz for _, sz in window) / max(now - window[0][0], span)
            mean = sum(sz for _, sz in window) / len(window)
        else:
            rate, bw = 0.0, 0.0
            mean = float(window[0][1]) if window else 0.0
        return TopicStatsInfo(topic=topic, rate_hz=rate, bandwidth_bps=bw,
                              latency_ms=(sum(lat) / len(lat)) if lat else None,
                              message_count=count,
                              last_message_age_s=(now - last) if last else None,
                              mean_size_bytes=mean)

    def _publish_stats(self) -> None:
        with self._lock:
            topics = list(self._stats_consumers)
        for t in topics:
            st = self.stats(t)
            if st is not None:
                self.publish(TopicStats(stats=st))

    # ----------------------------------------------------------------- echo

    def start_echo(self, topic: str, opts: Optional[EchoOptions] = None,
                   file: Optional[FileOptions] = None, type_name: Optional[str] = None) -> str:
        """Start an echo session; returns its id. Samples arrive as ``TopicSample``."""
        opts = opts or EchoOptions(
            max_rate_hz=float(self.config.get('topics.echo_max_rate_hz', 15.0)),
            truncate_length=int(self.config.get('topics.echo_truncate_length', 64)))
        echo_id = f'echo{next(self._ids)}'
        echo = _Echo(echo_id, topic, opts)
        if file is not None:
            echo.writer = _FileWriter(self, echo_id, topic, file,
                                      int(self.config.get('topics.file_queue_max', 20000)))
            echo.writer.start()
        with self._lock:
            self._echoes[echo_id] = echo
        try:
            self.acquire(topic, echo_id, lambda s, e=echo: self._on_echo_sample(e, s),
                         qos=opts.qos, type_name=type_name)
        except (LookupError, TypeUnavailable) as exc:
            with self._lock:
                self._echoes.pop(echo_id, None)
            if echo.writer:
                echo.writer.close()
            raise exc
        return echo_id

    def set_paused(self, echo_id: str, paused: bool) -> None:
        echo = self._echoes.get(echo_id)
        if echo is not None:
            echo.paused = paused

    def start_file(self, echo_id: str, file: FileOptions) -> None:
        echo = self._echoes.get(echo_id)
        if echo is None:
            raise KeyError(echo_id)
        self.stop_file(echo_id)
        echo.writer = _FileWriter(self, echo_id, echo.topic, file,
                                  int(self.config.get('topics.file_queue_max', 20000)))
        echo.writer.start()

    def stop_file(self, echo_id: str) -> None:
        echo = self._echoes.get(echo_id)
        if echo is not None and echo.writer is not None:
            echo.writer.close()
            echo.writer = None

    def _file_limit_reached(self, echo_id: str, reason: str) -> None:
        echo = self._echoes.get(echo_id)
        if echo is not None and echo.writer is not None:
            echo.writer = None

    def stop_echo(self, echo_id: str, reason: str = 'stopped') -> None:
        with self._lock:
            echo = self._echoes.pop(echo_id, None)
        if echo is None:
            return
        echo.done = True
        self.release(echo_id)
        if echo.writer is not None:
            echo.writer.close()
        self.publish(EchoStopped(echo_id=echo_id, topic=echo.topic, reason=reason))

    def active_echoes(self) -> list[str]:
        with self._lock:
            return list(self._echoes)

    def _on_echo_sample(self, echo: _Echo, sample: RawSample) -> None:
        if echo.done:
            return
        writer = echo.writer
        emit = False
        with echo.lock:
            now = sample.receive_time
            min_dt = 1.0 / echo.opts.max_rate_hz if echo.opts.max_rate_hz > 0 else 0.0
            if echo.opts.once and echo.last_emit > 0:
                return                      # "once" already delivered its message
            if echo.opts.once or (not echo.paused and now - echo.last_emit >= min_dt):
                emit = True
                echo.last_emit = now
                dropped, echo.dropped = echo.dropped, 0
            else:
                echo.dropped += 1
        if writer is not None and (writer.opts.every_message or emit):
            writer.offer(sample)
        if not emit:
            return
        msg = sample.msg
        if echo.opts.metadata_only:
            text = metadata_only(msg, self.rt.compat)
        else:
            text = format_message(msg, self.rt.compat, truncate_length=echo.opts.truncate_length,
                                  no_arr=echo.opts.no_arr)
        self.publish(TopicSample(echo_id=echo.id, topic=echo.topic, text=text,
                                 receive_time=sample.receive_time,
                                 header_stamp=header_stamp(msg), dropped=dropped))
        if echo.opts.once:
            self.rt.submit(self.stop_echo, echo.id, 'once')

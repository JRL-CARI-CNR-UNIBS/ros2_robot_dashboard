"""TfService: TF tree with rates and issues, TF echo between two frames.

``/tf`` and ``/tf_static`` are subscribed only while some consumer needs them
(TF page, robot page, TF echo).
"""

from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass

from ..domain.events import TfTreeChanged, TransformSampleEvent
from ..domain.tf import FrameRecord, TfTreeSnapshot, TransformSample, analyze_tree
from ..runtime import BaseService


@dataclass
class _TfEcho:
    id: str
    target: str
    source: str
    period: float
    last: float = 0.0


class TfService(BaseService):
    name = 'tf'

    def __init__(self, rt):
        super().__init__(rt)
        self._lock = threading.RLock()
        self._consumers: set[str] = set()
        self._records: dict[str, FrameRecord] = {}
        self._invalid_frames: set[str] = set()
        self._buffer = None
        self._sub_tf = None
        self._sub_static = None
        self._tree_timer = None
        self._echo_timer = None
        self._echoes: dict[str, _TfEcho] = {}
        self._ids = itertools.count(1)
        self._snapshot = TfTreeSnapshot()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        with self._lock:
            self._consumers.clear()
            self._echoes.clear()
            self._teardown()

    # ----------------------------------------------------------------- consumers

    def acquire(self, consumer_id: str) -> None:
        with self._lock:
            self._consumers.add(consumer_id)
            if self._sub_tf is None:
                self._setup()

    def release(self, consumer_id: str) -> None:
        with self._lock:
            self._consumers.discard(consumer_id)
            if not self._consumers and not self._echoes:
                self._teardown()

    def _setup(self) -> None:
        import tf2_ros
        from rclpy.duration import Duration
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from tf2_msgs.msg import TFMessage

        self._buffer = tf2_ros.Buffer(cache_time=Duration(seconds=10.0))
        self._records = {}
        self._invalid_frames: set[str] = set()
        qos = QoSProfile(depth=100, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        qos_static = QoSProfile(depth=100, history=HistoryPolicy.KEEP_LAST,
                                reliability=ReliabilityPolicy.RELIABLE,
                                durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._sub_tf = self.rt.create_subscription(
            TFMessage, '/tf', lambda m: self._on_tf(m, False), qos, source='tf')
        self._sub_static = self.rt.create_subscription(
            TFMessage, '/tf_static', lambda m: self._on_tf(m, True), qos_static,
            source='tf_static')
        period = float(self.config.get('polling.tf_tree_s', 1.0))
        self._tree_timer = self.rt.create_timer(period, self._publish_tree, 'tf.tree')
        self._echo_timer = self.rt.create_timer(0.05, self._tick_echoes, 'tf.echo')

    def _teardown(self) -> None:
        self.rt.destroy_subscription(self._sub_tf)
        self.rt.destroy_subscription(self._sub_static)
        self.rt.destroy_timer(self._tree_timer)
        self.rt.destroy_timer(self._echo_timer)
        self._sub_tf = self._sub_static = self._tree_timer = self._echo_timer = None
        self._buffer = None

    # ----------------------------------------------------------------- data

    def _on_tf(self, msg, is_static: bool) -> None:
        now = time.time()
        buf = self._buffer
        window_s = 2.0
        for t in msg.transforms:
            child = t.child_frame_id.lstrip('/')
            parent = t.header.frame_id.lstrip('/')
            tr, rq = t.transform.translation, t.transform.rotation
            if not all(math.isfinite(v) for v in (tr.x, tr.y, tr.z, rq.x, rq.y, rq.z, rq.w)):
                # Some drivers publish NaN for unknown frames; tf2 would log an error each time.
                self._invalid_frames.add(child)
                continue
            if buf is not None:
                try:
                    if is_static:
                        buf.set_transform_static(t, 'robot_dashboard')
                    else:
                        buf.set_transform(t, 'robot_dashboard')
                except Exception:  # noqa: BLE001 - invalid transform (NaN, empty frame)
                    pass
            with self._lock:
                rec = self._records.get(child)
                if rec is None:
                    rec = FrameRecord(child, parent, is_static)
                    self._records[child] = rec
                rec.parents_seen.add(parent)
                rec.parent = parent
                rec.is_static = is_static
                rec.last_update = now
                if not is_static:
                    rec.stamps.append(now)
                    cut = 0
                    while cut < len(rec.stamps) and now - rec.stamps[cut] > window_s:
                        cut += 1
                    if cut:
                        del rec.stamps[:cut]
                    if len(rec.stamps) > 2000:
                        del rec.stamps[:-2000]

    def snapshot(self) -> TfTreeSnapshot:
        with self._lock:
            return self._snapshot

    def compute_snapshot(self) -> TfTreeSnapshot:
        with self._lock:
            records = {k: FrameRecord(v.name, v.parent, v.is_static, list(v.stamps),
                                      v.last_update, set(v.parents_seen))
                       for k, v in self._records.items()}
        return analyze_tree(records, time.time())

    def _publish_tree(self) -> None:
        snap = self.compute_snapshot()
        with self._lock:
            self._snapshot = snap
        self.publish(TfTreeChanged(tree=snap))

    def frames(self) -> list[str]:
        return self.snapshot().names() or sorted(self._records)

    def clear(self) -> None:
        """Forget frames seen so far (after a robot restart, for example)."""
        with self._lock:
            self._records.clear()
            if self._buffer is not None:
                self._buffer.clear()

    # ----------------------------------------------------------------- lookups

    def lookup(self, target: str, source: str) -> TransformSample:
        """Latest transform from ``source`` to ``target`` (non-blocking)."""
        buf = self._buffer
        if buf is None:
            return TransformSample(target, source, 0.0, error='TF not subscribed')
        try:
            import rclpy.time
            t = buf.lookup_transform(target, source, rclpy.time.Time())
        except Exception as exc:  # noqa: BLE001 - tf2 Lookup/Connectivity/Extrapolation
            return TransformSample(target, source, 0.0, error=_tf_error(exc))
        tr = t.transform.translation
        q = t.transform.rotation
        stamp = t.header.stamp.sec + t.header.stamp.nanosec * 1e-9
        return TransformSample(target, source, stamp, (tr.x, tr.y, tr.z), (q.x, q.y, q.z, q.w))

    def start_echo(self, target: str, source: str, rate_hz: float = 10.0) -> str:
        echo_id = f'tfecho{next(self._ids)}'
        with self._lock:
            self._echoes[echo_id] = _TfEcho(echo_id, target, source,
                                            1.0 / max(0.1, min(rate_hz, 20.0)))
            if self._sub_tf is None:
                self._setup()
        return echo_id

    def update_echo(self, echo_id: str, target: str, source: str) -> None:
        with self._lock:
            e = self._echoes.get(echo_id)
            if e is not None:
                e.target, e.source = target, source

    def stop_echo(self, echo_id: str) -> None:
        with self._lock:
            self._echoes.pop(echo_id, None)
            if not self._consumers and not self._echoes:
                self._teardown()

    def _tick_echoes(self) -> None:
        now = time.time()
        with self._lock:
            due = [e for e in self._echoes.values() if now - e.last >= e.period]
            for e in due:
                e.last = now
        for e in due:
            self.publish(TransformSampleEvent(echo_id=e.id, sample=self.lookup(e.target,
                                                                               e.source)))


def _tf_error(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip() or name
    if 'Extrapolation' in name:
        return f'extrapolation: {text}'
    if 'Connectivity' in name:
        return f'frames not connected: {text}'
    if 'Lookup' in name:
        return f'lookup failed: {text}'
    return f'{name}: {text}'

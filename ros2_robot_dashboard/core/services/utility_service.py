"""UtilityService: global search, parameter comparison, plot sources."""

from __future__ import annotations

import itertools
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Optional

from ..domain.base import Serializable
from ..domain.events import PlotSample
from ..domain.params import ParamInfo, values_equal
from ..futures import gather, then
from ..msgs.introspection import extract_field, parse_path, to_float
from ..runtime import BaseService


@dataclass(frozen=True)
class SearchResult(Serializable):
    kind: str          # node | topic | service | action | parameter | frame
    name: str
    detail: str = ''
    node: str = ''     # for parameters


@dataclass(frozen=True)
class ParamComparison(Serializable):
    name: str
    left: Any
    right: Any
    left_present: bool
    right_present: bool

    @property
    def equal(self) -> bool:
        return self.left_present and self.right_present and values_equal(self.left, self.right)


class UtilityService(BaseService):
    name = 'utility'

    def __init__(self, rt):
        super().__init__(rt)
        self._plots: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def stop(self) -> None:
        for sid in list(self._plots):
            self.stop_plot(sid)

    # ----------------------------------------------------------------- search

    def search(self, text: str, limit: int = 300) -> list[SearchResult]:
        """Case-insensitive substring search in the graph, cached parameters and TF frames."""
        q = text.strip().lower()
        if not q:
            return []
        out: list[SearchResult] = []
        snap = self.rt.graph.snapshot()
        for n in snap.nodes:
            if q in n.fqn.lower():
                out.append(SearchResult('node', n.fqn))
        for t in snap.topics:
            if q in t.name.lower() or q in t.type.lower():
                out.append(SearchResult('topic', t.name, t.type))
        for s in snap.services:
            if q in s.name.lower() or q in s.type.lower():
                out.append(SearchResult('service', s.name, s.type))
        for a in snap.actions:
            if q in a.name.lower() or q in a.type.lower():
                out.append(SearchResult('action', a.name, a.type))
        params = self.rt.params
        for node in params.cached_nodes():
            for name, info in params.cached(node).items():
                if q in name.lower():
                    out.append(SearchResult('parameter', name, f'{node}', node))
        if self.rt.has_service('tf'):
            for f in self.rt.tf.snapshot().frames:
                if q in f.name.lower():
                    out.append(SearchResult('frame', f.name, f'parent: {f.parent or "(root)"}'))
        return out[:limit]

    # ----------------------------------------------------------------- comparison

    def compare_params(self, left_node: str, right_node: str) -> Future:
        """Future of list[ParamComparison] between two nodes."""
        params = self.rt.params
        return then(gather([params.load(left_node), params.load(right_node)]),
                    lambda r: compare_param_lists(r[0], r[1]))

    def compare_with_values(self, node: str, values: dict) -> Future:
        """Compare a node with preset values (``{dotted.name: value}``)."""
        return then(self.rt.params.load(node),
                    lambda params: compare_param_lists(
                        params, [ParamInfo(k, _ptype(v), v) for k, v in values.items()]))

    # ----------------------------------------------------------------- plot sources

    def start_plot(self, topic: str, path: str, max_rate_hz: float = 50.0,
                   type_name: Optional[str] = None) -> str:
        parts = parse_path(path)
        sid = f'plot{next(self._ids)}'
        state = {'topic': topic, 'path': path, 'parts': parts, 'last': 0.0,
                 'period': 1.0 / max(1.0, max_rate_hz), 'error': ''}
        with self._lock:
            self._plots[sid] = state

        def on_sample(sample) -> None:
            now = sample.receive_time
            if now - state['last'] < state['period']:
                return
            state['last'] = now
            try:
                value = to_float(extract_field(sample.msg, parts))
            except Exception as exc:  # noqa: BLE001
                if not state['error']:
                    state['error'] = str(exc)
                    self.error(f"plot {topic} {path}: {exc}")
                return
            self.publish(PlotSample(source_id=sid, topic=topic, path=path, t=now, value=value))

        try:
            self.rt.topics.acquire(topic, sid, on_sample, type_name=type_name)
        except Exception:
            with self._lock:
                self._plots.pop(sid, None)
            raise
        return sid

    def stop_plot(self, sid: str) -> None:
        with self._lock:
            self._plots.pop(sid, None)
        self.rt.topics.release(sid)

    def numeric_fields(self, topic: str) -> list[str]:
        from ..msgs.introspection import get_message_class, numeric_paths
        return numeric_paths(get_message_class(self.rt.topics.topic_type(topic)))


def _ptype(v):
    from ..domain.params import infer_type
    return infer_type(v)


def compare_param_lists(left: list[ParamInfo], right: list[ParamInfo]) -> list[ParamComparison]:
    lmap = {p.name: p for p in left}
    rmap = {p.name: p for p in right}
    out = []
    for name in sorted(set(lmap) | set(rmap)):
        lp, rp = lmap.get(name), rmap.get(name)
        out.append(ParamComparison(name, lp.value if lp else None, rp.value if rp else None,
                                   lp is not None, rp is not None))
    return out

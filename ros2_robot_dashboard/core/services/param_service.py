"""ParamService: list, read and set parameters of any node, live updates.

Uses the ``rcl_interfaces`` services directly (no ``AsyncParameterClient``, which
Humble does not have), see design v1 section 2.2.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Iterable, Optional

from ..domain.events import NodesChanged, ParamEvent, ParamNodeStatus
from ..domain.params import (NodeStatus, ParamDescriptorInfo, ParamInfo, ParamType, SetResult,
                             coerce_value, descriptor_from_msg, infer_type, param_type_from_msg,
                             value_from_msg, value_to_msg)
from ..futures import gather, then, try_set_exception, try_set_result
from ..runtime import BaseService


class ParamService(BaseService):
    name = 'params'

    def __init__(self, rt):
        super().__init__(rt)
        self._sub = None
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, ParamInfo]] = {}
        self._status: dict[str, NodeStatus] = {}
        self._loading: dict[str, list] = {}       # node -> buffered events while loading
        self._bus_sub = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        from rcl_interfaces.msg import ParameterEvent
        from rclpy.qos import qos_profile_parameter_events

        self._sub = self.rt.create_subscription(ParameterEvent, '/parameter_events',
                                                self._on_event, qos_profile_parameter_events,
                                                source='params.events')
        self._bus_sub = self.rt.bus.subscribe(NodesChanged, self._on_nodes_changed)

    def stop(self) -> None:
        self.rt.destroy_subscription(self._sub)
        if self._bus_sub is not None:
            self.rt.bus.unsubscribe(self._bus_sub)

    # ----------------------------------------------------------------- queries

    def cached(self, node: str) -> dict[str, ParamInfo]:
        with self._lock:
            return dict(self._cache.get(node, {}))

    def cached_nodes(self) -> list[str]:
        with self._lock:
            return sorted(self._cache)

    def status(self, node: str) -> NodeStatus:
        return self._status.get(node, NodeStatus.DISCOVERED)

    def is_reachable(self, node: str) -> bool:
        from rcl_interfaces.srv import ListParameters
        return self.rt.service_is_ready(ListParameters, f'{node}/list_parameters')

    def _set_status(self, node: str, status: NodeStatus, message: str = '') -> None:
        self._status[node] = status
        self.publish(ParamNodeStatus(node=node, status=status, message=message))

    # ----------------------------------------------------------------- load

    def load(self, node: str) -> Future:
        """Future of ``list[ParamInfo]`` for all parameters of ``node``."""
        from rcl_interfaces.srv import ListParameters

        with self._lock:
            self._loading.setdefault(node, [])
        self._set_status(node, NodeStatus.LOADING)
        req = ListParameters.Request()
        req.depth = 0
        req.prefixes = []
        fut = self.rt.call_service(ListParameters, f'{node}/list_parameters', req)
        result = then(fut, lambda res: self._load_values(node, list(res.result.names)))
        out: Future = Future()

        def done(f: Future) -> None:
            exc = f.exception()
            with self._lock:
                buffered = self._loading.pop(node, [])
            if exc is not None:
                self._set_status(node, NodeStatus.UNREACHABLE, str(exc))
                try_set_exception(out, exc)
                return
            params = f.result()
            with self._lock:
                self._cache[node] = {p.name: p for p in params}
            self._set_status(node, NodeStatus.READY)
            try_set_result(out, params)
            # Events received while loading are applied after the load (v1, rule 9).
            for ev in buffered:
                self._apply_event(*ev)

        result.add_done_callback(done)
        return out

    def _chunks(self, names: list[str]) -> list[list[str]]:
        n = max(1, int(self.config.get('params.chunk_size', 50)))
        return [names[i:i + n] for i in range(0, len(names), n)] or [[]]

    def _load_values(self, node: str, names: list[str]) -> Future:
        names = sorted(set(names))
        if not names:
            out: Future = Future()
            out.set_result([])
            return out
        futs = []
        for chunk in self._chunks(names):
            futs.append(gather([self._get_values(node, chunk), self._describe(node, chunk)]))

        def combine(results: list) -> list[ParamInfo]:
            params = []
            for chunk, (values, descs) in zip(self._chunks(names), results):
                for name, value_msg, desc in zip(chunk, values, descs):
                    params.append(ParamInfo(name, param_type_from_msg(value_msg),
                                            value_from_msg(value_msg), desc))
            return params

        return then(gather(futs), combine)

    def _get_values(self, node: str, names: list[str]) -> Future:
        from rcl_interfaces.srv import GetParameters
        req = GetParameters.Request()
        req.names = names

        def check(res):
            if len(res.values) != len(names):
                raise RuntimeError(f'{node}: get_parameters returned {len(res.values)} values '
                                   f'for {len(names)} names')
            return list(res.values)

        return then(self.rt.call_service(GetParameters, f'{node}/get_parameters', req), check)

    def _describe(self, node: str, names: list[str]) -> Future:
        from rcl_interfaces.srv import DescribeParameters
        req = DescribeParameters.Request()
        req.names = names

        def convert(res):
            descs = [descriptor_from_msg(d) for d in res.descriptors]
            if len(descs) != len(names):
                descs = (descs + [ParamDescriptorInfo()] * len(names))[:len(names)]
            return descs

        return then(self.rt.call_service(DescribeParameters, f'{node}/describe_parameters', req),
                    convert)

    def describe(self, node: str, names: list[str]) -> Future:
        return self._describe(node, names)

    # ----------------------------------------------------------------- set

    def _build_parameters(self, node: str, values: dict[str, Any]) -> list:
        from rcl_interfaces.msg import Parameter
        cache = self.cached(node)
        params = []
        for name, value in values.items():
            info = cache.get(name)
            if info is None:
                ptype = infer_type(value)
            elif info.descriptor.dynamic_typing:
                # Dynamic typing: the set uses the type of the new value (v1, rule 6).
                try:
                    coerce_value(value, info.type)
                    ptype = info.type
                except ValueError:
                    ptype = infer_type(value)
            else:
                ptype = info.type
            p = Parameter()
            p.name = name
            p.value = value_to_msg(value, ptype)
            params.append(p)
        return params

    def set(self, node: str, values: dict[str, Any]) -> Future:
        """Future of ``list[SetResult]`` (one per parameter)."""
        from rcl_interfaces.srv import SetParameters
        out: Future = Future()
        try:
            self.rt.safety.check_write('set_parameters', node)
            req = SetParameters.Request()
            req.parameters = self._build_parameters(node, values)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        names = [p.name for p in req.parameters]

        def convert(res) -> list[SetResult]:
            results = [SetResult(n, bool(r.successful), r.reason)
                       for n, r in zip(names, res.results)]
            self._after_set(node, values, results)
            return results

        return self._logged(then(self.rt.call_service(SetParameters, f'{node}/set_parameters',
                                                      req), convert),
                            'set_parameters', node, values)

    def set_atomic(self, node: str, values: dict[str, Any]) -> Future:
        """Future of ``list[SetResult]`` (same outcome for every parameter)."""
        from rcl_interfaces.srv import SetParametersAtomically
        out: Future = Future()
        try:
            self.rt.safety.check_write('set_parameters_atomically', node)
            req = SetParametersAtomically.Request()
            req.parameters = self._build_parameters(node, values)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        names = [p.name for p in req.parameters]

        def convert(res) -> list[SetResult]:
            r = res.result
            results = [SetResult(n, bool(r.successful), r.reason) for n in names]
            self._after_set(node, values, results)
            return results

        return self._logged(
            then(self.rt.call_service(SetParametersAtomically,
                                      f'{node}/set_parameters_atomically', req), convert),
            'set_parameters_atomically', node, values)

    def _logged(self, fut: Future, action: str, node: str, values: dict) -> Future:
        detail = ', '.join(f'{k}={v!r}' for k, v in values.items())
        if len(detail) > 500:
            detail = detail[:500] + '...'

        def cb(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                self.rt.safety.log(action, node, False, f'{detail} -> {exc}')
            else:
                failed = [r for r in f.result() if not r.successful]
                self.rt.safety.log(action, node, not failed,
                                   detail + ('' if not failed else
                                             ' -> rejected: ' + '; '.join(
                                                 f'{r.name}: {r.reason}' for r in failed)))

        fut.add_done_callback(cb)
        return fut

    def _after_set(self, node: str, values: dict, results: Iterable[SetResult]) -> None:
        with self._lock:
            cache = self._cache.get(node)
            if cache is None:
                return
            for r in results:
                info = cache.get(r.name)
                if r.successful and info is not None:
                    try:
                        v = coerce_value(values[r.name], info.type)
                    except (ValueError, KeyError):
                        continue
                    cache[r.name] = ParamInfo(info.name, info.type, v, info.descriptor)

    # ----------------------------------------------------------------- events

    def _on_event(self, msg) -> None:
        node = msg.node if msg.node.startswith('/') else '/' + msg.node
        if node == f'/{self.rt.node_name}':
            return
        items = ([('new', p) for p in msg.new_parameters]
                 + [('changed', p) for p in msg.changed_parameters]
                 + [('deleted', p) for p in msg.deleted_parameters])
        for kind, p in items:
            info = ParamInfo(p.name, param_type_from_msg(p.value), value_from_msg(p.value))
            with self._lock:
                if node in self._loading:
                    self._loading[node].append((node, info, kind))
                    continue
            self._apply_event(node, info, kind)

    def _apply_event(self, node: str, info: ParamInfo, kind: str) -> None:
        with self._lock:
            cache = self._cache.get(node)
            known = cache.get(info.name) if cache is not None else None
        if kind == 'deleted':
            if cache is not None:
                with self._lock:
                    cache.pop(info.name, None)
            self.publish(ParamEvent(node=node, param=info, kind=kind))
            return
        if known is not None:
            info = ParamInfo(info.name, info.type, info.value, known.descriptor)
            with self._lock:
                cache[info.name] = info
            self.publish(ParamEvent(node=node, param=info, kind=kind))
            return
        if kind == 'new' and cache is not None:
            # The descriptor is not part of the event: ask for it.
            def with_desc(descs) -> None:
                full = ParamInfo(info.name, info.type, info.value, descs[0])
                with self._lock:
                    if node in self._cache:
                        self._cache[node][info.name] = full
                self.publish(ParamEvent(node=node, param=full, kind=kind))

            f = self._describe(node, [info.name])
            f.add_done_callback(lambda fut: with_desc(fut.result()) if fut.exception() is None
                                else self.publish(ParamEvent(node=node, param=info, kind=kind)))
            return
        self.publish(ParamEvent(node=node, param=info, kind=kind))

    def _on_nodes_changed(self, ev: NodesChanged) -> None:
        current = {n.fqn for n in ev.nodes}
        for node in ev.removed:
            if node in self._status or node in self._cache:
                self._set_status(node, NodeStatus.GONE)
            self.rt.release_clients(node + '/')
        for node in ev.added:
            if self._status.get(node) == NodeStatus.GONE:
                self._set_status(node, NodeStatus.DISCOVERED)
        # Keep the cache only for nodes that exist or are still shown as gone.
        with self._lock:
            for node in list(self._cache):
                if node not in current and self._status.get(node) != NodeStatus.GONE:
                    del self._cache[node]

    # ----------------------------------------------------------------- helpers

    def get_value(self, node: str, name: str, timeout: Optional[float] = None) -> Future:
        """Future of the Python value of one parameter."""
        return then(self._get_values(node, [name]), lambda v: value_from_msg(v[0]))

    def visible(self, name: str) -> bool:
        import re
        pattern = self.config.get('params.hide_regex', '')
        return not (pattern and re.match(pattern, name))


__all__ = ['ParamService', 'ParamType']

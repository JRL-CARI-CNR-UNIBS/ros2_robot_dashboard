"""LifecycleService: state and transitions of managed (lifecycle) nodes."""

from __future__ import annotations

import threading
from concurrent.futures import Future

from ..domain.events import GraphChanged, LifecycleNodesChanged
from ..domain.logs import LifecycleNodeInfo, LifecycleTransition
from ..futures import gather, then, try_set_exception, try_set_result
from ..runtime import BaseService

GET_STATE_TYPE = 'lifecycle_msgs/srv/GetState'


class LifecycleService(BaseService):
    name = 'lifecycle'

    def __init__(self, rt):
        super().__init__(rt)
        self._nodes: tuple[str, ...] = ()
        self._info: dict[str, LifecycleNodeInfo] = {}
        self._consumers: set[str] = set()
        self._lock = threading.Lock()
        self._timer = None
        self._bus_sub = None
        self._polling = False

    def start(self) -> None:
        self._bus_sub = self.rt.bus.subscribe(GraphChanged, self._on_graph)
        period = float(self.config.get('polling.lifecycle_s', 2.0))
        self._timer = self.rt.create_timer(period, self._poll, 'lifecycle.poll')

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)
        if self._bus_sub is not None:
            self.rt.bus.unsubscribe(self._bus_sub)

    def nodes(self) -> tuple[str, ...]:
        return self._nodes

    def info(self) -> list[LifecycleNodeInfo]:
        return [self._info[n] for n in self._nodes if n in self._info]

    def _on_graph(self, ev: GraphChanged) -> None:
        found = tuple(sorted(s.name[:-len('/get_state')] for s in ev.graph.services
                             if s.name.endswith('/get_state') and GET_STATE_TYPE in s.types))
        if found != self._nodes:
            self._nodes = found
            for n in list(self._info):
                if n not in found:
                    del self._info[n]
            if self._consumers:
                self._poll()

    def watch(self, consumer_id: str) -> None:
        self._consumers.add(consumer_id)
        self.rt.submit(self._poll)

    def unwatch(self, consumer_id: str) -> None:
        self._consumers.discard(consumer_id)

    def _poll(self) -> None:
        if not self._consumers or self._polling:
            return
        self._polling = True
        nodes = list(self._nodes)
        if not nodes:
            self._polling = False
            self.publish(LifecycleNodesChanged(nodes=()))
            return

        def done(f: Future) -> None:
            self._polling = False
            self.publish(LifecycleNodesChanged(nodes=tuple(self.info())))

        gather([self.refresh(n) for n in nodes]).add_done_callback(done)

    def refresh(self, node: str) -> Future:
        """Future of LifecycleNodeInfo (never fails: errors are in ``info.error``)."""
        out: Future = Future()

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                prev = self._info.get(node)
                info = LifecycleNodeInfo(node, prev.state_id if prev else 0,
                                         prev.state_label if prev else 'unknown', (), str(exc))
            else:
                (sid, label), transitions = f.result()
                info = LifecycleNodeInfo(node, sid, label, tuple(transitions))
            self._info[node] = info
            try_set_result(out, info)

        gather([self.get_state(node), self.available_transitions(node)]).add_done_callback(done)
        return out

    def get_state(self, node: str) -> Future:
        from lifecycle_msgs.srv import GetState
        return then(self.rt.call_service(GetState, f'{node}/get_state', GetState.Request()),
                    lambda r: (int(r.current_state.id), r.current_state.label))

    def available_transitions(self, node: str) -> Future:
        from lifecycle_msgs.srv import GetAvailableTransitions
        return then(self.rt.call_service(GetAvailableTransitions,
                                         f'{node}/get_available_transitions',
                                         GetAvailableTransitions.Request()),
                    lambda r: [LifecycleTransition(int(t.transition.id), t.transition.label,
                                                   t.start_state.label, t.goal_state.label)
                               for t in r.available_transitions])

    def change_state(self, node: str, transition_id: int, label: str = '') -> Future:
        """Future of ``(ok, message)``."""
        from lifecycle_msgs.srv import ChangeState
        out: Future = Future()
        action = 'lifecycle_change_state'
        detail = f'{label or transition_id}'
        try:
            self.rt.safety.check_write(action, node)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        req = ChangeState.Request()
        req.transition.id = int(transition_id)
        req.transition.label = label
        fut = self.rt.call_service(ChangeState, f'{node}/change_state', req,
                                   timeout=float(self.config.get('timeouts.lifecycle_s', 5.0)))

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                self.rt.safety.log(action, node, False, f'{detail} -> {exc}')
                try_set_exception(out, exc)
            else:
                ok = bool(f.result().success)
                self.rt.safety.log(action, node, ok, detail)
                try_set_result(out, (ok, '' if ok else 'transition rejected by the node'))
            self.refresh(node).add_done_callback(
                lambda _f: self.publish(LifecycleNodesChanged(nodes=tuple(self.info()))))

        fut.add_done_callback(done)
        return out

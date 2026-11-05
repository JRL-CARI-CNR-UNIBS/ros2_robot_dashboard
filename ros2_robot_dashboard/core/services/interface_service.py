"""InterfaceService: generic service calls and action goals from dicts/YAML.

Used by the Services/Actions page. Not in the original list of design v2 services,
added because calling services and sending goals needs a core backend.
"""

from __future__ import annotations

import itertools
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Optional

from ..domain.events import Event
from ..futures import try_set_exception, try_set_result
from ..msgs.introspection import (fill_message, get_action_class, get_service_class,
                                  message_to_dict)
from ..runtime import BaseService


@dataclass(frozen=True)
class ActionFeedback(Event):
    goal_id: str
    action: str
    feedback: dict


@dataclass(frozen=True)
class ActionStatusChanged(Event):
    goal_id: str
    action: str
    # sending | accepted | rejected | succeeded | aborted | canceled | error
    status: str
    result: Optional[dict] = None
    message: str = ''


_STATUS = {0: 'unknown', 1: 'accepted', 2: 'executing', 3: 'canceling', 4: 'succeeded',
           5: 'canceled', 6: 'aborted'}


@dataclass
class _Goal:
    id: str
    action: str
    handle: Any = None
    client: Any = None
    done: bool = False


class InterfaceService(BaseService):
    name = 'interfaces'

    def __init__(self, rt):
        super().__init__(rt)
        self._action_clients: dict[tuple, Any] = {}
        self._goals: dict[str, _Goal] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def stop(self) -> None:
        for g in list(self._goals.values()):
            if not g.done and g.handle is not None:
                try:
                    g.handle.cancel_goal_async()
                except Exception:  # noqa: BLE001
                    pass
        for c in self._action_clients.values():
            try:
                c.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._action_clients.clear()

    # ----------------------------------------------------------------- services

    def call(self, service: str, type_name: str, request: Optional[dict] = None,
             timeout: Optional[float] = None) -> Future:
        """Future of the response as a dict."""
        out: Future = Future()
        try:
            self.rt.safety.check_write('call_service', service)
            cls = get_service_class(type_name)
            req = fill_message(cls.Request, request or {})
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        fut = self.rt.call_service(cls, service, req, timeout=timeout)

        def done(f: Future) -> None:
            exc = f.exception()
            self.rt.safety.log('call_service', service, exc is None,
                               f'{type_name} {request!r}'[:400] + (f' -> {exc}' if exc else ''))
            if exc is not None:
                try_set_exception(out, exc)
            else:
                try_set_result(out, message_to_dict(f.result()))

        fut.add_done_callback(done)
        return out

    # ----------------------------------------------------------------- actions

    def _client(self, action: str, type_name: str):
        from rclpy.action import ActionClient
        key = (action, type_name)
        with self._lock:
            c = self._action_clients.get(key)
            if c is None:
                c = ActionClient(self.rt.node, get_action_class(type_name), action,
                                 callback_group=self.rt.callback_group)
                self._action_clients[key] = c
            return c

    def send_goal(self, action: str, type_name: str, goal: Optional[dict] = None,
                  wait_s: float = 2.0) -> str:
        """Send a goal; returns a goal id. Progress arrives as events."""
        self.rt.safety.check_write('send_goal', action)
        cls = get_action_class(type_name)
        goal_msg = fill_message(cls.Goal, goal or {})
        client = self._client(action, type_name)
        gid = f'goal{next(self._ids)}'
        g = _Goal(gid, action, client=client)
        self._goals[gid] = g
        self.publish(ActionStatusChanged(goal_id=gid, action=action, status='sending'))
        self.rt.safety.log('send_goal', action, True, f'{type_name} {goal!r}'[:400])

        def run() -> None:
            if not client.wait_for_server(timeout_sec=wait_s):
                g.done = True
                self.publish(ActionStatusChanged(goal_id=gid, action=action, status='error',
                                                 message='action server not available'))
                return
            fut = client.send_goal_async(
                goal_msg, feedback_callback=lambda fb: self.publish(ActionFeedback(
                    goal_id=gid, action=action, feedback=message_to_dict(fb.feedback))))
            fut.add_done_callback(lambda f: self._on_goal_response(g, f))

        self.rt.submit(run)
        return gid

    def _on_goal_response(self, g: _Goal, fut) -> None:
        try:
            handle = fut.result()
        except Exception as exc:  # noqa: BLE001
            g.done = True
            self.publish(ActionStatusChanged(goal_id=g.id, action=g.action, status='error',
                                             message=str(exc)))
            return
        if not handle.accepted:
            g.done = True
            self.publish(ActionStatusChanged(goal_id=g.id, action=g.action, status='rejected'))
            return
        g.handle = handle
        self.publish(ActionStatusChanged(goal_id=g.id, action=g.action, status='accepted'))
        handle.get_result_async().add_done_callback(lambda f: self._on_result(g, f))

    def _on_result(self, g: _Goal, fut) -> None:
        g.done = True
        try:
            res = fut.result()
        except Exception as exc:  # noqa: BLE001
            self.publish(ActionStatusChanged(goal_id=g.id, action=g.action, status='error',
                                             message=str(exc)))
            return
        self.publish(ActionStatusChanged(goal_id=g.id, action=g.action,
                                         status=_STATUS.get(int(res.status), str(res.status)),
                                         result=message_to_dict(res.result)))

    def cancel_goal(self, goal_id: str) -> Future:
        g = self._goals.get(goal_id)
        out: Future = Future()
        if g is None or g.handle is None or g.done:
            out.set_result(False)
            return out
        self.rt.safety.log('cancel_goal', g.action, True, goal_id)
        f = g.handle.cancel_goal_async()
        f.add_done_callback(lambda r: try_set_result(out, bool(r.result().goals_canceling)
                                                     if r.exception() is None else False))
        return out

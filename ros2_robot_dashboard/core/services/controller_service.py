"""ControllerService: ros2_control with any number of controller managers."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Iterable, Optional

from ..domain.control import (HW_STATES, ControllerInfo, HwInterfaceInfo, SwitchPlan,
                              interface_owners, plan_switch)
from ..domain.events import ControllerManagersChanged, ControllerStateChanged, GraphChanged
from ..futures import gather, then, try_set_exception, try_set_result
from ..runtime import BaseService

LIST_CONTROLLERS_TYPE = 'controller_manager_msgs/srv/ListControllers'


class ControllerService(BaseService):
    name = 'controllers'

    def __init__(self, rt):
        super().__init__(rt)
        self._managers: tuple[str, ...] = ()
        self._watchers: dict[str, set[str]] = {}       # manager -> consumer ids
        self._last: dict[str, ControllerStateChanged] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._timer = None
        self._bus_sub = None

    def start(self) -> None:
        self._bus_sub = self.rt.bus.subscribe(GraphChanged, self._on_graph)
        period = float(self.config.get('polling.controllers_s', 1.0))
        self._timer = self.rt.create_timer(period, self._poll_watched, 'controllers.poll')

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)
        if self._bus_sub is not None:
            self.rt.bus.unsubscribe(self._bus_sub)

    # ----------------------------------------------------------------- discovery

    def managers(self) -> tuple[str, ...]:
        return self._managers

    def _on_graph(self, ev: GraphChanged) -> None:
        found = sorted(s.name[:-len('/list_controllers')] or '/'
                       for s in ev.graph.services
                       if s.name.endswith('/list_controllers') and LIST_CONTROLLERS_TYPE in s.types)
        found_t = tuple(found)
        if found_t != self._managers:
            self._managers = found_t
            self.publish(ControllerManagersChanged(managers=found_t))

    # ----------------------------------------------------------------- watching

    def watch(self, manager: str, consumer_id: str) -> None:
        with self._lock:
            self._watchers.setdefault(manager, set()).add(consumer_id)
        self.refresh(manager)

    def unwatch(self, manager: str, consumer_id: str) -> None:
        with self._lock:
            ids = self._watchers.get(manager)
            if ids is not None:
                ids.discard(consumer_id)
                if not ids:
                    del self._watchers[manager]

    def last_state(self, manager: str) -> Optional[ControllerStateChanged]:
        return self._last.get(manager)

    def _poll_watched(self) -> None:
        with self._lock:
            managers = list(self._watchers)
        for m in managers:
            self.refresh(m)

    def refresh(self, manager: str) -> Future:
        """Read controllers, interfaces and components; publishes ControllerStateChanged."""
        with self._lock:
            if manager in self._in_flight:
                f: Future = Future()
                f.set_result(self._last.get(manager))
                return f
            self._in_flight.add(manager)
        out: Future = Future()

        def done(f: Future) -> None:
            with self._lock:
                self._in_flight.discard(manager)
            exc = f.exception()
            if exc is not None:
                prev = self._last.get(manager)
                ev = ControllerStateChanged(
                    manager=manager, controllers=prev.controllers if prev else (),
                    interfaces=prev.interfaces if prev else (),
                    components=prev.components if prev else (), error=str(exc))
            else:
                ev = f.result()
            self._last[manager] = ev
            self.publish(ev)
            try_set_result(out, ev)

        gather([self.list_controllers(manager), self._safe(self.list_hardware_interfaces(manager)),
                self._safe(self.list_hardware_components(manager))]).add_done_callback(
            lambda f: done(then(f, lambda r: self._compose(manager, *r))))
        return out

    @staticmethod
    def _safe(fut: Future) -> Future:
        """Optional parts (interfaces, components) must not hide the controllers."""
        out: Future = Future()
        fut.add_done_callback(lambda f: try_set_result(out, () if f.exception() else f.result()))
        return out

    def _compose(self, manager: str, controllers, interfaces, components) -> ControllerStateChanged:
        owners = interface_owners(controllers)
        itfs = tuple(HwInterfaceInfo(i.name, i.kind, i.is_available, i.is_claimed, i.data_type,
                                     owners.get(i.name, '')) for i in interfaces)
        return ControllerStateChanged(manager=manager, controllers=tuple(controllers),
                                      interfaces=itfs, components=tuple(components))

    # ----------------------------------------------------------------- reads

    def _srv(self, manager: str, name: str) -> str:
        return f'{manager.rstrip("/")}/{name}'

    def list_controllers(self, manager: str) -> Future:
        from controller_manager_msgs.srv import ListControllers
        c = self.rt.compat
        return then(self.rt.call_service(ListControllers, self._srv(manager, 'list_controllers'),
                                         ListControllers.Request()),
                    lambda r: sorted((c.controller_from_msg(m) for m in r.controller),
                                     key=lambda x: x.name))

    def list_hardware_interfaces(self, manager: str) -> Future:
        from controller_manager_msgs.srv import ListHardwareInterfaces
        c = self.rt.compat

        def conv(r):
            return ([c.hw_interface_from_msg(i, 'command') for i in r.command_interfaces]
                    + [c.hw_interface_from_msg(i, 'state') for i in r.state_interfaces])

        return then(self.rt.call_service(ListHardwareInterfaces,
                                         self._srv(manager, 'list_hardware_interfaces'),
                                         ListHardwareInterfaces.Request()), conv)

    def list_hardware_components(self, manager: str) -> Future:
        from controller_manager_msgs.srv import ListHardwareComponents
        c = self.rt.compat
        return then(self.rt.call_service(ListHardwareComponents,
                                         self._srv(manager, 'list_hardware_components'),
                                         ListHardwareComponents.Request()),
                    lambda r: sorted((c.hw_component_from_msg(m) for m in r.component),
                                     key=lambda x: x.name))

    # ----------------------------------------------------------------- planning

    def plan_switch(self, manager: str, activate: Iterable[str],
                    deactivate: Iterable[str]) -> SwitchPlan:
        last = self._last.get(manager)
        controllers: tuple[ControllerInfo, ...] = last.controllers if last else ()
        return plan_switch(controllers, activate, deactivate)

    # ----------------------------------------------------------------- writes

    def _write(self, action: str, manager: str, detail: str, fut_factory) -> Future:
        out: Future = Future()
        try:
            self.rt.safety.check_write(action, manager)
            fut = fut_factory()
        except Exception as exc:  # noqa: BLE001
            try_set_exception(out, exc)
            return out

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                self.rt.safety.log(action, manager, False, f'{detail} -> {exc}')
                try_set_exception(out, exc)
            else:
                ok, message = f.result()
                self.rt.safety.log(action, manager, ok, detail + (f' -> {message}' if message
                                                                  else ''))
                try_set_result(out, (ok, message))
            self.refresh(manager)

        fut.add_done_callback(done)
        return out

    def switch(self, manager: str, activate: Iterable[str], deactivate: Iterable[str],
               strict: bool = True, timeout_s: Optional[float] = None) -> Future:
        """Future of ``(ok, message)``."""
        from controller_manager_msgs.srv import SwitchController
        act, deact = list(activate), list(deactivate)
        timeout_s = float(timeout_s if timeout_s is not None else
                          self.config.get('timeouts.switch_controller_s', 5.0))
        compat = self.rt.compat
        detail = (f'activate={act} deactivate={deact} '
                  f'{"strict" if strict else "best_effort"} timeout={timeout_s:g}s')

        def call():
            req = compat.make_switch_request(act, deact, strict, timeout_s)
            return then(self.rt.call_service(SwitchController, self._srv(manager,
                                                                         'switch_controller'),
                                             req, timeout=timeout_s + 2.0),
                        compat.switch_result)

        return self._write('switch_controller', manager, detail, call)

    def _simple(self, srv_name: str, srv_cls, manager: str, controller: str) -> Future:
        def call():
            req = srv_cls.Request()
            req.name = controller
            return then(self.rt.call_service(srv_cls, self._srv(manager, srv_name), req,
                                             timeout=float(self.config.get(
                                                 'timeouts.switch_controller_s', 5.0))),
                        lambda r: (bool(r.ok), ''))

        return self._write(srv_name, manager, controller, call)

    def load(self, manager: str, controller: str) -> Future:
        from controller_manager_msgs.srv import LoadController
        return self._simple('load_controller', LoadController, manager, controller)

    def configure(self, manager: str, controller: str) -> Future:
        from controller_manager_msgs.srv import ConfigureController
        return self._simple('configure_controller', ConfigureController, manager, controller)

    def unload(self, manager: str, controller: str) -> Future:
        from controller_manager_msgs.srv import UnloadController
        return self._simple('unload_controller', UnloadController, manager, controller)

    def cleanup(self, manager: str, controller: str) -> Future:
        import controller_manager_msgs.srv as srv
        if not hasattr(srv, 'CleanupController'):
            f: Future = Future()
            f.set_exception(NotImplementedError('cleanup_controller is not available on '
                                                f'{self.rt.compat.distro()}'))
            return f
        return self._simple('cleanup_controller', srv.CleanupController, manager, controller)

    def set_hardware_component_state(self, manager: str, component: str, state: str) -> Future:
        """``state``: unconfigured | inactive | active | finalized. Future of (ok, message)."""
        from controller_manager_msgs.srv import SetHardwareComponentState
        from lifecycle_msgs.msg import State

        def call():
            req = SetHardwareComponentState.Request()
            req.name = component
            st = State()
            st.id = HW_STATES[state]
            st.label = state
            req.target_state = st
            return then(self.rt.call_service(SetHardwareComponentState,
                                             self._srv(manager, 'set_hardware_component_state'),
                                             req, timeout=float(self.config.get(
                                                 'timeouts.switch_controller_s', 5.0))),
                        lambda r: (bool(r.ok), f'state: {r.state.label}'))

        if state not in HW_STATES:
            f: Future = Future()
            f.set_exception(ValueError(f'unknown state {state}'))
            return f
        return self._write('set_hardware_component_state', manager, f'{component} -> {state}',
                           call)

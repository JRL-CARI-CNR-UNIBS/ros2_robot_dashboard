"""CoreRuntime: rclpy node, executor thread, event bus, compat layer and services.

The runtime uses its own rclpy ``Context`` so it never interferes with other rclpy
users in the same process (tests, embedding).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from .compat import CompatLayer, create_compat
from .config import Config, state_dir
from .domain.events import CoreError, CoreStatus
from .event_bus import EventBus
from .futures import ServiceTimeout, ServiceUnavailable, try_set_exception, try_set_result
from .safety import Safety

_log = logging.getLogger(__name__)


class Scheduler:
    """One thread that runs delayed callbacks (timeouts) without one thread per timer."""

    def __init__(self) -> None:
        self._heap: list = []
        self._cond = threading.Condition()
        self._counter = itertools.count()
        self._running = True
        self._thread = threading.Thread(target=self._run, name='core-scheduler', daemon=True)
        self._thread.start()

    def call_later(self, delay: float, fn: Callable[[], None]) -> list:
        handle = [True]          # handle[0] = still active
        with self._cond:
            heapq.heappush(self._heap, (time.monotonic() + delay, next(self._counter), fn,
                                        handle))
            self._cond.notify()
        return handle

    @staticmethod
    def cancel(handle: list) -> None:
        handle[0] = False

    def stop(self) -> None:
        with self._cond:
            self._running = False
            self._cond.notify()
        self._thread.join(1.0)

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._running and (not self._heap or self._heap[0][0] > time.monotonic()):
                    wait = (self._heap[0][0] - time.monotonic()) if self._heap else None
                    self._cond.wait(wait)
                if not self._running:
                    return
                _, _, fn, handle = heapq.heappop(self._heap)
            if handle[0]:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    _log.exception('scheduled callback failed')


class BaseService:
    """Base class of the core services. Subclasses override ``start``/``stop``."""

    name = 'base'

    def __init__(self, rt: 'CoreRuntime'):
        self.rt = rt

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    # convenience
    @property
    def node(self):
        return self.rt.node

    @property
    def config(self) -> Config:
        return self.rt.config

    def publish(self, event) -> None:
        self.rt.bus.publish(event)

    def error(self, message: str, exc: Optional[BaseException] = None) -> None:
        self.rt.report_error(self.name, message, exc)


class CoreRuntime:
    def __init__(self, config: Optional[Config] = None, compat: Optional[CompatLayer] = None,
                 node_name: Optional[str] = None, read_only: Optional[bool] = None,
                 enable_services: Optional[list[str]] = None):
        self.config = config or Config()
        distro = self.config.get('distro_override') or None
        self.compat: CompatLayer = compat or create_compat(distro)
        self.bus = EventBus()
        self.bus.set_error_handler(self._on_bus_error)
        ro = self.config.get('read_only', False) if read_only is None else read_only
        self.safety = Safety(
            read_only=bool(ro),
            log_path=os.path.join(state_dir(), 'actions.log'),
            risky_words=self.config.get('safety.risky_service_words', []),
            critical_params=self.config.get('params.critical', []),
        )
        self.node_name = node_name or f'robot_dashboard_{os.getpid()}'
        self._enable = enable_services
        self._services: dict[str, BaseService] = {}
        self._context = None
        self._node = None
        self._executor = None
        self._spin_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.RLock()
        self._clients: dict[tuple, Any] = {}
        self._cb_group = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._scheduler: Optional[Scheduler] = None
        self._timers: list = []

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> 'CoreRuntime':
        with self._lock:
            if self._running:
                return self
            import rclpy
            from rclpy.callback_groups import ReentrantCallbackGroup
            from rclpy.context import Context
            from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor

            self._context = Context()
            rclpy.init(context=self._context)
            self._node = rclpy.create_node(self.node_name, context=self._context,
                                           start_parameter_services=False)
            self._cb_group = ReentrantCallbackGroup()
            # Single-threaded by default: rclpy's MultiThreadedExecutor handles ~20x fewer
            # messages per second (measured: 25 vs 480 msg/s on a 500 Hz /joint_states).
            # Callbacks never block: waiting happens in the worker pool.
            if str(self.config.get('core.executor', 'single')) == 'multi':
                self._executor = MultiThreadedExecutor(num_threads=6, context=self._context)
            else:
                self._executor = SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)
            self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='core-worker')
            self._scheduler = Scheduler()
            self._running = True
            self.bus.start()
            self._spin_thread = threading.Thread(target=self._spin, name='ros-spin', daemon=True)
            self._spin_thread.start()
            self._create_services()
            for svc in self._services.values():
                try:
                    svc.start()
                except Exception as exc:  # noqa: BLE001
                    self.report_error(svc.name, f'failed to start: {exc}', exc)
            self.publish_status()
        return self

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            for svc in reversed(list(self._services.values())):
                try:
                    svc.stop()
                except Exception:  # noqa: BLE001
                    _log.exception('stopping %s failed', svc.name)
            self._running = False
        if self._spin_thread is not None:
            self._spin_thread.join(3.0)
        try:
            self._executor.shutdown(timeout_sec=2.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._node.destroy_node()
        except Exception:  # noqa: BLE001
            pass
        try:
            import rclpy
            rclpy.shutdown(context=self._context)
        except Exception:  # noqa: BLE001
            pass
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._scheduler is not None:
            self._scheduler.stop()
        self.bus.stop()
        self._clients.clear()

    @property
    def running(self) -> bool:
        return self._running

    def _spin(self) -> None:
        while self._running:
            try:
                self._executor.spin_once(timeout_sec=0.1)
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                # Never let an exception stop the executor.
                self.report_error('executor', str(exc), exc)

    # ----------------------------------------------------------------- services registry

    def _create_services(self) -> None:
        from .services import create_default_services
        for svc in create_default_services(self, self._enable):
            self._services[svc.name] = svc

    def add_service(self, svc: BaseService) -> BaseService:
        self._services[svc.name] = svc
        if self._running:
            svc.start()
        return svc

    def service(self, name: str) -> BaseService:
        return self._services[name]

    def has_service(self, name: str) -> bool:
        return name in self._services

    def services(self) -> dict[str, BaseService]:
        return dict(self._services)

    def __getattr__(self, item: str):
        # rt.params, rt.graph, ... are shortcuts to the services.
        services = self.__dict__.get('_services', {})
        if item in services:
            return services[item]
        raise AttributeError(item)

    # ----------------------------------------------------------------- accessors

    @property
    def node(self):
        return self._node

    @property
    def callback_group(self):
        return self._cb_group

    def domain_id(self) -> int:
        try:
            return int(os.environ.get('ROS_DOMAIN_ID', '0') or 0)
        except ValueError:
            return 0

    def now(self) -> float:
        return time.time()

    def ros_now(self) -> float:
        return self._node.get_clock().now().nanoseconds / 1e9

    def publish_status(self) -> None:
        self.bus.publish(CoreStatus(running=self._running, distro=self.compat.distro(),
                                    domain_id=self.domain_id(), node_name=self.node_name,
                                    read_only=self.safety.read_only))

    # ----------------------------------------------------------------- errors

    def report_error(self, source: str, message: str, exc: Optional[BaseException] = None) -> None:
        detail = ''
        if exc is not None:
            detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log.debug('core error from %s: %s', source, message)
        self.bus.publish(CoreError(source=source, message=message, detail=detail))

    def _on_bus_error(self, exc, sub, event) -> None:
        _log.error('event subscriber failed on %s: %s', type(event).__name__, exc)

    def safe(self, fn: Callable, source: str) -> Callable:
        """Wrap a ROS callback: exceptions become CoreError events."""

        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                self.report_error(source, f'{type(exc).__name__}: {exc}', exc)
                return None

        return wrapper

    # ----------------------------------------------------------------- threads and timers

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        """Run ``fn`` in a core worker thread."""
        if self._pool is None:
            raise RuntimeError('runtime not started')
        return self._pool.submit(fn, *args, **kwargs)

    def call_later(self, delay: float, fn: Callable[[], None]) -> list:
        return self._scheduler.call_later(delay, fn)

    def cancel_call(self, handle) -> None:
        if handle is not None:
            Scheduler.cancel(handle)

    def create_timer(self, period_s: float, fn: Callable, source: str = 'timer'):
        timer = self._node.create_timer(period_s, self.safe(fn, source),
                                        callback_group=self._cb_group)
        self._timers.append(timer)
        return timer

    def destroy_timer(self, timer) -> None:
        if timer is None:
            return
        try:
            timer.cancel()
            self._node.destroy_timer(timer)
        except Exception:  # noqa: BLE001
            pass
        if timer in self._timers:
            self._timers.remove(timer)

    def create_subscription(self, msg_type, topic: str, callback: Callable, qos,
                            source: str = 'subscription', raw: bool = False):
        return self._node.create_subscription(msg_type, topic, self.safe(callback, source), qos,
                                              callback_group=self._cb_group, raw=raw)

    def destroy_subscription(self, sub) -> None:
        if sub is None:
            return
        try:
            self._node.destroy_subscription(sub)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- service calls

    def _get_client(self, srv_type, name: str):
        key = (srv_type, name)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._node.create_client(srv_type, name, callback_group=self._cb_group)
                self._clients[key] = client
            return client

    def release_clients(self, prefix: str) -> int:
        """Destroy cached clients whose service name starts with ``prefix``."""
        n = 0
        with self._lock:
            for key in [k for k in self._clients if k[1].startswith(prefix)]:
                client = self._clients.pop(key)
                try:
                    self._node.destroy_client(client)
                except Exception:  # noqa: BLE001
                    pass
                n += 1
        return n

    def service_is_ready(self, srv_type, name: str) -> bool:
        try:
            return self._get_client(srv_type, name).service_is_ready()
        except Exception:  # noqa: BLE001
            return False

    def call_service(self, srv_type, name: str, request, timeout: Optional[float] = None,
                     wait_timeout: Optional[float] = None) -> Future:
        """Call a ROS service without blocking the caller.

        If the service is ready the call is sent immediately; otherwise a worker waits
        for it up to ``wait_timeout``. The result future fails with ``ServiceUnavailable``
        or ``ServiceTimeout``.
        """
        if not self._running:
            fut: Future = Future()
            fut.set_exception(RuntimeError('runtime not running'))
            return fut
        timeout = float(timeout if timeout is not None else
                        self.config.get('timeouts.service_s', 2.0))
        wait_timeout = float(wait_timeout if wait_timeout is not None else
                             self.config.get('timeouts.wait_for_service_s', 1.0))
        out: Future = Future()
        try:
            client = self._get_client(srv_type, name)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out

        def send() -> None:
            try:
                ros_fut = client.call_async(request)
            except Exception as exc:  # noqa: BLE001
                try_set_exception(out, exc)
                return

            def on_timeout() -> None:
                if try_set_exception(out, ServiceTimeout(f'{name}: no response in {timeout:g} s')):
                    try:
                        client.remove_pending_request(ros_fut)
                    except Exception:  # noqa: BLE001
                        pass

            handle = self.call_later(timeout, on_timeout)

            def on_response(f) -> None:
                Scheduler.cancel(handle)
                try:
                    exc = f.exception()
                except Exception as e:  # noqa: BLE001
                    exc = e
                if exc is not None:
                    try_set_exception(out, exc)
                else:
                    res = f.result()
                    if res is None:
                        try_set_exception(out, ServiceUnavailable(f'{name}: call failed'))
                    else:
                        try_set_result(out, res)

            ros_fut.add_done_callback(on_response)

        if client.service_is_ready():
            send()
        else:
            def wait_then_send() -> None:
                try:
                    ok = client.wait_for_service(timeout_sec=wait_timeout)
                except Exception as exc:  # noqa: BLE001
                    try_set_exception(out, exc)
                    return
                if not ok:
                    try_set_exception(out, ServiceUnavailable(f'service {name} not available'))
                    return
                send()

            self.submit(wait_then_send)
        return out

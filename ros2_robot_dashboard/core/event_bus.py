"""Thread-safe publish/subscribe bus for core events.

``publish`` never blocks: events go into a bounded queue per subscriber and a single
dispatcher thread delivers them round-robin. When a subscriber falls behind, the oldest
events in its queue are dropped and counted; the other subscribers are not affected.
Callbacks must be quick (the Qt adapter only emits a queued signal).
"""

from __future__ import annotations

import collections
import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional, Type

_log = logging.getLogger(__name__)


@dataclass(eq=False)
class Subscription:
    event_type: type
    callback: Callable
    max_queue: int
    queue: collections.deque = field(default_factory=collections.deque)
    dropped: int = 0
    active: bool = True


class EventBus:
    def __init__(self, default_max_queue: int = 10000):
        self._default_max_queue = default_max_queue
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._error_handler: Optional[Callable[[BaseException, Subscription, object], None]] = None
        self.synchronous = False   # deliver in the publisher's thread (tests)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._run, name='event-bus', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._running = False
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def set_error_handler(self, handler) -> None:
        self._error_handler = handler

    # ----------------------------------------------------------------- api

    def subscribe(self, event_type: Type, callback: Callable,
                  max_queue: Optional[int] = None) -> Subscription:
        sub = Subscription(event_type, callback, max_queue or self._default_max_queue)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            sub.active = False
            sub.queue.clear()
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, event) -> None:
        if self.synchronous:
            for sub in list(self._subs):
                if sub.active and isinstance(event, sub.event_type):
                    self._deliver(sub, event)
            return
        with self._cond:
            delivered = False
            for sub in self._subs:
                if isinstance(event, sub.event_type):
                    if len(sub.queue) >= sub.max_queue:
                        sub.queue.popleft()
                        sub.dropped += 1
                    sub.queue.append(event)
                    delivered = True
            if delivered:
                self._cond.notify()

    def pending(self) -> int:
        with self._lock:
            return sum(len(s.queue) for s in self._subs)

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until all queues are empty (useful in tests)."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.pending() == 0:
                return True
            time.sleep(0.005)
        return self.pending() == 0

    # ----------------------------------------------------------------- dispatcher

    def _run(self) -> None:
        while True:
            batch = []
            with self._cond:
                while self._running and not any(s.queue for s in self._subs):
                    self._cond.wait(0.5)
                if not self._running:
                    return
                # Round-robin: one event per subscriber per pass keeps delivery fair.
                for sub in self._subs:
                    if sub.queue:
                        batch.append((sub, sub.queue.popleft()))
            for sub, event in batch:
                if sub.active:
                    self._deliver(sub, event)

    def _deliver(self, sub: Subscription, event) -> None:
        try:
            sub.callback(event)
        except Exception as exc:  # noqa: BLE001 - a subscriber must never break the bus
            if self._error_handler is not None:
                try:
                    self._error_handler(exc, sub, event)
                except Exception:  # noqa: BLE001
                    pass
            else:
                _log.error('event subscriber failed: %s\n%s', exc, traceback.format_exc())

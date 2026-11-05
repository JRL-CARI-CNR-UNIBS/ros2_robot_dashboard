"""QtBridge: core callbacks and events -> Qt main thread.

The core calls back from ROS or worker threads. The bridge re-emits everything as
queued Qt signals, so pages only ever run in the UI thread and never touch rclpy.
"""

from __future__ import annotations

import traceback
from concurrent.futures import Future
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, Slot

from ...core.domain.events import CoreError, Event


class Registration:
    __slots__ = ('event_type', 'slot', 'active')

    def __init__(self, event_type, slot):
        self.event_type = event_type
        self.slot = slot
        self.active = True


class QtBridge(QObject):
    event_received = Signal(object)
    _done = Signal(object, object, object, object)     # on_ok, on_err, result, exc
    core_error = Signal(object)                         # CoreError
    read_only_changed = Signal(bool)

    def __init__(self, runtime, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.rt = runtime
        self._regs: list[Registration] = []
        self.event_received.connect(self._dispatch)
        self._done.connect(self._deliver)
        # Called in the bus dispatcher thread: emitting is thread-safe (queued delivery).
        self._sub = runtime.bus.subscribe(Event, self._emit_event, max_queue=50000)
        self._closed = False
        self._remove_ro = runtime.safety.on_read_only_changed(
            lambda ro: self._safe_emit(self.read_only_changed, ro))

    def _safe_emit(self, signal, *args) -> None:
        if self._closed:
            return
        try:
            signal.emit(*args)
        except RuntimeError:          # the QObject was deleted
            self._closed = True

    def _emit_event(self, event) -> None:
        self._safe_emit(self.event_received, event)

    def close(self) -> None:
        self._closed = True
        try:
            self.rt.bus.unsubscribe(self._sub)
        except Exception:  # noqa: BLE001
            pass
        self._remove_ro()
        self._regs.clear()

    # ----------------------------------------------------------------- events

    def on(self, event_type, slot: Callable[[Any], None]) -> Registration:
        reg = Registration(event_type, slot)
        self._regs.append(reg)
        return reg

    def off(self, reg: Registration) -> None:
        reg.active = False
        if reg in self._regs:
            self._regs.remove(reg)

    @Slot(object)
    def _dispatch(self, event) -> None:
        if isinstance(event, CoreError):
            self.core_error.emit(event)
        for reg in list(self._regs):
            if reg.active and isinstance(event, reg.event_type):
                try:
                    reg.slot(event)
                except Exception:  # noqa: BLE001 - a page bug must not stop the others
                    traceback.print_exc()

    # ----------------------------------------------------------------- commands

    def call(self, future: Future, on_ok: Optional[Callable[[Any], None]] = None,
             on_err: Optional[Callable[[BaseException], None]] = None) -> Future:
        """Attach callbacks that run in the UI thread when ``future`` completes."""

        def cb(f: Future) -> None:
            if f.cancelled():
                self._safe_emit(self._done, on_ok, on_err, None, RuntimeError('cancelled'))
                return
            exc = f.exception()
            self._safe_emit(self._done, on_ok, on_err, None if exc else f.result(), exc)

        future.add_done_callback(cb)
        return future

    def run(self, fn: Callable, *args, on_ok=None, on_err=None, **kwargs) -> Future:
        """Run ``fn`` in a core worker thread; callbacks in the UI thread."""
        return self.call(self.rt.submit(fn, *args, **kwargs), on_ok, on_err)

    @Slot(object, object, object, object)
    def _deliver(self, on_ok, on_err, result, exc) -> None:
        try:
            if exc is not None:
                if on_err is not None:
                    on_err(exc)
            elif on_ok is not None:
                on_ok(result)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

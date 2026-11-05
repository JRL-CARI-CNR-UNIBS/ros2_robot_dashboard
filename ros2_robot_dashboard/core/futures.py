"""Helpers around ``concurrent.futures.Future`` used by every core command."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Callable, Iterable, Optional


class ServiceTimeout(TimeoutError):
    pass


class ServiceUnavailable(RuntimeError):
    pass


def completed(value: Any = None) -> Future:
    f: Future = Future()
    f.set_result(value)
    return f


def failed(exc: BaseException) -> Future:
    f: Future = Future()
    f.set_exception(exc)
    return f


def try_set_result(fut: Future, value: Any) -> bool:
    if fut.done():
        return False
    try:
        fut.set_result(value)
        return True
    except Exception:  # noqa: BLE001 - InvalidStateError on races
        return False


def try_set_exception(fut: Future, exc: BaseException) -> bool:
    if fut.done():
        return False
    try:
        fut.set_exception(exc)
        return True
    except Exception:  # noqa: BLE001
        return False


def then(fut: Future, fn: Callable[[Any], Any]) -> Future:
    """Return a future with ``fn(result)``; exceptions propagate."""
    out: Future = Future()

    def _done(f: Future) -> None:
        if f.cancelled():
            out.cancel()
            return
        exc = f.exception()
        if exc is not None:
            try_set_exception(out, exc)
            return
        try:
            value = fn(f.result())
        except BaseException as e:  # noqa: BLE001
            try_set_exception(out, e)
            return
        if isinstance(value, Future):
            value.add_done_callback(lambda v: _forward(v, out))
        else:
            try_set_result(out, value)

    fut.add_done_callback(_done)
    return out


def _forward(src: Future, dst: Future) -> None:
    exc = src.exception()
    if exc is not None:
        try_set_exception(dst, exc)
    else:
        try_set_result(dst, src.result())


def gather(futures: Iterable[Future]) -> Future:
    """Future of the list of results; fails with the first exception."""
    futs = list(futures)
    out: Future = Future()
    if not futs:
        out.set_result([])
        return out
    results: list[Any] = [None] * len(futs)
    remaining = [len(futs)]
    lock = threading.Lock()

    def make_cb(i: int):
        def _cb(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                try_set_exception(out, exc)
                return
            with lock:
                results[i] = f.result()
                remaining[0] -= 1
                done = remaining[0] == 0
            if done:
                try_set_result(out, results)
        return _cb

    for i, f in enumerate(futs):
        f.add_done_callback(make_cb(i))
    return out


def on_done(fut: Future, on_ok: Optional[Callable[[Any], None]] = None,
            on_err: Optional[Callable[[BaseException], None]] = None) -> Future:
    """Attach callbacks (called in the thread that completes the future)."""

    def _cb(f: Future) -> None:
        if f.cancelled():
            if on_err:
                on_err(RuntimeError('cancelled'))
            return
        exc = f.exception()
        if exc is not None:
            if on_err:
                on_err(exc)
        elif on_ok:
            on_ok(f.result())

    fut.add_done_callback(_cb)
    return fut

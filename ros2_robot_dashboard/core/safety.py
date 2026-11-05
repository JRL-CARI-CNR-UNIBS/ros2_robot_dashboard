"""Read-only mode and the local log of write actions (design v2, section 11)."""

from __future__ import annotations

import fnmatch
import getpass
import json
import os
import socket
import threading
import time
from typing import Callable, Iterable, Optional


class ReadOnlyError(PermissionError):
    """Raised when a write action is attempted in read-only mode."""


class Safety:
    def __init__(self, read_only: bool = False, log_path: Optional[str] = None,
                 risky_words: Iterable[str] = (), critical_params: Iterable[str] = ()):
        self._read_only = read_only
        self._log_path = log_path
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._ro_listeners: list[Callable[[bool], None]] = []
        self.risky_words = tuple(w.lower() for w in risky_words)
        self.critical_params = tuple(critical_params)
        self._user = _safe(getpass.getuser)
        self._host = _safe(socket.gethostname)

    # ----------------------------------------------------------------- read-only

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, value: bool) -> None:
        value = bool(value)
        if value == self._read_only:
            return
        self._read_only = value
        self.log('read_only', 'dashboard', True, 'on' if value else 'off')
        for cb in list(self._ro_listeners):
            cb(value)

    def on_read_only_changed(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Register a listener; returns a function that removes it."""
        self._ro_listeners.append(cb)
        return lambda: self.remove_read_only_listener(cb)

    def remove_read_only_listener(self, cb: Callable[[bool], None]) -> None:
        if cb in self._ro_listeners:
            self._ro_listeners.remove(cb)

    def check_write(self, action: str, target: str = '') -> None:
        if self._read_only:
            self.log(action, target, False, 'blocked: read-only mode')
            raise ReadOnlyError(f'read-only mode: {action} {target}'.strip())

    # ----------------------------------------------------------------- classification

    def is_risky_service(self, name: str) -> bool:
        base = name.rsplit('/', 1)[-1].lower()
        return any(w in base for w in self.risky_words)

    def is_critical_param(self, node: str, param: str) -> bool:
        key = f'{node}:{param}'
        return any(fnmatch.fnmatchcase(key, pat) for pat in self.critical_params)

    # ----------------------------------------------------------------- action log

    def on_action(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def log(self, action: str, target: str, ok: bool, detail: str = '') -> dict:
        entry = {
            'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'user': self._user,
            'host': self._host,
            'action': action,
            'target': target,
            'ok': bool(ok),
            'detail': detail,
        }
        if self._log_path:
            with self._lock:
                try:
                    os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
                    with open(self._log_path, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(entry) + '\n')
                except OSError:
                    pass
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:  # noqa: BLE001
                pass
        return entry


def _safe(fn) -> str:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return 'unknown'

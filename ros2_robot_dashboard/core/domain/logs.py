"""Log records (/rosout), diagnostics and lifecycle types."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .base import Serializable


class LogLevel(enum.IntEnum):
    """Values of ``rcl_interfaces/msg/Log``."""

    DEBUG = 10
    INFO = 20
    WARN = 30
    ERROR = 40
    FATAL = 50

    @classmethod
    def from_value(cls, value: int) -> 'LogLevel':
        try:
            return cls(int(value))
        except ValueError:
            # Unknown values are mapped to the closest lower level.
            for lvl in sorted(cls, reverse=True):
                if value >= lvl:
                    return lvl
            return cls.DEBUG


@dataclass(frozen=True)
class LogRecord(Serializable):
    seq: int                     # increasing id assigned by the dashboard
    stamp: float                 # message time (s)
    level: LogLevel
    node: str
    message: str
    file: str = ''
    function: str = ''
    line: int = 0

    @property
    def location(self) -> str:
        return f'{self.file}:{self.line}' if self.file else ''


class DiagLevel(enum.IntEnum):
    OK = 0
    WARN = 1
    ERROR = 2
    STALE = 3

    @classmethod
    def from_value(cls, value) -> 'DiagLevel':
        if isinstance(value, (bytes, bytearray)):
            value = value[0] if value else 0
        try:
            return cls(int(value))
        except ValueError:
            return cls.ERROR


@dataclass(frozen=True)
class DiagnosticStatusInfo(Serializable):
    name: str
    level: DiagLevel
    message: str
    hardware_id: str
    values: tuple[tuple[str, str], ...] = ()
    last_update: float = 0.0
    previous_level: DiagLevel | None = None
    changed_at: float = 0.0


@dataclass(frozen=True)
class LifecycleTransition(Serializable):
    id: int
    label: str
    start_state: str
    goal_state: str


@dataclass(frozen=True)
class LifecycleNodeInfo(Serializable):
    node: str
    state_id: int
    state_label: str
    transitions: tuple[LifecycleTransition, ...] = ()
    error: str = ''

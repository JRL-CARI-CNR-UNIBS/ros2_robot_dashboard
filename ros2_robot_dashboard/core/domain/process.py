"""Launch profiles, declared arguments and process states."""

from __future__ import annotations

import enum
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import Serializable


class ProcessState(enum.Enum):
    STOPPED = 'stopped'
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'
    EXITED = 'exited'
    FAILED = 'failed'

    @property
    def is_active(self) -> bool:
        return self in (ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING)


class ArgType(enum.Enum):
    BOOL = 'bool'
    STRING = 'string'
    INT = 'int'
    FLOAT = 'float'
    CHOICE = 'choice'


@dataclass(frozen=True)
class LaunchArg(Serializable):
    name: str
    type: ArgType = ArgType.STRING
    default: Any = None
    description: str = ''
    choices: tuple[str, ...] = ()
    enabled_if: str = ''           # expression over other args, e.g. "not sim"
    required: bool = False
    # Not passed on the command line unless the user enables it: the launch file
    # then uses its own default.
    use_default: bool = False

    def format_value(self, value: Any) -> str:
        if self.type == ArgType.BOOL:
            return 'true' if _as_bool(value) else 'false'
        return '' if value is None else str(value)

    def parse(self, value: Any) -> Any:
        if self.type == ArgType.BOOL:
            return _as_bool(value)
        if self.type == ArgType.INT:
            return int(value)
        if self.type == ArgType.FLOAT:
            return float(value)
        if self.type == ArgType.CHOICE:
            v = str(value)
            if self.choices and v not in self.choices:
                raise ValueError(f"'{v}' is not one of {list(self.choices)}")
            return v
        return '' if value is None else str(value)


@dataclass(frozen=True)
class LaunchPreset(Serializable):
    name: str
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StopPolicy(Serializable):
    sigint_timeout_s: float = 10.0
    sigterm_timeout_s: float = 5.0


@dataclass(frozen=True)
class ProcessProfile(Serializable):
    id: str
    name: str
    command: str
    description: str = ''
    cwd: str = ''
    env: dict = field(default_factory=dict)
    source: str = ''
    args: tuple[LaunchArg, ...] = ()
    presets: tuple[LaunchPreset, ...] = ()
    stop: StopPolicy = field(default_factory=StopPolicy)
    origin: str = ''               # file the profile was loaded from

    def arg(self, name: str) -> Optional[LaunchArg]:
        for a in self.args:
            if a.name == name:
                return a
        return None

    def default_values(self) -> dict:
        return {a.name: a.default for a in self.args}


@dataclass(frozen=True)
class ScenarioStep(Serializable):
    profile: str
    preset: str = ''
    wait_node: str = ''
    wait_service: str = ''
    wait_s: float = 0.0
    timeout_s: float = 30.0


@dataclass(frozen=True)
class Scenario(Serializable):
    id: str
    name: str
    steps: tuple[ScenarioStep, ...] = ()


@dataclass(frozen=True)
class ProcessStatus(Serializable):
    process_id: str
    state: ProcessState
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    command: tuple[str, ...] = ()
    started_at: float = 0.0
    leftover_pids: tuple[int, ...] = ()
    message: str = ''


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


def command_args(command: str) -> dict[str, str]:
    """``name:=value`` pairs already written in a command line."""
    out = {}
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for t in tokens:
        name, sep, value = t.partition(':=')
        if sep and name and not name.startswith('-'):
            out[name] = value
    return out


def compose_launch_command(profile: ProcessProfile, values: dict) -> list[str]:
    """Return argv: ``command`` split with shlex plus ``name:=value`` for passed args.

    ``values`` overrides the profile defaults; a value of ``None`` means "not passed"
    (the launch file uses its own default). Arguments with ``use_default`` are passed
    only when ``values`` gives them a value. Arguments already written in the command
    (``name:=value``) are not passed again: ``ros2 launch`` would use the last one.
    An empty value is not passed for arguments without a default.
    """
    from ..processes.launch_args import evaluate_condition

    argv = shlex.split(profile.command)
    values = values or {}
    # Values seen by enabled_if conditions: what the launch file will use.
    shown = profile.default_values()
    shown.update({k: v for k, v in values.items() if v is not None})
    fixed = command_args(profile.command)
    shown.update(fixed)
    merged = {a.name: a.default for a in profile.args if not a.use_default}
    merged.update(values)
    for a in profile.args:
        if a.name in fixed:
            continue
        if a.enabled_if and not evaluate_condition(a.enabled_if, shown, profile):
            continue
        v = merged.get(a.name)
        if v is None or (v == '' and (a.type != ArgType.STRING or a.default is None)):
            continue
        argv.append(f'{a.name}:={a.format_value(v)}')
    return argv

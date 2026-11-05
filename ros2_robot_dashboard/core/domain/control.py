"""ros2_control types and the pre-switch conflict check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .base import Serializable


@dataclass(frozen=True)
class ControllerInfo(Serializable):
    name: str
    type: str
    state: str                                   # active | inactive | unconfigured | finalized
    claimed_interfaces: tuple[str, ...] = ()
    required_command_interfaces: tuple[str, ...] = ()
    required_state_interfaces: tuple[str, ...] = ()
    update_rate: Optional[int] = None            # None when the distro does not report it
    is_chainable: bool = False
    is_chained: bool = False


@dataclass(frozen=True)
class HwInterfaceInfo(Serializable):
    name: str
    kind: str                                    # command | state
    is_available: bool
    is_claimed: bool
    data_type: str = ''
    claimed_by: str = ''


@dataclass(frozen=True)
class HwComponentInfo(Serializable):
    name: str
    type: str                                    # actuator | sensor | system
    plugin: str
    state_id: int
    state_label: str
    command_interfaces: tuple[HwInterfaceInfo, ...] = ()
    state_interfaces: tuple[HwInterfaceInfo, ...] = ()


@dataclass(frozen=True)
class SwitchConflict(Serializable):
    interface: str
    wanted_by: str
    held_by: str
    message: str


@dataclass(frozen=True)
class SwitchPlan(Serializable):
    """What a switch would do: shown in the confirmation dialog."""

    activate: tuple[str, ...]
    deactivate: tuple[str, ...]
    conflicts: tuple[SwitchConflict, ...]
    released: tuple[tuple[str, str], ...]        # (interface, previous owner)
    acquired: tuple[tuple[str, str], ...]        # (interface, new owner)
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts


# Lifecycle states of hardware components (lifecycle_msgs/State ids).
HW_STATES = {
    'unconfigured': 1,
    'inactive': 2,
    'active': 3,
    'finalized': 4,
}


def plan_switch(controllers: Iterable[ControllerInfo], activate: Iterable[str],
                deactivate: Iterable[str]) -> SwitchPlan:
    """Compute interface ownership changes and conflicts before calling switch_controller."""
    by_name = {c.name: c for c in controllers}
    act = tuple(dict.fromkeys(activate))
    deact = tuple(dict.fromkeys(deactivate))
    warnings = []
    owner: dict[str, str] = {}
    for c in by_name.values():
        if c.state == 'active':
            for itf in c.claimed_interfaces:
                owner[itf] = c.name
    released = []
    for name in deact:
        c = by_name.get(name)
        if c is None:
            warnings.append(f"unknown controller '{name}'")
            continue
        if c.state != 'active':
            warnings.append(f"'{name}' is not active ({c.state})")
        for itf, who in list(owner.items()):
            if who == name:
                released.append((itf, name))
                del owner[itf]
    conflicts = []
    acquired = []
    for name in act:
        c = by_name.get(name)
        if c is None:
            warnings.append(f"unknown controller '{name}'")
            continue
        if c.state == 'active':
            warnings.append(f"'{name}' is already active")
            continue
        if c.state not in ('inactive',):
            warnings.append(f"'{name}' is {c.state}: it must be configured (inactive) first")
        for itf in c.required_command_interfaces:
            holder = owner.get(itf)
            if holder is not None and holder != name:
                conflicts.append(SwitchConflict(
                    itf, name, holder,
                    f"'{name}' needs '{itf}', held by '{holder}' which is not being deactivated"))
            else:
                owner[itf] = name
                acquired.append((itf, name))
    return SwitchPlan(act, deact, tuple(conflicts), tuple(released), tuple(acquired),
                      tuple(warnings))


def interface_owners(controllers: Iterable[ControllerInfo]) -> dict[str, str]:
    owners = {}
    for c in controllers:
        if c.state == 'active':
            for itf in c.claimed_interfaces:
                owners[itf] = c.name
    return owners

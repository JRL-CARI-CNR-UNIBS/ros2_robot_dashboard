"""Robot state types and a small URDF parser (joints, limits, kinematic tree)."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .base import Serializable

ROTATIONAL = ('revolute', 'continuous')


@dataclass(frozen=True)
class JointLimits(Serializable):
    lower: Optional[float] = None
    upper: Optional[float] = None
    velocity: Optional[float] = None
    effort: Optional[float] = None


@dataclass(frozen=True)
class JointInfo(Serializable):
    name: str
    type: str                       # revolute | continuous | prismatic | fixed | ...
    parent: str
    child: str
    limits: JointLimits = field(default_factory=JointLimits)

    @property
    def is_rotational(self) -> bool:
        return self.type in ROTATIONAL

    @property
    def is_movable(self) -> bool:
        return self.type not in ('fixed',)


@dataclass(frozen=True)
class JointState(Serializable):
    name: str
    position: Optional[float] = None
    velocity: Optional[float] = None
    effort: Optional[float] = None


@dataclass(frozen=True)
class RobotModel(Serializable):
    name: str
    joints: tuple[JointInfo, ...] = ()
    links: tuple[str, ...] = ()
    source: str = ''                 # where the description came from

    def joint(self, name: str) -> Optional[JointInfo]:
        for j in self.joints:
            if j.name == name:
                return j
        return None

    def root_links(self) -> list[str]:
        children = {j.child for j in self.joints}
        return [ln for ln in self.links if ln not in children]

    def tip_links(self) -> list[str]:
        parents = {j.parent for j in self.joints}
        return [ln for ln in self.links if ln not in parents]


def parse_urdf(xml_text: str, source: str = '') -> RobotModel:
    """Parse joints, limits and links from a URDF string. Raises ``ValueError``."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f'invalid URDF: {exc}') from None
    if root.tag != 'robot':
        raise ValueError(f"invalid URDF: root element is '{root.tag}', expected 'robot'")
    links = tuple(ln.get('name', '') for ln in root.findall('link'))
    joints = []
    for j in root.findall('joint'):
        parent = j.find('parent')
        child = j.find('child')
        lim = j.find('limit')
        limits = JointLimits()
        if lim is not None:
            limits = JointLimits(_f(lim.get('lower')), _f(lim.get('upper')),
                                 _f(lim.get('velocity')), _f(lim.get('effort')))
        joints.append(JointInfo(
            name=j.get('name', ''),
            type=j.get('type', ''),
            parent=parent.get('link', '') if parent is not None else '',
            child=child.get('link', '') if child is not None else '',
            limits=limits,
        ))
    return RobotModel(root.get('name', ''), tuple(joints), links, source)


def _f(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def limit_ratio(joint: JointInfo, position: Optional[float]) -> Optional[float]:
    """Position relative to the limits in [0, 1]; None if no finite limits."""
    lo, hi = joint.limits.lower, joint.limits.upper
    if position is None or lo is None or hi is None or joint.type == 'continuous':
        return None
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return None
    return (position - lo) / (hi - lo)


def near_limit(joint: JointInfo, position: Optional[float], margin: float = 0.05) -> bool:
    r = limit_ratio(joint, position)
    return r is not None and (r <= margin or r >= 1.0 - margin)

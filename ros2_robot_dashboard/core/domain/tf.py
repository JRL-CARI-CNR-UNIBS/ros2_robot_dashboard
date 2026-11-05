"""TF types and rotation math (quaternion, RPY, 4x4 matrix)."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field

from .base import Serializable


class RotationFormat(enum.Enum):
    QUATERNION = 'quaternion'
    RPY_RAD = 'rpy_rad'
    RPY_DEG = 'rpy_deg'
    MATRIX = 'se3'


@dataclass(frozen=True)
class FrameInfo(Serializable):
    name: str
    parent: str                 # '' for a root
    is_static: bool
    rate_hz: float              # 0 for static frames
    last_update: float          # wall time of the last received transform
    parents_seen: tuple[str, ...] = ()   # more than one = error


@dataclass(frozen=True)
class TfIssue(Serializable):
    kind: str                   # multiple_roots | multiple_parents | loop | stale
    frames: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class TfTreeSnapshot(Serializable):
    frames: tuple[FrameInfo, ...] = ()
    issues: tuple[TfIssue, ...] = ()

    def names(self) -> list[str]:
        return sorted(f.name for f in self.frames)

    def roots(self) -> list[str]:
        return sorted(f.name for f in self.frames if not f.parent)

    def children(self, parent: str) -> list[FrameInfo]:
        return sorted((f for f in self.frames if f.parent == parent), key=lambda f: f.name)


@dataclass(frozen=True)
class TransformSample(Serializable):
    target: str
    source: str
    stamp: float                         # transform time (s)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)   # x, y, z, w
    error: str = ''

    @property
    def ok(self) -> bool:
        return not self.error

    def rpy(self) -> tuple[float, float, float]:
        return quaternion_to_rpy(*self.rotation)

    def matrix(self) -> list[list[float]]:
        return transform_matrix(self.translation, self.rotation)


# --------------------------------------------------------------------------- math

def quaternion_to_rpy(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    """Roll, pitch, yaw (rad), fixed axes XYZ (same convention as tf2 getRPY)."""
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    sinr = 2.0 * (w * x + y * z)
    cosr = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)
    sinp = 2.0 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)
    siny = 2.0 * (w * z + x * y)
    cosy = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny, cosy)
    return roll, pitch, yaw


def rpy_to_quaternion(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


def rotation_matrix(x: float, y: float, z: float, w: float) -> list[list[float]]:
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def transform_matrix(translation, rotation) -> list[list[float]]:
    r = rotation_matrix(*rotation)
    t = translation
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0.0, 0.0, 0.0, 1.0]]


def format_rotation(sample: TransformSample, fmt: RotationFormat, precision: int = 4) -> str:
    p = precision
    if fmt == RotationFormat.QUATERNION:
        x, y, z, w = sample.rotation
        return f'x {x:.{p}f}  y {y:.{p}f}  z {z:.{p}f}  w {w:.{p}f}'
    if fmt in (RotationFormat.RPY_RAD, RotationFormat.RPY_DEG):
        r, pi, y = sample.rpy()
        if fmt == RotationFormat.RPY_DEG:
            r, pi, y = (math.degrees(v) for v in (r, pi, y))
            unit = '°'
        else:
            unit = ' rad'
        return f'roll {r:.{p}f}{unit}  pitch {pi:.{p}f}{unit}  yaw {y:.{p}f}{unit}'
    m = sample.matrix()
    return '\n'.join('  '.join(f'{v: .{p}f}' for v in row) for row in m)


# --------------------------------------------------------------------------- tree analysis

@dataclass
class FrameRecord:
    """Mutable per-frame bookkeeping used by the TF service."""

    name: str
    parent: str
    is_static: bool
    stamps: list = field(default_factory=list)      # wall-clock receive times (window)
    last_update: float = 0.0
    parents_seen: set = field(default_factory=set)


def analyze_tree(records: dict[str, FrameRecord], now: float, window_s: float = 2.0,
                 stale_s: float = 5.0) -> TfTreeSnapshot:
    """Build an immutable snapshot and detect roots, loops, multiple parents and stale data."""
    frames: dict[str, FrameInfo] = {}
    for name, rec in records.items():
        recent = [t for t in rec.stamps if now - t <= window_s]
        rate = 0.0 if rec.is_static or len(recent) < 2 else (len(recent) - 1) / max(
            recent[-1] - recent[0], 1e-6)
        frames[name] = FrameInfo(name, rec.parent, rec.is_static, rate, rec.last_update,
                                 tuple(sorted(rec.parents_seen)))
    # Parents that never appear as children are roots: add them as frames.
    for rec in list(records.values()):
        if rec.parent and rec.parent not in frames:
            frames[rec.parent] = FrameInfo(rec.parent, '', True, 0.0, 0.0)
    issues: list[TfIssue] = []
    roots = sorted(n for n, f in frames.items() if not f.parent)
    if len(roots) > 1:
        issues.append(TfIssue('multiple_roots', tuple(roots),
                              f'{len(roots)} separate trees: ' + ', '.join(roots)))
    for f in frames.values():
        if len(f.parents_seen) > 1:
            issues.append(TfIssue('multiple_parents', (f.name,) + f.parents_seen,
                                  f"frame '{f.name}' published with parents "
                                  + ', '.join(f.parents_seen)))
        if not f.is_static and f.last_update and now - f.last_update > stale_s:
            issues.append(TfIssue('stale', (f.name,),
                                  f"frame '{f.name}' not updated for "
                                  f'{now - f.last_update:.1f} s'))
    # Loop detection: follow parents.
    reported: set[str] = set()
    for start in frames:
        seen = []
        cur = start
        while cur and cur in frames and cur not in seen:
            seen.append(cur)
            cur = frames[cur].parent
        if cur and cur in seen:
            loop = tuple(seen[seen.index(cur):])
            key = frozenset(loop)
            if not key & reported:
                reported |= set(loop)
                issues.append(TfIssue('loop', loop, 'loop: ' + ' -> '.join(loop + (cur,))))
    return TfTreeSnapshot(tuple(sorted(frames.values(), key=lambda f: f.name)), tuple(issues))

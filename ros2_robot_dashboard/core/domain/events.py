"""Events published on the core EventBus.

Events are immutable dataclasses. Every event is JSON-serializable through
``to_dict()`` so a future web adapter can forward it as
``{"type": "event.<ClassName>", "payload": {...}}``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import Serializable
from .bag import RecordingStatus
from .control import ControllerInfo, HwComponentInfo, HwInterfaceInfo
from .graph import GraphSnapshot, NodeInfo
from .health import HostMetrics, TopicHealth, TopicStatsInfo
from .logs import DiagnosticStatusInfo, LifecycleNodeInfo, LogRecord
from .params import NodeStatus, ParamInfo
from .process import ProcessStatus
from .robot import JointState, RobotModel
from .tf import TfTreeSnapshot, TransformSample


@dataclass(frozen=True)
class Event(Serializable):
    stamp: float = field(default_factory=time.time, kw_only=True)

    @property
    def type_name(self) -> str:
        return 'event.' + type(self).__name__

    def envelope(self) -> dict:
        return {'type': self.type_name, 'payload': self.to_dict()}


# --------------------------------------------------------------------------- core

@dataclass(frozen=True)
class CoreError(Event):
    source: str
    message: str
    detail: str = ''


@dataclass(frozen=True)
class CoreStatus(Event):
    running: bool
    distro: str
    domain_id: int
    node_name: str
    read_only: bool


@dataclass(frozen=True)
class ActionLogged(Event):
    action: str
    target: str
    ok: bool
    detail: str = ''


# --------------------------------------------------------------------------- graph and params

@dataclass(frozen=True)
class NodesChanged(Event):
    nodes: tuple[NodeInfo, ...]
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphChanged(Event):
    graph: GraphSnapshot


@dataclass(frozen=True)
class ParamEvent(Event):
    node: str
    param: ParamInfo
    kind: str                    # new | changed | deleted


@dataclass(frozen=True)
class ParamNodeStatus(Event):
    node: str
    status: NodeStatus
    message: str = ''


# --------------------------------------------------------------------------- topics

@dataclass(frozen=True)
class TopicSample(Event):
    echo_id: str
    topic: str
    text: str
    receive_time: float
    header_stamp: Optional[float] = None
    dropped: int = 0             # messages skipped by sampling since the previous sample


@dataclass(frozen=True)
class EchoStopped(Event):
    echo_id: str
    topic: str
    reason: str = ''


@dataclass(frozen=True)
class EchoFileStatus(Event):
    echo_id: str
    path: str
    written: int
    dropped: int
    active: bool
    message: str = ''


@dataclass(frozen=True)
class TopicStats(Event):
    stats: TopicStatsInfo


@dataclass(frozen=True)
class PlotSample(Event):
    source_id: str
    topic: str
    path: str
    t: float
    value: float


# --------------------------------------------------------------------------- tf and robot

@dataclass(frozen=True)
class TfTreeChanged(Event):
    tree: TfTreeSnapshot


@dataclass(frozen=True)
class TransformSampleEvent(Event):
    echo_id: str
    sample: TransformSample


@dataclass(frozen=True)
class JointStatesUpdated(Event):
    topic: str
    joints: tuple[JointState, ...]


@dataclass(frozen=True)
class RobotModelChanged(Event):
    model: Optional[RobotModel]
    error: str = ''


# --------------------------------------------------------------------------- control

@dataclass(frozen=True)
class ControllerManagersChanged(Event):
    managers: tuple[str, ...]


@dataclass(frozen=True)
class ControllerStateChanged(Event):
    manager: str
    controllers: tuple[ControllerInfo, ...]
    interfaces: tuple[HwInterfaceInfo, ...] = ()
    components: tuple[HwComponentInfo, ...] = ()
    error: str = ''


@dataclass(frozen=True)
class LifecycleNodesChanged(Event):
    nodes: tuple[LifecycleNodeInfo, ...]


# --------------------------------------------------------------------------- logs and diagnostics

@dataclass(frozen=True)
class LogRecordsBatch(Event):
    records: tuple[LogRecord, ...]


@dataclass(frozen=True)
class DiagnosticsUpdated(Event):
    items: tuple[DiagnosticStatusInfo, ...]


# --------------------------------------------------------------------------- processes and bags

@dataclass(frozen=True)
class ProcessOutput(Event):
    process_id: str
    lines: tuple[tuple[str, str], ...]     # (stream, line)


@dataclass(frozen=True)
class ProcessStateChanged(Event):
    status: ProcessStatus


@dataclass(frozen=True)
class RecordingStatusChanged(Event):
    status: RecordingStatus


@dataclass(frozen=True)
class ProfilesChanged(Event):
    profiles: tuple[Any, ...]
    scenarios: tuple[Any, ...] = ()
    errors: tuple[str, ...] = ()


# --------------------------------------------------------------------------- health

@dataclass(frozen=True)
class HealthUpdated(Event):
    profile_id: str
    topics: tuple[TopicHealth, ...]


@dataclass(frozen=True)
class HealthAlert(Event):
    topic: str
    reason: str
    level: str = 'error'


@dataclass(frozen=True)
class HostMetricsUpdated(Event):
    hosts: tuple[HostMetrics, ...]


@dataclass(frozen=True)
class DiscoveryIssues(Event):
    unreachable_nodes: tuple[str, ...]
    message: str = ''

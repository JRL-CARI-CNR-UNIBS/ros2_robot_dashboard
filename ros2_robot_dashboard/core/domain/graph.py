"""ROS graph types: nodes, topics, endpoints, QoS, services, actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base import Serializable


@dataclass(frozen=True)
class QosInfo(Serializable):
    reliability: str = 'unknown'      # reliable | best_effort | unknown
    durability: str = 'unknown'       # volatile | transient_local | unknown
    history: str = 'unknown'          # keep_last | keep_all | unknown
    depth: int = 0
    deadline_s: Optional[float] = None    # None = infinite / default
    lifespan_s: Optional[float] = None
    liveliness: str = 'unknown'       # automatic | manual_by_topic | unknown
    liveliness_lease_s: Optional[float] = None

    def short(self) -> str:
        rel = {'reliable': 'R', 'best_effort': 'BE'}.get(self.reliability, '?')
        dur = {'volatile': 'V', 'transient_local': 'TL'}.get(self.durability, '?')
        hist = f'KL{self.depth}' if self.history == 'keep_last' else (
            'KA' if self.history == 'keep_all' else '?')
        return f'{rel}/{dur}/{hist}'


@dataclass(frozen=True)
class EndpointInfo(Serializable):
    node_name: str
    node_namespace: str
    topic_type: str
    endpoint_type: str                 # publisher | subscription
    gid: str = ''
    qos: QosInfo = field(default_factory=QosInfo)

    @property
    def node_fqn(self) -> str:
        return join_name(self.node_namespace, self.node_name)


@dataclass(frozen=True)
class NodeInfo(Serializable):
    name: str
    namespace: str

    @property
    def fqn(self) -> str:
        return join_name(self.namespace, self.name)


@dataclass(frozen=True)
class TopicInfo(Serializable):
    name: str
    types: tuple[str, ...]
    publishers: tuple[EndpointInfo, ...] = ()
    subscribers: tuple[EndpointInfo, ...] = ()

    @property
    def type(self) -> str:
        return self.types[0] if self.types else ''


@dataclass(frozen=True)
class ServiceInfo(Serializable):
    name: str
    types: tuple[str, ...]
    servers: tuple[str, ...] = ()      # node FQNs offering it (when known)

    @property
    def type(self) -> str:
        return self.types[0] if self.types else ''


@dataclass(frozen=True)
class ActionInfo(Serializable):
    name: str
    type: str
    servers: tuple[str, ...] = ()
    clients: tuple[str, ...] = ()


@dataclass(frozen=True)
class QosIssue(Serializable):
    topic: str
    publisher: str
    subscriber: str
    reason: str


@dataclass(frozen=True)
class GraphSnapshot(Serializable):
    nodes: tuple[NodeInfo, ...] = ()
    topics: tuple[TopicInfo, ...] = ()
    services: tuple[ServiceInfo, ...] = ()
    actions: tuple[ActionInfo, ...] = ()

    def topic(self, name: str) -> Optional[TopicInfo]:
        for t in self.topics:
            if t.name == name:
                return t
        return None

    def topology_key(self) -> tuple:
        """Hashable description of the connections (used to keep the graph layout stable)."""
        edges = []
        for t in self.topics:
            for p in t.publishers:
                edges.append((p.node_fqn, t.name))
            for s in t.subscribers:
                edges.append((t.name, s.node_fqn))
        return (tuple(sorted(n.fqn for n in self.nodes)), tuple(sorted(set(edges))))


def join_name(namespace: str, name: str) -> str:
    ns = namespace or '/'
    if not ns.startswith('/'):
        ns = '/' + ns
    if ns.endswith('/'):
        return ns + name
    return ns + '/' + name


def split_name(fqn: str) -> tuple[str, str]:
    """Split ``/ns/sub/name`` into (``/ns/sub``, ``name``)."""
    fqn = fqn if fqn.startswith('/') else '/' + fqn
    ns, _, name = fqn.rpartition('/')
    return (ns or '/'), name


def qos_issues(topic: TopicInfo) -> list[QosIssue]:
    """Detect common QoS incompatibilities between publishers and subscribers."""
    issues = []
    for p in topic.publishers:
        for s in topic.subscribers:
            reasons = []
            if p.qos.reliability == 'best_effort' and s.qos.reliability == 'reliable':
                reasons.append('publisher best_effort, subscriber reliable')
            if p.qos.durability == 'volatile' and s.qos.durability == 'transient_local':
                reasons.append('publisher volatile, subscriber transient_local')
            if (p.qos.deadline_s is not None and s.qos.deadline_s is not None
                    and p.qos.deadline_s > s.qos.deadline_s):
                reasons.append('publisher deadline longer than subscriber deadline')
            if p.qos.liveliness == 'automatic' and s.qos.liveliness == 'manual_by_topic':
                reasons.append('liveliness automatic vs manual_by_topic')
            if reasons:
                issues.append(QosIssue(topic.name, p.node_fqn, s.node_fqn, '; '.join(reasons)))
    return issues

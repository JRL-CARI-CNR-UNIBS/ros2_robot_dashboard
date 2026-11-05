"""GraphService: nodes, topics, services, actions with endpoints and QoS.

The graph is polled periodically from the local DDS cache (cheap); events are
published only when something changes.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

from ..domain.events import GraphChanged, NodesChanged
from ..domain.graph import (ActionInfo, EndpointInfo, GraphSnapshot, NodeInfo, ServiceInfo,
                            TopicInfo, join_name)
from ..runtime import BaseService

_ACTION_SUFFIX = '/_action/send_goal'


def is_hidden_name(name: str) -> bool:
    """ROS convention: a name component starting with '_' is hidden."""
    return any(part.startswith('_') for part in name.split('/') if part)


class GraphService(BaseService):
    name = 'graph'

    def __init__(self, rt):
        super().__init__(rt)
        self._timer = None
        self._snapshot = GraphSnapshot()
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._hidden_re: Optional[re.Pattern] = None

    def start(self) -> None:
        pattern = self.config.get('graph.hidden_node_regex', '')
        self._hidden_re = re.compile(pattern) if pattern else None
        period = float(self.config.get('polling.graph_s', 1.5))
        self._timer = self.rt.create_timer(period, self.poll, 'graph.poll')
        self.rt.submit(self.poll)

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)

    # ----------------------------------------------------------------- api

    def snapshot(self) -> GraphSnapshot:
        with self._lock:
            return self._snapshot

    def refresh(self):
        return self.rt.submit(self.poll)

    def is_hidden_node(self, fqn: str) -> bool:
        if self._hidden_re is not None and self._hidden_re.search(fqn):
            return True
        return is_hidden_name(fqn)

    def topic_endpoints(self, topic: str) -> tuple[list[EndpointInfo], list[EndpointInfo]]:
        node = self.rt.node
        return (self.rt.compat.publishers_info(node, topic),
                self.rt.compat.subscriptions_info(node, topic))

    def node_interfaces(self, fqn: str) -> dict:
        """Publishers, subscribers, services and clients of one node (from the snapshot)."""
        snap = self.snapshot()
        pubs = sorted(t.name for t in snap.topics if any(p.node_fqn == fqn for p in t.publishers))
        subs = sorted(t.name for t in snap.topics
                      if any(s.node_fqn == fqn for s in t.subscribers))
        services = sorted(s.name for s in snap.services if fqn in s.servers)
        actions = sorted(a.name for a in snap.actions if fqn in a.servers)
        action_clients = sorted(a.name for a in snap.actions if fqn in a.clients)
        return {'publishers': pubs, 'subscribers': subs, 'services': services,
                'action_servers': actions, 'action_clients': action_clients}

    # ----------------------------------------------------------------- polling

    def poll(self) -> GraphSnapshot:
        if not self._poll_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            snap = self._build()
        except Exception as exc:  # noqa: BLE001
            self.error(f'graph poll failed: {exc}', exc)
            return self.snapshot()
        finally:
            self._poll_lock.release()
        with self._lock:
            old = self._snapshot
            self._snapshot = snap
        old_nodes = {n.fqn for n in old.nodes}
        new_nodes = {n.fqn for n in snap.nodes}
        if old_nodes != new_nodes:
            self.publish(NodesChanged(nodes=snap.nodes,
                                      added=tuple(sorted(new_nodes - old_nodes)),
                                      removed=tuple(sorted(old_nodes - new_nodes))))
        if snap != old:
            self.publish(GraphChanged(graph=snap))
        return snap

    def _build(self) -> GraphSnapshot:
        node = self.rt.node
        own = join_name(node.get_namespace(), node.get_name())
        nodes = []
        seen = set()
        for name, ns in node.get_node_names_and_namespaces():
            info = NodeInfo(name, ns)
            if info.fqn == own or info.fqn in seen:
                continue
            seen.add(info.fqn)
            nodes.append(info)
        nodes.sort(key=lambda n: n.fqn)

        hide_hidden = bool(self.config.get('graph.hide_hidden_topics', True))
        topics = []
        compat = self.rt.compat
        for tname, types in node.get_topic_names_and_types():
            if hide_hidden and is_hidden_name(tname):
                continue
            try:
                pubs = [p for p in compat.publishers_info(node, tname) if p.node_fqn != own]
                subs = [s for s in compat.subscriptions_info(node, tname) if s.node_fqn != own]
            except Exception:  # noqa: BLE001 - topic vanished between calls
                pubs, subs = [], []
            pubs.sort(key=lambda e: (e.node_fqn, e.gid))
            subs.sort(key=lambda e: (e.node_fqn, e.gid))
            if not pubs and not subs and self._only_own(node, tname):
                continue
            topics.append(TopicInfo(tname, tuple(types), tuple(pubs), tuple(subs)))
        topics.sort(key=lambda t: t.name)

        # Servers and clients per node.
        servers: dict[str, list[str]] = {}
        action_clients: dict[str, list[str]] = {}
        for n in nodes:
            try:
                for sname, _types in node.get_service_names_and_types_by_node(n.name, n.namespace):
                    servers.setdefault(sname, []).append(n.fqn)
                for cname, _types in node.get_client_names_and_types_by_node(n.name, n.namespace):
                    if cname.endswith(_ACTION_SUFFIX):
                        action_clients.setdefault(cname, []).append(n.fqn)
            except Exception:  # noqa: BLE001 - node vanished
                continue

        services = []
        actions = []
        for sname, types in node.get_service_names_and_types():
            if sname.endswith(_ACTION_SUFFIX):
                aname = sname[:-len(_ACTION_SUFFIX)]
                atype = types[0].replace('_SendGoal', '') if types else ''
                actions.append(ActionInfo(aname, atype, tuple(sorted(servers.get(sname, []))),
                                          tuple(sorted(action_clients.get(sname, [])))))
                continue
            if '/_action/' in sname:
                continue
            if sname.startswith(own + '/'):
                continue
            services.append(ServiceInfo(sname, tuple(types), tuple(sorted(servers.get(sname, [])))))
        # Action servers seen only through clients.
        known = {a.name for a in actions}
        for cname, cl in action_clients.items():
            aname = cname[:-len(_ACTION_SUFFIX)]
            if aname not in known:
                actions.append(ActionInfo(aname, '', (), tuple(sorted(cl))))
        services.sort(key=lambda s: s.name)
        actions.sort(key=lambda a: a.name)
        return GraphSnapshot(tuple(nodes), tuple(topics), tuple(services), tuple(actions))

    def _only_own(self, node, tname: str) -> bool:
        """True if the topic exists only because the dashboard itself uses it."""
        try:
            return (node.count_publishers(tname) + node.count_subscribers(tname)) > 0
        except Exception:  # noqa: BLE001
            return False

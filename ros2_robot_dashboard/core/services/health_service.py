"""HealthService: critical topics (rate, latency, timeout), local and remote host metrics.

Remote PCs run the ``host_monitor`` node, which publishes a
``diagnostic_msgs/DiagnosticArray`` on ``/<host>/host_metrics``.
"""

from __future__ import annotations

import glob
import os
import socket
import threading
import time
from typing import Optional

import yaml

from ..config import package_config_dir, user_dir
from ..domain.events import (DiscoveryIssues, GraphChanged, HealthAlert, HealthUpdated,
                             HostMetricsUpdated)
from ..domain.health import (CriticalTopic, HealthLevel, HealthProfile, HostMetrics, TopicHealth,
                             evaluate_topic)
from ..domain.params import NodeStatus
from ..runtime import BaseService
from .host_metrics import LocalMetrics, metrics_from_diagnostics

HOST_METRICS_SUFFIX = '/host_metrics'


def health_profile_dirs(config) -> list[str]:
    dirs = [os.path.join(user_dir(), 'health_profiles')]
    for d in config.get('health.profile_dirs', []) or []:
        dirs.append(os.path.expanduser(d))
    pkg = package_config_dir()
    if pkg:
        dirs.append(os.path.join(pkg, 'health_profiles'))
    return dirs


def parse_health_yaml(text: str, origin: str = '') -> list[HealthProfile]:
    data = yaml.safe_load(text) or {}
    out = []
    for p in data.get('profiles', []) or []:
        topics = tuple(CriticalTopic(
            topic=t['topic'], expected_hz=float(t.get('expected_hz', 0.0)),
            tolerance=float(t.get('tolerance', 0.2)),
            max_latency_ms=(float(t['max_latency_ms']) if t.get('max_latency_ms') is not None
                            else None),
            timeout_s=float(t.get('timeout_s', 2.0))) for t in p.get('topics', []) or [])
        out.append(HealthProfile(str(p['id']), p.get('name', p['id']), topics, origin))
    return out


class HealthService(BaseService):
    name = 'health'

    def __init__(self, rt):
        super().__init__(rt)
        self._profiles: dict[str, HealthProfile] = {}
        self._errors: list[str] = []
        self._active: Optional[HealthProfile] = None
        self._last_levels: dict[str, HealthLevel] = {}
        self._consumers: set[str] = set()
        self._local = LocalMetrics(socket.gethostname())
        self._remote: dict[str, HostMetrics] = {}
        self._remote_subs: dict[str, object] = {}
        self._lock = threading.Lock()
        self._timer = None
        self._bus_sub = None

    def start(self) -> None:
        self.reload()
        default = self.config.get('health.default_profile', '')
        if default and default in self._profiles:
            self.set_profile(default)
        period = float(self.config.get('polling.host_metrics_s', 1.0))
        self._timer = self.rt.create_timer(period, self._tick, 'health.tick')
        self._bus_sub = self.rt.bus.subscribe(GraphChanged, self._on_graph)

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)
        if self._bus_sub is not None:
            self.rt.bus.unsubscribe(self._bus_sub)
        self.set_profile(None)
        for sub in self._remote_subs.values():
            self.rt.destroy_subscription(sub)
        self._remote_subs.clear()

    # ----------------------------------------------------------------- profiles

    def reload(self) -> None:
        profiles: dict[str, HealthProfile] = {}
        errors = []
        for d in health_profile_dirs(self.config):
            for path in sorted(glob.glob(os.path.join(d, '*.yaml'))):
                try:
                    with open(path, encoding='utf-8') as f:
                        for p in parse_health_yaml(f.read(), path):
                            profiles.setdefault(p.id, p)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'{path}: {exc}')
        self._profiles, self._errors = profiles, errors

    def profiles(self) -> list[HealthProfile]:
        return list(self._profiles.values())

    def errors(self) -> list[str]:
        return list(self._errors)

    def active_profile(self) -> Optional[HealthProfile]:
        return self._active

    def set_profile(self, profile_id: Optional[str]) -> None:
        topics = self.rt.topics
        if self._active is not None:
            for t in self._active.topics:
                topics.stop_stats(t.topic, 'health')
        self._active = self._profiles.get(profile_id) if profile_id else None
        self._last_levels.clear()
        if self._active is not None:
            for t in self._active.topics:
                try:
                    topics.start_stats(t.topic, 'health')
                except Exception:  # noqa: BLE001 - topic not there yet: retried in _tick
                    pass

    def set_custom_profile(self, profile: HealthProfile) -> None:
        self._profiles[profile.id] = profile
        self.set_profile(profile.id)

    # ----------------------------------------------------------------- watch

    def watch(self, consumer_id: str) -> None:
        self._consumers.add(consumer_id)

    def unwatch(self, consumer_id: str) -> None:
        self._consumers.discard(consumer_id)

    def evaluate(self) -> list[TopicHealth]:
        if self._active is None:
            return []
        topics = self.rt.topics
        out = []
        for spec in self._active.topics:
            st = topics.stats(spec.topic)
            if st is None:
                try:
                    topics.start_stats(spec.topic, 'health')
                except Exception:  # noqa: BLE001
                    pass
            out.append(evaluate_topic(spec, st))
        return out

    def _tick(self) -> None:
        if self._active is not None:
            results = self.evaluate()
            for r in results:
                prev = self._last_levels.get(r.spec.topic)
                if r.level in (HealthLevel.WARN, HealthLevel.ERROR) and prev != r.level:
                    self.publish(HealthAlert(topic=r.spec.topic, reason=r.reason,
                                             level=r.level.value))
                self._last_levels[r.spec.topic] = r.level
            self.publish(HealthUpdated(profile_id=self._active.id, topics=tuple(results)))
        if self._consumers:
            local = self._local.sample()
            with self._lock:
                remote = [m for m in self._remote.values()
                          if time.time() - m.stamp < 10.0 and m.host != local.host]
            self.publish(HostMetricsUpdated(hosts=tuple([local] + sorted(remote,
                                                                         key=lambda m: m.host))))
            self._check_discovery()

    def _check_discovery(self) -> None:
        """Nodes visible in the graph whose services never answer: typical DDS issue."""
        params = self.rt.params
        unreachable = tuple(sorted(n for n in params.cached_nodes() + list(params._status)
                                   if params.status(n) == NodeStatus.UNREACHABLE))
        if unreachable:
            msg = ('nodes visible but their services do not answer: check discovery '
                   '(subnet, Wi-Fi, firewall, ROS_DOMAIN_ID, ROS_AUTOMATIC_DISCOVERY_RANGE)')
        else:
            msg = ''
        self.publish(DiscoveryIssues(unreachable_nodes=tuple(dict.fromkeys(unreachable)),
                                     message=msg))

    # ----------------------------------------------------------------- remote hosts

    def _on_graph(self, ev: GraphChanged) -> None:
        from diagnostic_msgs.msg import DiagnosticArray
        wanted = {t.name for t in ev.graph.topics if t.name.endswith(HOST_METRICS_SUFFIX)
                  and 'diagnostic_msgs/msg/DiagnosticArray' in t.types}
        for name in wanted - set(self._remote_subs):
            self._remote_subs[name] = self.rt.create_subscription(
                DiagnosticArray, name, self._on_remote, 10, source=f'health {name}')
        for name in set(self._remote_subs) - wanted:
            self.rt.destroy_subscription(self._remote_subs.pop(name))

    def _on_remote(self, msg) -> None:
        for m in metrics_from_diagnostics(msg):
            with self._lock:
                self._remote[m.host] = m

    def hosts(self) -> list[HostMetrics]:
        with self._lock:
            return list(self._remote.values())

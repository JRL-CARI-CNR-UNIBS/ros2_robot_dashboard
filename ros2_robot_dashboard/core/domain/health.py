"""Health types: topic statistics, critical-topic checks and host resources."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from .base import Serializable


@dataclass(frozen=True)
class TopicStatsInfo(Serializable):
    topic: str
    rate_hz: float
    bandwidth_bps: float
    latency_ms: Optional[float] = None      # header.stamp vs receive time, when available
    message_count: int = 0
    last_message_age_s: Optional[float] = None
    mean_size_bytes: float = 0.0


class HealthLevel(enum.Enum):
    OK = 'ok'
    WARN = 'warn'
    ERROR = 'error'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class CriticalTopic(Serializable):
    topic: str
    expected_hz: float
    tolerance: float = 0.2               # fraction of expected_hz
    max_latency_ms: Optional[float] = None
    timeout_s: float = 2.0               # no message for this long = error


@dataclass(frozen=True)
class HealthProfile(Serializable):
    id: str
    name: str
    topics: tuple[CriticalTopic, ...] = ()
    origin: str = ''


@dataclass(frozen=True)
class TopicHealth(Serializable):
    spec: CriticalTopic
    level: HealthLevel
    reason: str
    stats: Optional[TopicStatsInfo] = None


@dataclass(frozen=True)
class HostMetrics(Serializable):
    host: str
    cpu_percent: float
    memory_percent: float
    memory_used_bytes: int = 0
    memory_total_bytes: int = 0
    net_rx_bps: float = 0.0
    net_tx_bps: float = 0.0
    temperature_c: Optional[float] = None
    load_avg: tuple[float, ...] = ()
    per_cpu_percent: tuple[float, ...] = ()
    stamp: float = 0.0
    local: bool = False
    extra: dict = field(default_factory=dict)


def evaluate_topic(spec: CriticalTopic, stats: Optional[TopicStatsInfo]) -> TopicHealth:
    """Compare measured statistics with the expected rate, latency and timeout."""
    if stats is None or stats.message_count == 0:
        return TopicHealth(spec, HealthLevel.ERROR, 'no messages received', stats)
    if stats.last_message_age_s is not None and stats.last_message_age_s > spec.timeout_s:
        return TopicHealth(spec, HealthLevel.ERROR,
                           f'no message for {stats.last_message_age_s:.1f} s', stats)
    if stats.message_count < 3 and stats.rate_hz <= 0.0:
        # Just subscribed: not enough messages to measure a rate yet.
        return TopicHealth(spec, HealthLevel.UNKNOWN, 'measuring…', stats)
    low = spec.expected_hz * (1.0 - spec.tolerance)
    if spec.expected_hz > 0 and stats.rate_hz < low:
        level = HealthLevel.ERROR if stats.rate_hz < 0.5 * low else HealthLevel.WARN
        return TopicHealth(spec, level,
                           f'rate {stats.rate_hz:.1f} Hz < {low:.1f} Hz', stats)
    if (spec.max_latency_ms is not None and stats.latency_ms is not None
            and stats.latency_ms > spec.max_latency_ms):
        return TopicHealth(spec, HealthLevel.WARN,
                           f'latency {stats.latency_ms:.1f} ms > {spec.max_latency_ms:.1f} ms',
                           stats)
    return TopicHealth(spec, HealthLevel.OK, '', stats)

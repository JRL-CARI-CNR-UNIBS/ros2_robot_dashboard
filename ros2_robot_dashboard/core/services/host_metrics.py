"""Host metrics: local sampling with psutil and conversion to/from DiagnosticArray.

The same conversion is used by the ``host_monitor`` node (publisher) and by the
HealthService (subscriber), so the format is defined in one place.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..domain.health import HostMetrics

PREFIX = 'host_metrics: '


class LocalMetrics:
    """Samples CPU, memory, network rates and temperature with psutil."""

    def __init__(self, host: str):
        self.host = host
        self._last_net = None
        self._last_t = 0.0
        try:
            import psutil
            self._psutil = psutil
            psutil.cpu_percent(percpu=True)       # first call primes the counters
        except ImportError:
            self._psutil = None

    def sample(self) -> HostMetrics:
        ps = self._psutil
        now = time.time()
        if ps is None:
            return HostMetrics(self.host, 0.0, 0.0, stamp=now, local=True,
                               extra={'error': 'psutil not installed'})
        per_cpu = tuple(ps.cpu_percent(percpu=True))
        cpu = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
        mem = ps.virtual_memory()
        net = ps.net_io_counters()
        rx = tx = 0.0
        if self._last_net is not None and now > self._last_t:
            dt = now - self._last_t
            rx = max(0.0, (net.bytes_recv - self._last_net.bytes_recv) / dt)
            tx = max(0.0, (net.bytes_sent - self._last_net.bytes_sent) / dt)
        self._last_net, self._last_t = net, now
        try:
            load = tuple(os.getloadavg())
        except OSError:
            load = ()
        return HostMetrics(self.host, cpu, float(mem.percent), int(mem.used), int(mem.total),
                           rx, tx, self._temperature(), load, per_cpu, now, local=True)

    def _temperature(self) -> Optional[float]:
        fn = getattr(self._psutil, 'sensors_temperatures', None)
        if fn is None:
            return None
        try:
            temps = fn()
        except Exception:  # noqa: BLE001
            return None
        best = None
        for key in ('coretemp', 'k10temp', 'cpu_thermal', 'acpitz', 'zenpower'):
            for entry in temps.get(key, []):
                if entry.current and (best is None or entry.current > best):
                    best = float(entry.current)
            if best is not None:
                return best
        for entries in temps.values():
            for entry in entries:
                if entry.current and (best is None or entry.current > best):
                    best = float(entry.current)
        return best


def metrics_to_diagnostics(m: HostMetrics, stamp_msg=None):
    """HostMetrics -> diagnostic_msgs/DiagnosticArray (one status per host)."""
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

    arr = DiagnosticArray()
    if stamp_msg is not None:
        arr.header.stamp = stamp_msg
    st = DiagnosticStatus()
    st.name = PREFIX + m.host
    st.hardware_id = m.host
    high = m.cpu_percent > 90 or m.memory_percent > 90 or (m.temperature_c or 0) > 85
    st.level = DiagnosticStatus.WARN if high else DiagnosticStatus.OK
    st.message = (f'CPU {m.cpu_percent:.0f}%  MEM {m.memory_percent:.0f}%'
                  + (f'  {m.temperature_c:.0f}°C' if m.temperature_c is not None else ''))
    values = {
        'host': m.host,
        'cpu_percent': f'{m.cpu_percent:.2f}',
        'memory_percent': f'{m.memory_percent:.2f}',
        'memory_used_bytes': str(m.memory_used_bytes),
        'memory_total_bytes': str(m.memory_total_bytes),
        'net_rx_bps': f'{m.net_rx_bps:.1f}',
        'net_tx_bps': f'{m.net_tx_bps:.1f}',
        'load_avg': ' '.join(f'{v:.2f}' for v in m.load_avg),
        'per_cpu_percent': ' '.join(f'{v:.1f}' for v in m.per_cpu_percent),
    }
    if m.temperature_c is not None:
        values['temperature_c'] = f'{m.temperature_c:.1f}'
    st.values = [KeyValue(key=k, value=v) for k, v in values.items()]
    arr.status = [st]
    return arr


def metrics_from_diagnostics(msg) -> list[HostMetrics]:
    out = []
    now = time.time()
    for st in msg.status:
        if not st.name.startswith(PREFIX):
            continue
        kv = {k.key: k.value for k in st.values}

        def f(key: str, default: float = 0.0) -> float:
            try:
                return float(kv.get(key, default))
            except ValueError:
                return default

        def floats(key: str) -> tuple[float, ...]:
            try:
                return tuple(float(x) for x in kv.get(key, '').split())
            except ValueError:
                return ()

        temp = kv.get('temperature_c')
        out.append(HostMetrics(
            host=kv.get('host', st.hardware_id or st.name[len(PREFIX):]),
            cpu_percent=f('cpu_percent'), memory_percent=f('memory_percent'),
            memory_used_bytes=int(f('memory_used_bytes')),
            memory_total_bytes=int(f('memory_total_bytes')),
            net_rx_bps=f('net_rx_bps'), net_tx_bps=f('net_tx_bps'),
            temperature_c=float(temp) if temp not in (None, '') else None,
            load_avg=floats('load_avg'), per_cpu_percent=floats('per_cpu_percent'),
            stamp=now, local=False))
    return out

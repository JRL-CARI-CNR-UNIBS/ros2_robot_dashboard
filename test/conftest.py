"""Shared test setup.

Tests are isolated from the lab network and from the user's files:
- random ROS_DOMAIN_ID and localhost-only discovery,
- ROS_HOME and XDG_STATE_HOME point to a temporary directory.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'test'))

if 'DASHBOARD_TEST_KEEP_ENV' not in os.environ:
    os.environ['ROS_DOMAIN_ID'] = str(random.randint(150, 230))
    os.environ['ROS_LOCALHOST_ONLY'] = '1'                 # Humble
    os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'   # Jazzy and newer
_TMP = tempfile.mkdtemp(prefix='dashboard_test_')
os.environ['ROS_HOME'] = os.path.join(_TMP, 'ros_home')
os.environ['XDG_STATE_HOME'] = os.path.join(_TMP, 'state')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(scope='session')
def qapp_args():
    return ['dashboard-tests']


def pytest_configure(config):
    # Same font cache workaround as the application (see adapters/qt/env_fix.py).
    try:
        from ros2_robot_dashboard.adapters.qt.env_fix import prepare_fontconfig
    except ImportError:
        return
    restore = prepare_fontconfig()
    try:
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            config._dashboard_qapp = QApplication(['dashboard-tests'])
    except ImportError:
        pass
    restore()


def _rclpy_available() -> bool:
    try:
        import rclpy  # noqa: F401
        return True
    except ImportError:
        return False


HAS_RCLPY = _rclpy_available()


def pytest_collection_modifyitems(config, items):
    skip_ros = pytest.mark.skip(reason='rclpy not available')
    skip_cell = pytest.mark.skip(reason='set RUN_CELL_TESTS=1 to run the UR mock cell tests')
    for item in items:
        if 'ros' in item.keywords and not HAS_RCLPY:
            item.add_marker(skip_ros)
        if 'cell' in item.keywords and os.environ.get('RUN_CELL_TESTS') != '1':
            item.add_marker(skip_cell)


def wait_until(predicate, timeout: float = 10.0, period: float = 0.05):
    """Poll ``predicate`` until it returns a truthy value; return it (or None on timeout)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = predicate()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
        time.sleep(period)
    return None


@pytest.fixture(scope='session')
def tmp_root():
    return _TMP


@pytest.fixture(scope='session')
def dummy_system():
    if not HAS_RCLPY:
        pytest.skip('rclpy not available')
    from fixtures.dummy_nodes import DummySystem
    system = DummySystem()
    yield system
    system.shutdown()


@pytest.fixture(scope='session')
def runtime(dummy_system):
    from ros2_robot_dashboard.core.config import Config
    from ros2_robot_dashboard.core.runtime import CoreRuntime
    cfg = Config({'polling': {'graph_s': 0.5, 'controllers_s': 0.5, 'lifecycle_s': 0.5}})
    rt = CoreRuntime(cfg).start()
    # Wait for discovery of the fixture nodes.
    assert wait_until(lambda: any(n.fqn == '/test/dummy_params'
                                  for n in rt.graph.poll().nodes), 20.0), 'discovery failed'
    yield rt
    rt.stop()


class EventRecorder:
    """Collects events of given types from the bus."""

    def __init__(self, bus, *types):
        import threading
        self.events = []
        self._lock = threading.Lock()
        self._subs = [bus.subscribe(t, self._add) for t in types]
        self._bus = bus

    def _add(self, ev):
        with self._lock:
            self.events.append(ev)

    def of(self, cls, pred=lambda e: True):
        with self._lock:
            return [e for e in self.events if isinstance(e, cls) and pred(e)]

    def wait(self, cls, pred=lambda e: True, timeout: float = 10.0):
        return wait_until(lambda: self.of(cls, pred), timeout)

    def close(self):
        for s in self._subs:
            self._bus.unsubscribe(s)


@pytest.fixture
def recorder(runtime):
    recs = []

    def make(*types):
        r = EventRecorder(runtime.bus, *types)
        recs.append(r)
        return r

    yield make
    for r in recs:
        r.close()

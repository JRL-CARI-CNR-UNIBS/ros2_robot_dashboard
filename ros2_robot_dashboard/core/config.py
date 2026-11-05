"""Dashboard configuration and standard paths.

Configuration is merged from (lowest to highest priority):
1. built-in defaults (``DEFAULTS``),
2. the package ``config/dashboard.yaml``,
3. ``$ROS_HOME/robot_dashboard/dashboard.yaml`` (``ROS_HOME`` defaults to ``~/.ros``),
4. a file given with ``--config``,
5. command-line overrides.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Optional

import yaml

DEFAULTS: dict[str, Any] = {
    'distro_override': '',
    'read_only': False,
    # auto: private fontconfig cache inside conda/pixi (avoids a fontconfig crash)
    'qt': {'private_font_cache': 'auto'},
    # ROS executor of the core: single (fast, default) or multi
    'core': {'executor': 'single'},
    'confirm_actions': True,
    'confirm_low_risk_params': False,
    'timeouts': {
        'service_s': 2.0,
        'switch_controller_s': 5.0,
        'lifecycle_s': 5.0,
        'wait_for_service_s': 1.0,
    },
    'polling': {
        'graph_s': 1.5,
        'controllers_s': 1.0,
        'lifecycle_s': 2.0,
        'tf_tree_s': 1.0,
        'host_metrics_s': 1.0,
    },
    'graph': {
        'hidden_node_regex': r'^/(_ros2cli|launch_ros|transform_listener_impl|robot_dashboard_)',
        'hide_hidden_topics': True,
        'max_elements': 600,
    },
    'params': {
        'chunk_size': 50,
        'debounce_ms': 200,
        'auto_apply': True,
        'atomic_apply': False,
        'hide_regex': r'^(use_sim_time|qos_overrides\..*|start_type_description_service)$',
        'critical': [],          # list of "node:param" (glob) needing double confirmation
    },
    'topics': {
        'echo_max_rate_hz': 15.0,
        'echo_truncate_length': 64,
        'terminal_max_lines': 5000,
        'heavy_types': [
            'sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage',
            'sensor_msgs/msg/PointCloud2', 'sensor_msgs/msg/LaserScan',
            'nav_msgs/msg/OccupancyGrid', 'octomap_msgs/msg/Octomap',
        ],
        'heavy_rate_hz': 100.0,
        'file_queue_max': 20000,
        'stats_window_s': 2.0,
    },
    'logs': {'buffer_size': 20000},
    'diagnostics': {'stale_s': 5.0, 'history': 50},
    'robot': {
        'joint_states_topic': '/joint_states',
        'robot_description_topic': '/robot_description',
        'robot_description_node': '/robot_state_publisher',
        'publish_rate_hz': 10.0,
        'near_limit_margin': 0.05,
    },
    'bag': {'default_dir': '~/bags'},
    'launch': {'sigint_timeout_s': 10.0, 'sigterm_timeout_s': 5.0, 'output_max_lines': 20000},
    'safety': {
        'risky_service_words': ['stop', 'reset', 'enable', 'disable', 'switch', 'start',
                                'power', 'brake', 'unlock', 'shutdown', 'restart', 'home',
                                'execute', 'move', 'set_io', 'play'],
    },
    'health': {'default_profile': ''},
}


def ros_home() -> str:
    return os.path.expanduser(os.environ.get('ROS_HOME', '~/.ros'))


def user_dir() -> str:
    """Shared, copyable configuration: profiles, presets, health profiles."""
    return os.path.join(ros_home(), 'robot_dashboard')


def state_dir() -> str:
    """Machine-local state: action log, graph layout cache."""
    base = os.environ.get('XDG_STATE_HOME', os.path.expanduser('~/.local/state'))
    return os.path.join(base, 'ros2_robot_dashboard')


def package_config_dir() -> Optional[str]:
    """``share/ros2_robot_dashboard/config`` when installed, else the source tree."""
    try:
        from ament_index_python.packages import get_package_share_directory
        d = os.path.join(get_package_share_directory('ros2_robot_dashboard'), 'config')
        if os.path.isdir(d):
            return d
    except Exception:  # noqa: BLE001 - not installed / no ament index
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.normpath(os.path.join(here, '..', '..', 'config'))
    return d if os.path.isdir(d) else None


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: Optional[dict] = None, sources: Optional[list[str]] = None):
        self.data = deep_merge(DEFAULTS, data or {})
        self.sources = sources or []

    @classmethod
    def load(cls, extra_file: Optional[str] = None,
             overrides: Optional[dict] = None, use_user_files: bool = True) -> 'Config':
        data: dict = {}
        sources: list[str] = []
        candidates = []
        pkg = package_config_dir()
        if pkg:
            candidates.append(os.path.join(pkg, 'dashboard.yaml'))
        if use_user_files:
            candidates.append(os.path.join(user_dir(), 'dashboard.yaml'))
        if extra_file:
            candidates.append(os.path.expanduser(extra_file))
        for path in candidates:
            if os.path.isfile(path):
                with open(path, encoding='utf-8') as f:
                    loaded = yaml.safe_load(f) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f'{path}: top level must be a mapping')
                data = deep_merge(data, loaded)
                sources.append(path)
            elif path == extra_file:
                raise FileNotFoundError(path)
        if overrides:
            data = deep_merge(data, overrides)
        return cls(data, sources)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split('.')
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

"""Shared compat behaviour. Distro classes override only what differs.

Where possible the code uses feature detection (message fields present or not)
instead of the distro name, so new distros usually work without changes.
"""

from __future__ import annotations

import os
from typing import Any

from ..domain.bag import BagInfo, BagPlayConfig, BagRecordConfig, BagTopic
from ..domain.control import ControllerInfo, HwComponentInfo, HwInterfaceInfo
from ..domain.graph import EndpointInfo, QosInfo
from .interface import Capabilities, CompatLayer

_INFINITE_NS = 0x7FFFFFFFFFFFFFFF


def _duration_s(d) -> float | None:
    """rclpy Duration -> seconds; None for 'infinite' or 'default' (0)."""
    if d is None:
        return None
    ns = getattr(d, 'nanoseconds', None)
    if ns is None:
        sec = getattr(d, 'sec', 0)
        nsec = getattr(d, 'nsec', getattr(d, 'nanosec', 0))
        ns = sec * 1_000_000_000 + nsec
    if ns <= 0 or ns >= _INFINITE_NS or ns >= 9_000_000_000 * 1_000_000_000:
        return None
    return ns / 1e9


def _policy_name(policy) -> str:
    if policy is None:
        return 'unknown'
    name = getattr(policy, 'name', str(policy)).lower()
    return {
        'reliable': 'reliable', 'best_effort': 'best_effort',
        'volatile': 'volatile', 'transient_local': 'transient_local',
        'keep_last': 'keep_last', 'keep_all': 'keep_all',
        'automatic': 'automatic', 'manual_by_topic': 'manual_by_topic',
        'system_default': 'system_default',
    }.get(name, name)


def qos_from_rclpy(profile) -> QosInfo:
    if profile is None:
        return QosInfo()
    return QosInfo(
        reliability=_policy_name(getattr(profile, 'reliability', None)),
        durability=_policy_name(getattr(profile, 'durability', None)),
        history=_policy_name(getattr(profile, 'history', None)),
        depth=int(getattr(profile, 'depth', 0) or 0),
        deadline_s=_duration_s(getattr(profile, 'deadline', None)),
        lifespan_s=_duration_s(getattr(profile, 'lifespan', None)),
        liveliness=_policy_name(getattr(profile, 'liveliness', None)),
        liveliness_lease_s=_duration_s(getattr(profile, 'liveliness_lease_duration', None)),
    )


def _gid_text(gid) -> str:
    if gid is None:
        return ''
    try:
        return '.'.join(f'{b:02x}' for b in bytes(gid))
    except (TypeError, ValueError):
        return str(gid)


def _has_field(msg_or_cls, name: str) -> bool:
    fields = getattr(msg_or_cls, 'get_fields_and_field_types', None)
    if fields is None:
        return hasattr(msg_or_cls, name)
    return name in fields()


class CommonCompat(CompatLayer):
    DISTRO = 'unknown'
    BAG_DEFAULT_STORAGE = 'mcap'

    def __init__(self) -> None:
        self._caps: Capabilities | None = None

    def distro(self) -> str:
        return self.DISTRO

    # ----------------------------------------------------------------- capabilities

    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = self._detect_capabilities()
        return self._caps

    def _detect_capabilities(self) -> Capabilities:
        kwargs: dict[str, Any] = {'distro': self.DISTRO}
        try:
            from controller_manager_msgs import msg as cm_msg, srv as cm_srv
            kwargs['switch_returns_message'] = _has_field(cm_srv.SwitchController.Response,
                                                          'message')
            kwargs['cleanup_controller'] = hasattr(cm_srv, 'CleanupController')
            kwargs['controller_update_rate'] = _has_field(cm_msg.ControllerState, 'update_rate')
            kwargs['hw_interface_data_type'] = _has_field(cm_msg.HardwareInterface, 'data_type')
        except ImportError:
            pass
        kwargs['bag_default_storage'] = self.BAG_DEFAULT_STORAGE
        kwargs['bag_storages'] = self._bag_storages()
        kwargs.update(self._extra_capabilities())
        return Capabilities(**kwargs)

    def _extra_capabilities(self) -> dict:
        return {}

    def _bag_storages(self) -> tuple[str, ...]:
        try:
            import rosbag2_py
            fn = getattr(rosbag2_py, 'get_registered_writers', None)
            if fn is not None:
                found = tuple(sorted(fn()))
                if found:
                    return found
        except Exception:  # noqa: BLE001 - optional dependency
            pass
        return ('mcap', 'sqlite3')

    # ----------------------------------------------------------------- graph

    def endpoint_from_rclpy(self, info, kind: str) -> EndpointInfo:
        etype = getattr(info, 'endpoint_type', None)
        ename = getattr(etype, 'name', str(etype or kind)).lower()
        if 'pub' in ename:
            kind = 'publisher'
        elif 'sub' in ename:
            kind = 'subscription'
        return EndpointInfo(
            node_name=getattr(info, 'node_name', ''),
            node_namespace=getattr(info, 'node_namespace', '/'),
            topic_type=getattr(info, 'topic_type', ''),
            endpoint_type=kind,
            gid=_gid_text(getattr(info, 'endpoint_gid', None)),
            qos=qos_from_rclpy(getattr(info, 'qos_profile', None)),
        )

    def publishers_info(self, node, topic: str) -> list[EndpointInfo]:
        return [self.endpoint_from_rclpy(i, 'publisher')
                for i in node.get_publishers_info_by_topic(topic)]

    def subscriptions_info(self, node, topic: str) -> list[EndpointInfo]:
        return [self.endpoint_from_rclpy(i, 'subscription')
                for i in node.get_subscriptions_info_by_topic(topic)]

    # ----------------------------------------------------------------- ros2_control

    def controller_from_msg(self, msg) -> ControllerInfo:
        rate = getattr(msg, 'update_rate', None)
        return ControllerInfo(
            name=msg.name,
            type=msg.type,
            state=msg.state,
            claimed_interfaces=tuple(msg.claimed_interfaces),
            required_command_interfaces=tuple(msg.required_command_interfaces),
            required_state_interfaces=tuple(msg.required_state_interfaces),
            update_rate=int(rate) if rate is not None else None,
            is_chainable=bool(getattr(msg, 'is_chainable', False)),
            is_chained=bool(getattr(msg, 'is_chained', False)),
        )

    def hw_interface_from_msg(self, msg, kind: str) -> HwInterfaceInfo:
        return HwInterfaceInfo(
            name=msg.name,
            kind=kind,
            is_available=bool(msg.is_available),
            is_claimed=bool(msg.is_claimed),
            data_type=getattr(msg, 'data_type', ''),
        )

    def hw_component_from_msg(self, msg) -> HwComponentInfo:
        plugin = getattr(msg, 'plugin_name', '') or getattr(msg, 'class_type', '')
        return HwComponentInfo(
            name=msg.name,
            type=msg.type,
            plugin=plugin,
            state_id=int(msg.state.id),
            state_label=msg.state.label,
            command_interfaces=tuple(self.hw_interface_from_msg(i, 'command')
                                     for i in msg.command_interfaces),
            state_interfaces=tuple(self.hw_interface_from_msg(i, 'state')
                                   for i in msg.state_interfaces),
        )

    def make_switch_request(self, activate, deactivate, strict: bool, timeout_s: float,
                            activate_asap: bool = False):
        from builtin_interfaces.msg import Duration
        from controller_manager_msgs.srv import SwitchController

        req = SwitchController.Request()
        req.activate_controllers = list(activate)
        req.deactivate_controllers = list(deactivate)
        req.strictness = (SwitchController.Request.STRICT if strict
                          else SwitchController.Request.BEST_EFFORT)
        if _has_field(req, 'activate_asap'):
            req.activate_asap = bool(activate_asap)
        sec = int(timeout_s)
        req.timeout = Duration(sec=sec, nanosec=int((timeout_s - sec) * 1e9))
        return req

    def switch_result(self, response) -> tuple[bool, str]:
        return bool(response.ok), getattr(response, 'message', '')

    # ----------------------------------------------------------------- bags

    def _record_topic_args(self, config: BagRecordConfig) -> list[str]:
        args: list[str] = []
        if config.all_topics:
            args.append('--all-topics')
        elif config.topics:
            args += ['--topics', *config.topics]
        if config.include_regex:
            args += ['--regex', config.include_regex]
        if config.exclude_regex:
            args += ['--exclude-regex', config.exclude_regex]
        return args

    def bag_record_command(self, config: BagRecordConfig, output_path: str) -> list[str]:
        cmd = ['ros2', 'bag', 'record', '-o', output_path]
        storage = config.storage or self.BAG_DEFAULT_STORAGE
        cmd += ['-s', storage]
        cmd += self._record_topic_args(config)
        if config.max_duration_s:
            cmd += ['-d', str(int(config.max_duration_s))]
        if config.max_size_bytes:
            cmd += ['-b', str(int(config.max_size_bytes))]
        if config.compression:
            cmd += ['--compression-mode', config.compression_mode or 'file',
                    '--compression-format', config.compression]
        if config.qos_overrides_path:
            cmd += ['--qos-profile-overrides-path', config.qos_overrides_path]
        if config.include_hidden:
            cmd.append('--include-hidden-topics')
        if config.include_unpublished:
            cmd.append('--include-unpublished-topics')
        cmd += self._record_extra_args(config)
        return cmd

    def _record_extra_args(self, config: BagRecordConfig) -> list[str]:
        # Keyboard controls read from the terminal; we drive the process with signals.
        return ['--disable-keyboard-controls']

    def bag_play_command(self, config: BagPlayConfig) -> list[str]:
        cmd = ['ros2', 'bag', 'play', config.path]
        if config.storage and self._play_accepts_storage():
            cmd += ['-s', config.storage]
        if config.rate and config.rate != 1.0:
            cmd += ['-r', f'{config.rate:g}']
        if config.loop:
            cmd.append('-l')
        if config.start_offset_s:
            cmd += ['--start-offset', f'{config.start_offset_s:g}']
        if config.topics:
            cmd += ['--topics', *config.topics]
        if config.clock:
            cmd += ['--clock', f'{config.clock_hz:g}']
        if config.start_paused:
            cmd.append('-p')
        cmd.append('--disable-keyboard-controls')
        return cmd

    def _play_accepts_storage(self) -> bool:
        return True

    def bag_info(self, path: str) -> BagInfo:
        """Read metadata.yaml (same layout on all distros); fall back to rosbag2_py."""
        import yaml

        path = os.path.expanduser(path)
        bag_dir = path if os.path.isdir(path) else os.path.dirname(path)
        meta_path = os.path.join(bag_dir, 'metadata.yaml')
        size = _dir_size(bag_dir)
        if os.path.isfile(meta_path):
            with open(meta_path, encoding='utf-8') as f:
                meta = yaml.safe_load(f) or {}
            info = meta.get('rosbag2_bagfile_information', meta)
            topics = []
            for t in info.get('topics_with_message_count', []) or []:
                md = t.get('topic_metadata', {})
                topics.append(BagTopic(md.get('name', ''), md.get('type', ''),
                                       int(t.get('message_count', 0)),
                                       md.get('serialization_format', 'cdr')))
            duration = info.get('duration', {}).get('nanoseconds', 0) / 1e9
            start = info.get('starting_time', {}).get('nanoseconds_since_epoch', 0) / 1e9
            return BagInfo(
                path=bag_dir,
                storage=info.get('storage_identifier', ''),
                duration_s=duration,
                start_s=start,
                message_count=int(info.get('message_count', 0)),
                size_bytes=size,
                topics=tuple(sorted(topics, key=lambda t: t.name)),
                files=tuple(info.get('relative_file_paths', []) or []),
                compression=info.get('compression_format', '') or '',
            )
        return self._bag_info_rosbag2_py(path, size)

    def _bag_info_rosbag2_py(self, path: str, size: int) -> BagInfo:
        import rosbag2_py

        storage = 'mcap' if path.endswith('.mcap') else (
            'sqlite3' if path.endswith('.db3') else '')
        meta = rosbag2_py.Info().read_metadata(path, storage)
        topics = tuple(sorted(
            (BagTopic(t.topic_metadata.name, t.topic_metadata.type, int(t.message_count),
                      t.topic_metadata.serialization_format)
             for t in meta.topics_with_message_count), key=lambda t: t.name))
        return BagInfo(
            path=path, storage=meta.storage_identifier,
            duration_s=meta.duration.total_seconds() if hasattr(meta.duration, 'total_seconds')
            else meta.duration.nanoseconds / 1e9,
            start_s=meta.starting_time.timestamp() if hasattr(meta.starting_time, 'timestamp')
            else 0.0,
            message_count=int(meta.message_count), size_bytes=size, topics=topics,
            files=tuple(meta.relative_file_paths), compression=meta.compression_format,
        )

    # ----------------------------------------------------------------- messages

    def message_to_yaml(self, msg, truncate_length: int | None = None,
                        no_arr: bool = False) -> str:
        from rosidl_runtime_py import message_to_yaml
        return message_to_yaml(msg, truncate_length=truncate_length, no_arr=no_arr)


def _dir_size(path: str) -> int:
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total

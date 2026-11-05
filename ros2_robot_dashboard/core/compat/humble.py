"""ROS 2 Humble specifics.

- ``ros2 bag record``: topics are positional, ``-a`` means all topics, exclusion is ``-x``,
  no ``--disable-keyboard-controls``; default storage is sqlite3.
- ``SwitchController.Response`` has no ``message`` (feature-detected in common).
- ``ControllerState`` has no ``update_rate``; ``HardwareComponentState`` uses ``class_type``.
"""

from __future__ import annotations

from ..domain.bag import BagRecordConfig
from .common import CommonCompat


class HumbleCompat(CommonCompat):
    DISTRO = 'humble'
    BAG_DEFAULT_STORAGE = 'sqlite3'

    def _record_topic_args(self, config: BagRecordConfig) -> list[str]:
        args: list[str] = []
        if config.all_topics:
            args.append('-a')
        if config.include_regex:
            args += ['-e', config.include_regex]
        if config.exclude_regex:
            args += ['-x', config.exclude_regex]
        return args

    def bag_record_command(self, config: BagRecordConfig, output_path: str) -> list[str]:
        cmd = super().bag_record_command(config, output_path)
        if not config.all_topics and config.topics:
            cmd += ['--', *config.topics]     # positional topics go last
        return cmd

    def _record_extra_args(self, config: BagRecordConfig) -> list[str]:
        return []

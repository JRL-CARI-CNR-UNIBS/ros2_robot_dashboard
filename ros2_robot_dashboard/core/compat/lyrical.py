"""ROS 2 Lyrical specifics.

- ``rclpy.topic_endpoint_info`` was renamed to ``rclpy.endpoint_info``: we only read
  attributes of the returned objects, so no import is needed.
- ``ros2 bag play`` has no ``-s/--storage`` option (storage is detected from the file).
- ``HardwareComponentState`` dropped ``class_type`` (``plugin_name`` is used).
"""

from __future__ import annotations

from .jazzy import JazzyCompat


class LyricalCompat(JazzyCompat):
    DISTRO = 'lyrical'

    def _play_accepts_storage(self) -> bool:
        return False

"""ROS 2 Jazzy specifics.

- ``ros2 bag record`` uses ``--topics``, ``--all-topics``, ``--exclude-regex``;
  default storage is MCAP; services can be recorded too.
- ``SwitchController.Response.message`` and ``CleanupController`` exist.
"""

from __future__ import annotations

from .common import CommonCompat


class JazzyCompat(CommonCompat):
    DISTRO = 'jazzy'
    BAG_DEFAULT_STORAGE = 'mcap'

    def _extra_capabilities(self) -> dict:
        return {'bag_record_services': True, 'bag_play_delay': True,
                'message_publisher_gid': True}

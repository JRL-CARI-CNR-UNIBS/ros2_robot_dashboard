"""Select the CompatLayer implementation from ``ROS_DISTRO``.

Unknown distros use the newest implementation and log a warning.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .common import CommonCompat
from .humble import HumbleCompat
from .interface import Capabilities, CompatLayer
from .jazzy import JazzyCompat
from .lyrical import LyricalCompat

_log = logging.getLogger(__name__)

IMPLEMENTATIONS = {
    'humble': HumbleCompat,
    'jazzy': JazzyCompat,
    'lyrical': LyricalCompat,
}
NEWEST = LyricalCompat


def create_compat(distro: Optional[str] = None) -> CompatLayer:
    name = (distro or os.environ.get('ROS_DISTRO', '')).strip().lower()
    cls = IMPLEMENTATIONS.get(name)
    if cls is None:
        _log.warning("unknown ROS distro '%s': using the %s compat layer",
                     name or '(unset)', NEWEST.DISTRO)
        cls = NEWEST
    return cls()


__all__ = ['Capabilities', 'CommonCompat', 'CompatLayer', 'create_compat',
           'HumbleCompat', 'JazzyCompat', 'LyricalCompat', 'IMPLEMENTATIONS']

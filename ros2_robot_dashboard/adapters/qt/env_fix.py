"""Work around environment problems before Qt starts.

Fontconfig cache mismatch (conda / pixi / RoboStack): the conda ``libfontconfig`` can be
newer than the system one and crash (SIGSEGV in ``FcCharSetFindLeafForward``) while reading
the cache in ``~/.cache/fontconfig`` written by the system version. It happens as soon as
Qt needs a fallback font, for example to draw an arrow. Inside a conda environment we give
fontconfig its own cache directory. ``XDG_CACHE_HOME`` is only changed while Qt initialises
fontconfig, then restored, so launched processes see the original environment.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Callable


def in_conda_env() -> bool:
    return os.path.isdir(os.path.join(sys.prefix, 'conda-meta'))


def private_cache_dir() -> str:
    tag = hashlib.sha1(sys.prefix.encode()).hexdigest()[:10]
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'ros2_robot_dashboard', f'fontconfig-{tag}')


def prepare_fontconfig(mode: str = 'auto') -> Callable[[], None]:
    """Call before creating the QApplication; call the returned function after it.

    ``mode``: ``auto`` (only inside conda), ``on`` or ``off``.
    """
    if mode == 'off' or (mode == 'auto' and not in_conda_env()) or \
            os.environ.get('ROS2_DASHBOARD_SYSTEM_FONT_CACHE') == '1':
        return lambda: None
    cache = private_cache_dir()
    os.makedirs(cache, exist_ok=True)
    old = os.environ.get('XDG_CACHE_HOME')
    os.environ['XDG_CACHE_HOME'] = cache

    def restore() -> None:
        try:
            # Make fontconfig read its configuration now (while the variable is set).
            from PySide6.QtGui import QFontDatabase
            QFontDatabase.families()
        except Exception:  # noqa: BLE001
            pass
        if old is None:
            os.environ.pop('XDG_CACHE_HOME', None)
        else:
            os.environ['XDG_CACHE_HOME'] = old

    return restore

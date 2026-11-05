"""Entry point: ``ros2 run ros2_robot_dashboard robot_dashboard``."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from .cli import parse
    args = parse(argv)
    try:
        from .adapters.qt.app import run
    except ImportError as exc:
        if 'PySide6' in str(exc):
            print('PySide6 is not installed. Install it with "pip install PySide6" '
                  '(or conda/pixi), see the README.', file=sys.stderr)
            return 1
        raise
    return run(args)


if __name__ == '__main__':
    sys.exit(main())

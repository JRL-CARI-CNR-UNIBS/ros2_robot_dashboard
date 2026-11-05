"""Command-line arguments."""

from __future__ import annotations

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='robot_dashboard',
        description='Robot-agnostic ROS 2 dashboard (PySide6).')
    p.add_argument('--read-only', action='store_true',
                   help='disable every action that changes the system')
    p.add_argument('--config', metavar='FILE', help='extra dashboard YAML configuration')
    p.add_argument('--page', metavar='ID',
                   help='page to open: params, topics, services, graph, tf, robot, controllers, '
                        'diagnostics, launch, bags, health, utilities')
    p.add_argument('--theme', choices=['light', 'dark'], help='UI theme')
    p.add_argument('--distro', help='force the compat layer (humble, jazzy, lyrical)')
    p.add_argument('--screenshot', metavar='PNG', help=argparse.SUPPRESS)
    p.add_argument('--screenshot-delay', type=float, default=3.0, help=argparse.SUPPRESS)
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return p


def parse(argv=None):
    # ROS adds --ros-args ...: ignore everything after it.
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--ros-args' in argv:
        argv = argv[:argv.index('--ros-args')]
    return build_parser().parse_args(argv)

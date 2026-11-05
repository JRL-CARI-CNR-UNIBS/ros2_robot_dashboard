"""Build the core, the Qt bridge and the main window; run the Qt event loop."""

from __future__ import annotations

import os
import signal
import sys

from ...core.config import Config
from ...core.runtime import CoreRuntime


def run(args) -> int:
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    from . import theme
    from .main_window import MainWindow

    overrides: dict = {}
    if args.read_only:
        overrides['read_only'] = True
    if args.distro:
        overrides['distro_override'] = args.distro
    config = Config.load(args.config, overrides=overrides)

    from .env_fix import prepare_fontconfig
    restore_env = prepare_fontconfig(str(config.get('qt.private_font_cache', 'auto')))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    restore_env()
    app.setApplicationName('ROS 2 Robot Dashboard')
    app.setOrganizationName('ros2_robot_dashboard')
    settings = QSettings('ros2_robot_dashboard', 'ros2_robot_dashboard')
    theme.apply_theme(app, args.theme or settings.value('window/theme', 'light'))

    rt = CoreRuntime(config)
    rt.start()
    window = MainWindow(rt, settings, initial_page=args.page or '')
    window.show()

    # Ctrl+C in the terminal closes the window cleanly.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)    # lets Python handle signals
    keepalive.start(200)

    if args.screenshot:
        _schedule_screenshot(app, window, args.screenshot, args.screenshot_delay)

    try:
        code = app.exec()
    finally:
        rt.stop()
    return int(code)


def _schedule_screenshot(app, window, path: str, delay_s: float) -> None:
    from PySide6.QtCore import QTimer

    def shot():
        window.grab().save(os.path.expanduser(path))
        window.close()

    QTimer.singleShot(int(delay_s * 1000), shot)

"""BasePage and the context shared by all pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..qt_bridge import QtBridge
from ..widgets.common import confirm, show_error


@dataclass
class PageContext:
    bridge: QtBridge
    settings: QSettings
    status: Callable[[str, int], None]          # status bar message (text, ms)
    navigate: Callable[[str, str, str], None]   # (page_id, kind, name)

    @property
    def rt(self):
        return self.bridge.rt


class BasePage(QWidget):
    """A page of the dashboard. Subscriptions start in ``on_enter`` and stop in ``on_leave``."""

    page_id = 'base'
    title = 'Base'
    icon_name = 'SP_FileIcon'
    writes = False        # True if the page has write actions (disabled in read-only mode)

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.bridge = ctx.bridge
        self.rt = ctx.rt
        self.active = False
        self._regs = []
        self._write_widgets: list[QWidget] = []
        self.bridge.read_only_changed.connect(lambda _ro: self._apply_read_only())

    # ----------------------------------------------------------------- lifecycle

    def enter(self) -> None:
        if not self.active:
            self.active = True
            self._apply_read_only()
            self.on_enter()

    def leave(self) -> None:
        if self.active:
            self.active = False
            self.on_leave()

    def on_enter(self) -> None:
        pass

    def on_leave(self) -> None:
        pass

    def shutdown(self) -> None:
        """Called when the window closes."""
        self.leave()

    def focus_item(self, kind: str, name: str) -> None:
        """Select an item found by the global search."""

    # ----------------------------------------------------------------- helpers

    def on_event(self, event_type, slot) -> None:
        self._regs.append(self.bridge.on(event_type, slot))

    def call(self, future, on_ok=None, on_err=None, error_title: str = ''):
        if on_err is None:
            title = error_title or self.title

            def on_err(exc, _t=title):
                self.error(_t, exc)
        return self.bridge.call(future, on_ok, on_err)

    def error(self, title: str, exc: Any, dialog: bool = True) -> None:
        self.ctx.status(f'{title}: {exc}', 8000)
        if dialog:
            show_error(self, title, exc)

    def message(self, text: str, ms: int = 5000) -> None:
        self.ctx.status(text, ms)

    def register_write_widget(self, *widgets: QWidget) -> None:
        self._write_widgets.extend(widgets)
        self._apply_read_only()

    def _apply_read_only(self) -> None:
        ro = self.rt.safety.read_only
        for w in self._write_widgets:
            w.setEnabled(not ro)
            if ro:
                w.setToolTip('Disabled in read-only mode')

    def can_write(self) -> bool:
        if self.rt.safety.read_only:
            self.message('Read-only mode: action disabled')
            return False
        return True

    def confirm_action(self, title: str, text: str, details: str = '',
                       low_risk: bool = False) -> bool:
        """Confirmation for actions that change a real system (design v2, T5)."""
        if not self.can_write():
            return False
        cfg = self.rt.config
        if not cfg.get('confirm_actions', True):
            return True
        if low_risk and not cfg.get('confirm_low_risk_params', False):
            return True
        return confirm(self, title, text, details)

    def setting(self, key: str, default=None):
        v = self.ctx.settings.value(f'{self.page_id}/{key}', default)
        if isinstance(default, bool) and isinstance(v, str):
            return v.lower() == 'true'
        if isinstance(default, (int, float)) and isinstance(v, str):
            try:
                return type(default)(v)
            except ValueError:
                return default
        return v

    def set_setting(self, key: str, value) -> None:
        self.ctx.settings.setValue(f'{self.page_id}/{key}', value)


def page_header(title: str, subtitle: str = '') -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 4)
    t = QLabel(title)
    t.setObjectName('pageTitle')
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setWordWrap(True)
        s.setStyleSheet('color: gray;')
        lay.addWidget(s)
    return w

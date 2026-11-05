"""Light and dark themes (Fusion style + palette + a small stylesheet)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

# Status colors shared by all pages (work on both themes).
COLORS = {
    'ok': '#2e9d4f',
    'warn': '#d99a00',
    'error': '#d64541',
    'stale': '#8a8a8a',
    'info': '#3a7bd5',
    'modified_bg': '#fff1b8',
    'pending_bg': '#d6e8ff',
    'rejected_bg': '#ffd6d6',
    'external_bg': '#ffe0b8',
    'stale_bg': '#e6e6e6',
}
DARK_BG = {
    'modified_bg': '#6b5a12',
    'pending_bg': '#1f3d66',
    'rejected_bg': '#6b2222',
    'external_bg': '#6b4312',
    'stale_bg': '#3a3a3a',
}

_current = 'light'


def color(name: str) -> QColor:
    if _current == 'dark' and name in DARK_BG:
        return QColor(DARK_BG[name])
    return QColor(COLORS[name])


def current() -> str:
    return _current


_STYLE = """
QListWidget#sidebar { border: none; font-size: 13px; }
QListWidget#sidebar::item { padding: 8px 10px; }
QLabel#pageTitle { font-size: 16px; font-weight: 600; }
QLabel#bigValue { font-family: monospace; font-size: 15px; }
QPlainTextEdit#terminal { font-family: monospace; }
QLabel#recIndicator { color: white; background: #d64541; border-radius: 4px; padding: 2px 8px;
                      font-weight: 700; }
QLabel#readOnlyBadge { color: white; background: #d99a00; border-radius: 4px; padding: 1px 6px; }
"""


def apply_theme(app: QApplication, name: str = 'light') -> None:
    global _current
    _current = 'dark' if name == 'dark' else 'light'
    app.setStyle(QStyleFactory.create('Fusion'))
    if _current == 'dark':
        p = QPalette()
        base, alt, text = QColor(42, 42, 46), QColor(53, 53, 58), QColor(225, 225, 225)
        p.setColor(QPalette.Window, alt)
        p.setColor(QPalette.WindowText, text)
        p.setColor(QPalette.Base, base)
        p.setColor(QPalette.AlternateBase, alt)
        p.setColor(QPalette.ToolTipBase, alt)
        p.setColor(QPalette.ToolTipText, text)
        p.setColor(QPalette.Text, text)
        p.setColor(QPalette.Button, alt)
        p.setColor(QPalette.ButtonText, text)
        p.setColor(QPalette.BrightText, QColor('red'))
        p.setColor(QPalette.Highlight, QColor(58, 123, 213))
        p.setColor(QPalette.HighlightedText, QColor('white'))
        p.setColor(QPalette.Link, QColor(110, 160, 240))
        p.setColor(QPalette.PlaceholderText, QColor(140, 140, 140))
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            p.setColor(QPalette.Disabled, role, QColor(120, 120, 120))
        app.setPalette(p)
    else:
        app.setPalette(app.style().standardPalette())
    app.setStyleSheet(_STYLE)

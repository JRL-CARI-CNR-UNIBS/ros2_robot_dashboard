"""MainWindow: sidebar + QStackedWidget, status bar, global search, read-only, theme."""

from __future__ import annotations

import importlib
import time
from typing import Optional

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QStyle, QTextEdit, QVBoxLayout, QWidget)

from ...core.domain.events import CoreError, CoreStatus, GraphChanged
from . import theme
from .pages.base_page import BasePage, PageContext
from .qt_bridge import QtBridge
from .widgets import common

# (page id, title, module, class, icon) in the order of design v2, section 10.
PAGES = [
    ('params', 'Parameters', 'params_page', 'ParamsPage', 'SP_FileDialogDetailedView'),
    ('topics', 'Topics', 'topics_page', 'TopicsPage', 'SP_FileDialogListView'),
    ('services', 'Services / Actions', 'services_actions_page', 'ServicesActionsPage',
     'SP_CommandLink'),
    ('graph', 'Graph', 'graph_page', 'GraphPage', 'SP_DriveNetIcon'),
    ('tf', 'TF', 'tf_page', 'TfPage', 'SP_DirLinkIcon'),
    ('robot', 'Robot', 'robot_page', 'RobotPage', 'SP_ComputerIcon'),
    ('controllers', 'Controllers', 'controllers_page', 'ControllersPage', 'SP_MediaPlay'),
    ('diagnostics', 'Diagnostics & logs', 'diagnostics_page', 'DiagnosticsPage',
     'SP_MessageBoxInformation'),
    ('launch', 'Launch', 'launch_page', 'LaunchPage', 'SP_MediaSeekForward'),
    ('bags', 'Bags', 'bag_page', 'BagPage', 'SP_DriveHDIcon'),
    ('health', 'Health', 'health_page', 'HealthPage', 'SP_DialogApplyButton'),
    ('utilities', 'Utilities', 'utilities_page', 'UtilitiesPage', 'SP_FileDialogContentsView'),
]

SEARCH_TARGET = {'node': 'graph', 'topic': 'topics', 'service': 'services', 'action': 'services',
                 'parameter': 'params', 'frame': 'tf'}


class MainWindow(QMainWindow):
    def __init__(self, runtime, settings: Optional[QSettings] = None,
                 initial_page: str = '', parent=None):
        super().__init__(parent)
        self.rt = runtime
        self.settings = settings or QSettings('ros2_robot_dashboard', 'ros2_robot_dashboard')
        self.bridge = QtBridge(runtime, self)
        self.ctx = PageContext(self.bridge, self.settings, self.show_status, self.navigate)
        self._pages: dict[str, BasePage] = {}
        self._errors: list[CoreError] = []
        self._current: Optional[BasePage] = None
        self.page_errors: dict[str, BaseException] = {}
        self.setWindowTitle('ROS 2 Robot Dashboard')
        self._build()
        self._build_menu()
        self.bridge.on(CoreStatus, self._on_core_status)
        self.bridge.on(GraphChanged,
                       lambda ev: self.nodes_label.setText(f'{len(ev.graph.nodes)} nodes'))
        self.bridge.core_error.connect(self._on_core_error)
        self.bridge.read_only_changed.connect(lambda _ro: self._update_read_only())
        geo = self.settings.value('window/geometry')
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1400, 860)
        start = initial_page or self.settings.value('window/page', 'params')
        ids = [p[0] for p in PAGES]
        self.sidebar.setCurrentRow(ids.index(start) if start in ids else 0)
        self.rt.publish_status()
        self.nodes_label.setText(f'{len(self.rt.graph.snapshot().nodes)} nodes')
        self._update_read_only()

    # ----------------------------------------------------------------- ui

    def _build(self) -> None:
        self.sidebar = QListWidget()
        self.sidebar.setObjectName('sidebar')
        self.sidebar.setIconSize(QSize(18, 18))
        self.sidebar.setFixedWidth(200)
        style = self.style()
        for pid, title, _m, _c, icon in PAGES:
            qicon = style.standardIcon(getattr(QStyle, icon, QStyle.SP_FileIcon))
            it = QListWidgetItem(qicon, title)
            it.setData(Qt.UserRole, pid)
            self.sidebar.addItem(it)
        self.stack = QStackedWidget()
        self._placeholders: dict[str, QWidget] = {}
        for pid, *_ in PAGES:
            ph = QWidget()
            self._placeholders[pid] = ph
            self.stack.addWidget(ph)
        self.sidebar.currentRowChanged.connect(self._show_row)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.sidebar)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        sb = self.statusBar()
        self.conn_label = QLabel('core: starting')
        self.domain_label = QLabel('')
        self.distro_label = QLabel('')
        self.nodes_label = QLabel('')
        self.ro_badge = QLabel('READ-ONLY')
        self.ro_badge.setObjectName('readOnlyBadge')
        self.error_btn = QPushButton('')
        self.error_btn.setFlat(True)
        self.error_btn.clicked.connect(self._show_errors)
        self.error_btn.setVisible(False)
        for w in (self.conn_label, self.domain_label, self.distro_label, self.nodes_label):
            sb.addPermanentWidget(w)
        sb.addPermanentWidget(self.error_btn)
        sb.addPermanentWidget(self.ro_badge)
        QShortcut(QKeySequence('Ctrl+K'), self, activated=self.open_search)
        QShortcut(QKeySequence('Ctrl+F'), self, activated=self.open_search)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu('&File')
        m.addAction('Quit', self.close, QKeySequence.Quit)
        v = mb.addMenu('&View')
        self.search_action = v.addAction('Global search…', self.open_search,
                                         QKeySequence('Ctrl+K'))
        self.dark_action = QAction('Dark theme', self, checkable=True)
        self.dark_action.setChecked(self.settings.value('window/theme', 'light') == 'dark')
        self.dark_action.toggled.connect(self._toggle_theme)
        v.addAction(self.dark_action)
        s = mb.addMenu('&Safety')
        self.ro_action = QAction('Read-only mode', self, checkable=True)
        self.ro_action.setChecked(self.rt.safety.read_only)
        self.ro_action.toggled.connect(self._set_read_only)
        s.addAction(self.ro_action)
        s.addAction('Show action log…', self._show_action_log)
        h = mb.addMenu('&Help')
        h.addAction('About', self._about)

    # ----------------------------------------------------------------- pages

    def page(self, pid: str) -> BasePage:
        """Create the page on first use (keeps startup fast)."""
        if pid in self._pages:
            return self._pages[pid]
        entry = next(p for p in PAGES if p[0] == pid)
        mod = importlib.import_module(f'.pages.{entry[2]}', __package__)
        page = getattr(mod, entry[3])(self.ctx)
        idx = self.stack.indexOf(self._placeholders[pid])
        self.stack.removeWidget(self._placeholders[pid])
        self.stack.insertWidget(idx, page)
        self._pages[pid] = page
        return page

    def _show_row(self, row: int) -> None:
        pid = PAGES[row][0]
        if self._current is not None:
            self._current.leave()
        try:
            page = self.page(pid)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.page_errors[pid] = exc
            self.show_status(f'Cannot open {pid}: {exc}', 10000)
            common.show_error(self, 'Page error', exc)
            return
        self.stack.setCurrentWidget(page)
        self._current = page
        page.enter()
        self.settings.setValue('window/page', pid)

    def navigate(self, pid: str, kind: str = '', name: str = '') -> None:
        ids = [p[0] for p in PAGES]
        if pid not in ids:
            return
        self.sidebar.setCurrentRow(ids.index(pid))
        if kind:
            self.page(pid).focus_item(kind, name)

    def current_page_id(self) -> str:
        return PAGES[self.sidebar.currentRow()][0]

    # ----------------------------------------------------------------- status bar

    def show_status(self, text: str, ms: int = 5000) -> None:
        self.statusBar().showMessage(text, ms)

    def _on_core_status(self, ev: CoreStatus) -> None:
        self.conn_label.setText('core: running' if ev.running else 'core: stopped')
        self.domain_label.setText(f'ROS_DOMAIN_ID {ev.domain_id}')
        self.distro_label.setText(f'distro {ev.distro}')

    def _on_core_error(self, ev: CoreError) -> None:
        self._errors.append(ev)
        self._errors = self._errors[-200:]
        self.error_btn.setText(f'⚠ {ev.source}: {ev.message[:60]}')
        self.error_btn.setToolTip('Last core error (click for the list)')
        self.error_btn.setVisible(True)

    def _show_errors(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle('Core errors')
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText('\n\n'.join(
            f'{time.strftime("%H:%M:%S", time.localtime(e.stamp))} [{e.source}] {e.message}\n'
            f'{e.detail}' for e in reversed(self._errors)))
        clear = QPushButton('Clear')
        clear.clicked.connect(lambda: (self._errors.clear(), self.error_btn.setVisible(False),
                                       dlg.accept()))
        lay = QVBoxLayout(dlg)
        lay.addWidget(text)
        lay.addWidget(clear)
        dlg.resize(800, 500)
        dlg.exec()

    def _set_read_only(self, value: bool) -> None:
        self.rt.safety.set_read_only(value)

    def _update_read_only(self) -> None:
        ro = self.rt.safety.read_only
        self.ro_badge.setVisible(ro)
        if self.ro_action.isChecked() != ro:
            self.ro_action.setChecked(ro)

    def _toggle_theme(self, dark: bool) -> None:
        theme.apply_theme(QApplication.instance(), 'dark' if dark else 'light')
        self.settings.setValue('window/theme', 'dark' if dark else 'light')

    def _show_action_log(self) -> None:
        import os
        from ...core.config import state_dir
        path = os.path.join(state_dir(), 'actions.log')
        dlg = QDialog(self)
        dlg.setWindowTitle(f'Action log — {path}')
        text = QTextEdit()
        text.setReadOnly(True)
        try:
            with open(path, encoding='utf-8') as f:
                text.setPlainText(''.join(f.readlines()[-2000:]))
        except OSError:
            text.setPlainText('(no actions logged yet)')
        lay = QVBoxLayout(dlg)
        lay.addWidget(text)
        dlg.resize(900, 500)
        dlg.exec()

    def _about(self) -> None:
        from ... import __version__
        QMessageBox.about(self, 'About', f'<b>ROS 2 Robot Dashboard</b> {__version__}<br>'
                          f'distro: {self.rt.compat.distro()}<br>'
                          'Apache License 2.0<br><br>'
                          'LLM tools, including Claude, have been used to help write the code. '
                          'All code was reviewed by humans, and the authors take full '
                          'responsibility for its correctness.')

    # ----------------------------------------------------------------- search

    def open_search(self) -> None:
        dlg = SearchDialog(self)
        dlg.exec()

    # ----------------------------------------------------------------- close

    def closeEvent(self, event) -> None:
        running = []
        if self.rt.has_service('launch'):
            running += [f'launch: {p}' for p in self.rt.launch.running()]
        if self.rt.has_service('bags'):
            running += [f'bag: {h.process_id}' for h in self.rt.bags.runner.active()]
        if running:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle('Processes still running')
            box.setText('These processes are still running:\n  ' + '\n  '.join(running))
            leave = box.addButton('Leave them running', QMessageBox.AcceptRole)
            stop = box.addButton('Stop them', QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is stop:
                self.show_status('Stopping processes…', 0)
                QApplication.processEvents()
                if self.rt.has_service('launch'):
                    self.rt.launch.stop_all()
                if self.rt.has_service('bags'):
                    self.rt.bags.runner.stop_all()
            elif clicked is not leave:
                event.ignore()
                return
        for p in self._pages.values():
            try:
                p.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.settings.setValue('window/geometry', self.saveGeometry())
        self.bridge.close()
        event.accept()


class SearchDialog(QDialog):
    """One box that searches nodes, topics, services, actions, parameters, frames."""

    def __init__(self, window: MainWindow):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle('Global search')
        self.edit = QLineEdit()
        self.edit.setPlaceholderText('Search nodes, topics, services, actions, parameters, '
                                     'TF frames…')
        self.results = QListWidget()
        self.hint = QLabel('Parameters are searched in the nodes already opened in the '
                           'Parameters page; TF frames once the TF page has been opened.')
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color: gray;')
        lay = QVBoxLayout(self)
        lay.addWidget(self.edit)
        lay.addWidget(self.results, 1)
        lay.addWidget(self.hint)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._search)
        self.edit.textChanged.connect(lambda _t: self._timer.start(150))
        self.edit.returnPressed.connect(self._open_first)
        self.results.itemActivated.connect(self._open)
        self.resize(640, 480)

    def _search(self) -> None:
        self.results.clear()
        for r in self.window_.rt.utility.search(self.edit.text()):
            text = f'[{r.kind}] {r.name}' + (f'   — {r.detail}' if r.detail else '')
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, r)
            self.results.addItem(it)

    def _open_first(self) -> None:
        if self.results.count():
            self._open(self.results.item(0))

    def _open(self, item) -> None:
        r = item.data(Qt.UserRole)
        target = SEARCH_TARGET.get(r.kind, 'graph')
        name = f'{r.node}|{r.name}' if r.kind == 'parameter' else r.name
        self.accept()
        self.window_.navigate(target, r.kind, name)

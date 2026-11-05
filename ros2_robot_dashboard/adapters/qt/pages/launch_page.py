"""Launch page: profiles with typed arguments, presets, start/stop, live output."""

from __future__ import annotations

import re
import shlex
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QScrollArea, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget)

from ....core.domain.events import ProcessOutput, ProcessStateChanged, ProfilesChanged
from ....core.domain.process import (ArgType, LaunchArg, ProcessProfile, ProcessState,
                                     StopPolicy)
from ....core.processes.launch_args import evaluate_condition
from .. import theme
from ..widgets.common import button, confirm, copy_to_clipboard, info
from ..widgets.terminal import TerminalWidget
from .base_page import BasePage

STATE_COLOR = {ProcessState.RUNNING: 'ok', ProcessState.STARTING: 'warn',
               ProcessState.STOPPING: 'warn', ProcessState.FAILED: 'error',
               ProcessState.EXITED: 'stale', ProcessState.STOPPED: 'stale'}


class ArgsForm(QWidget):
    """One row per argument: a "pass" checkbox with the name, and a value widget.

    Unticked arguments are not passed, so the launch file uses its own default
    (``use_default: true`` in the profile unticks them initially). ``enabled_if`` is
    re-evaluated at every change.
    """

    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self.form = QFormLayout(self)
        self.form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.widgets: dict[str, QWidget] = {}
        self.passed: dict[str, QCheckBox] = {}
        self.profile: Optional[ProcessProfile] = None

    def set_profile(self, profile: Optional[ProcessProfile], values: dict) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.widgets = {}
        self.passed = {}
        self.profile = profile
        if profile is None:
            return
        if not profile.args:
            self.form.addRow(QLabel('This profile has no arguments.'))
        else:
            hint = QLabel('Unticked arguments are not passed: the launch file uses its default.')
            hint.setObjectName('hint')
            self.form.addRow(hint)
        for a in profile.args:
            w = self._make(a)
            self.widgets[a.name] = w
            cb = QCheckBox(a.name + (' *' if a.required else ''))
            cb.toggled.connect(self._changed)
            self.passed[a.name] = cb
            default = '' if a.default is None else a.format_value(a.default)
            tip = (a.description + (f'\nenabled if: {a.enabled_if}' if a.enabled_if else '') +
                   f'\n\nTicked: passed as {a.name}:=<value>.\nUnticked: not passed, the '
                   f'launch file uses its own default' + (f' ({default})' if default else '') +
                   '.').strip()
            cb.setToolTip(tip)
            w.setToolTip(tip)
            self.form.addRow(cb, w)
        self.set_values(values)

    def _make(self, a: LaunchArg) -> QWidget:
        if a.type == ArgType.BOOL:
            w = QCheckBox()
            w.toggled.connect(self._changed)
        elif a.type == ArgType.INT:
            w = QSpinBox()
            w.setRange(-2 ** 31, 2 ** 31 - 1)
            w.valueChanged.connect(self._changed)
        elif a.type == ArgType.FLOAT:
            w = QDoubleSpinBox()
            w.setRange(-1e12, 1e12)
            w.setDecimals(4)
            w.valueChanged.connect(self._changed)
        elif a.type == ArgType.CHOICE:
            w = QComboBox()
            w.addItems(list(a.choices))
            w.setEditable(False)
            w.currentTextChanged.connect(self._changed)
        else:
            w = QLineEdit()
            w.setPlaceholderText('(empty: not passed)' if a.default in (None, '') else '')
            w.textChanged.connect(self._changed)
        return w

    def set_values(self, values: dict) -> None:
        """Show ``values`` (a preset); missing names get the profile default state.

        A value of ``None`` means "not passed".
        """
        if self.profile is None:
            return
        for a in self.profile.args:
            w = self.widgets[a.name]
            if a.name in values:
                passed = values[a.name] is not None
                v = values[a.name] if passed else a.default
            else:
                passed = not a.use_default
                v = a.default
            cb = self.passed[a.name]
            cb.blockSignals(True)
            cb.setChecked(passed)
            cb.blockSignals(False)
            w.blockSignals(True)
            try:
                v = a.parse(v) if v is not None else None
            except (ValueError, TypeError):
                pass
            if isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QSpinBox):
                w.setValue(int(v or 0))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(float(v or 0.0))
            elif isinstance(w, QComboBox):
                idx = w.findText(str(v)) if v is not None else -1
                w.setCurrentIndex(max(idx, 0))
            else:
                w.setText('' if v is None else a.format_value(v))
            w.blockSignals(False)
        self._update_enabled()

    def _widget_value(self, w: QWidget):
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return w.value()
        if isinstance(w, QComboBox):
            return w.currentText()
        return w.text()

    def values(self) -> dict:
        """Current values; ``None`` for arguments that are not passed."""
        if self.profile is None:
            return {}
        return {a.name: (self._widget_value(self.widgets[a.name])
                         if self.passed[a.name].isChecked() else None)
                for a in self.profile.args}

    def enabled_values(self) -> dict:
        """Like :meth:`values`, with ``None`` also for arguments disabled by ``enabled_if``."""
        vals = self.values()
        if self.profile is None:
            return vals
        shown = self._shown_values()
        return {a.name: (vals[a.name] if not a.enabled_if or
                         evaluate_condition(a.enabled_if, shown, self.profile) else None)
                for a in self.profile.args}

    def _shown_values(self) -> dict:
        """Values the launch file will see (defaults for arguments not passed)."""
        out = {}
        for a in self.profile.args:
            if self.passed[a.name].isChecked():
                out[a.name] = self._widget_value(self.widgets[a.name])
            else:
                out[a.name] = a.default
        return out

    def _changed(self, *_a) -> None:
        self._update_enabled()
        self._on_change()

    def _update_enabled(self) -> None:
        if self.profile is None:
            return
        shown = self._shown_values()
        for a in self.profile.args:
            on = not a.enabled_if or evaluate_condition(a.enabled_if, shown, self.profile)
            self.passed[a.name].setEnabled(on)
            self.widgets[a.name].setEnabled(on and self.passed[a.name].isChecked())


class NewProfileDialog(QDialog):
    """Create a profile from a command; arguments imported with ``--show-args``."""

    def __init__(self, page: 'LaunchPage', parent=None):
        super().__init__(parent or page)
        self.page = page
        self.setWindowTitle('New launch profile')
        self.pid = QLineEdit()
        self.pid.setPlaceholderText('my_robot')
        self.name = QLineEdit()
        self.command = QLineEdit()
        self.command.setPlaceholderText('ros2 launch my_pkg bringup.launch.py')
        self.source = QLineEdit()
        self.source.setPlaceholderText('optional: setup.bash to source first')
        self.desc = QLineEdit()
        self.import_args = QCheckBox('Import arguments with --show-args')
        self.import_args.setChecked(True)
        self.status = QLabel('')
        form = QFormLayout(self)
        form.addRow('Id', self.pid)
        form.addRow('Name', self.name)
        form.addRow('Command', self.command)
        form.addRow('Source', self.source)
        form.addRow('Description', self.desc)
        form.addRow('', self.import_args)
        form.addRow(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.resize(640, 260)

    def _save(self) -> None:
        pid = re.sub(r'[^A-Za-z0-9_]+', '_', self.pid.text().strip())
        cmd = self.command.text().strip()
        if not pid or not cmd:
            self.status.setText('Id and command are required')
            return
        prof = ProcessProfile(pid, self.name.text().strip() or pid, cmd, self.desc.text().strip(),
                              source=self.source.text().strip(),
                              stop=StopPolicy(float(self.page.rt.config.get(
                                  'launch.sigint_timeout_s', 10.0)), float(
                                  self.page.rt.config.get('launch.sigterm_timeout_s', 5.0))))
        if not self.import_args.isChecked() or not cmd.startswith('ros2 launch'):
            self._write(prof)
            return
        self.status.setText('reading arguments (--show-args)…')

        def ok(args):
            self._write(ProcessProfile(prof.id, prof.name, prof.command, prof.description,
                                       source=prof.source, args=tuple(args), stop=prof.stop))

        def err(exc):
            self.status.setText(f'--show-args failed: {exc}. Saved without arguments.')
            self._write(prof, close=False)

        self.page.bridge.call(self.page.rt.launch.show_args(cmd, prof.source), ok, err)

    def _write(self, prof: ProcessProfile, close: bool = True) -> None:
        path = self.page.rt.launch.save_profile(prof)
        self.page.message(f'Profile saved to {path}')
        self.page.select_profile(prof.id)
        if close:
            self.accept()


class LaunchPage(BasePage):
    page_id = 'launch'
    title = 'Launch'
    writes = True

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.svc = self.rt.launch
        self.current = ''
        self._build()
        self.on_event(ProfilesChanged, lambda _e: self._fill_profiles())
        self.on_event(ProcessStateChanged, self._on_state)
        self.on_event(ProcessOutput, self._on_output)
        self._uptime = QTimer(self)
        self._uptime.timeout.connect(self._update_header)
        self._uptime.start(1000)

    def _build(self) -> None:
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._profile_selected)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        new_btn = button('New…', lambda: NewProfileDialog(self).exec())
        reload_btn = button('Reload', self.svc.reload, 'Read the profile files again')
        self.scenario_combo = QComboBox()
        self.scenario_btn = button('Run scenario', self._run_scenario)
        self.errors_label = QLabel('')
        self.errors_label.setWordWrap(True)
        self.errors_label.setStyleSheet(f'color: {theme.COLORS["error"]};')
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('Profiles'))
        ll.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(new_btn)
        row.addWidget(reload_btn)
        ll.addLayout(row)
        ll.addWidget(QLabel('Scenarios'))
        row = QHBoxLayout()
        row.addWidget(self.scenario_combo, 1)
        row.addWidget(self.scenario_btn)
        ll.addLayout(row)
        ll.addWidget(self.errors_label)

        self.header = QLabel('Select a profile')
        self.header.setObjectName('pageTitle')
        self.state_label = QLabel('')
        self.desc = QLabel('')
        self.desc.setWordWrap(True)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.activated.connect(self._apply_preset)
        save_preset = button('Save preset…', self._save_preset)
        defaults = button('Defaults', self._defaults)
        self.form = ArgsForm(self._update_preview)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        self.preview = QLineEdit()
        self.preview.setReadOnly(True)
        self.preview.setObjectName('terminal')
        copy_btn = button('Copy', lambda: copy_to_clipboard(self.preview.text()))
        self.start_btn = button('Start', self._start)
        self.stop_btn = button('Stop', self._stop)
        self.restart_btn = button('Restart', self._restart)
        self.kill_btn = button('Kill leftovers', self._kill_leftovers,
                               'Kill processes of this profile that survived the stop')
        self.kill_btn.setVisible(False)
        self.register_write_widget(self.start_btn, self.stop_btn, self.restart_btn,
                                   self.scenario_btn)
        self.terminal = TerminalWidget(int(self.rt.config.get('launch.output_max_lines', 20000)))
        right_top = QWidget()
        rl = QVBoxLayout(right_top)
        rl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.header)
        row.addStretch(1)
        row.addWidget(self.state_label)
        rl.addLayout(row)
        rl.addWidget(self.desc)
        row = QHBoxLayout()
        for w in (QLabel('Preset'), self.preset_combo, save_preset, defaults):
            row.addWidget(w)
        row.addStretch(1)
        rl.addLayout(row)
        rl.addWidget(scroll, 1)
        row = QHBoxLayout()
        row.addWidget(self.preview, 1)
        row.addWidget(copy_btn)
        rl.addLayout(row)
        row = QHBoxLayout()
        for w in (self.start_btn, self.stop_btn, self.restart_btn, self.kill_btn):
            row.addWidget(w)
        row.addStretch(1)
        rl.addLayout(row)
        right = QSplitter(Qt.Vertical)
        right.addWidget(right_top)
        right.addWidget(self.terminal)
        right.setSizes([380, 420])
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([280, 1000])
        lay = QVBoxLayout(self)
        lay.addWidget(split)

    # ----------------------------------------------------------------- profiles

    def on_enter(self) -> None:
        self._fill_profiles()

    def _fill_profiles(self) -> None:
        cur = self.current
        self.list.blockSignals(True)
        self.list.clear()
        for p in sorted(self.svc.profiles(), key=lambda p: p.name.lower()):
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, p.id)
            it.setToolTip(f'{p.command}\n{p.origin}')
            self.list.addItem(it)
            self._decorate(it, self.svc.status(p.id).state)
        self.list.blockSignals(False)
        self.scenario_combo.clear()
        for s in self.svc.scenarios():
            self.scenario_combo.addItem(s.name, s.id)
        self.scenario_btn.setEnabled(self.scenario_combo.count() > 0 and
                                     not self.rt.safety.read_only)
        errs = self.svc.errors()
        self.errors_label.setText('\n'.join(errs[:10]))
        target = cur or self.setting('profile', '')
        self.select_profile(target) if target else None
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)

    def select_profile(self, pid: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == pid:
                if self.list.currentRow() == i:
                    # No currentItemChanged signal: reload the form explicitly.
                    self.current = ''
                    self._profile_selected(self.list.item(i))
                else:
                    self.list.setCurrentRow(i)
                return

    def _decorate(self, it: QListWidgetItem, state: ProcessState) -> None:
        it.setForeground(theme.color(STATE_COLOR[state]) if state.is_active or
                         state == ProcessState.FAILED else self.palette().text().color())
        name = it.text().split('  [')[0]
        it.setText(name + (f'  [{state.value}]' if state != ProcessState.STOPPED else ''))

    def _profile_selected(self, item, _prev=None) -> None:
        if item is None:
            return
        pid = item.data(Qt.UserRole)
        if pid == self.current:
            return
        self.current = pid
        self.set_setting('profile', pid)
        p = self.svc.profile(pid)
        self.desc.setText(p.description)
        self.form.set_profile(p, self.svc.last_values(pid))
        self.preset_combo.clear()
        self.preset_combo.addItem('(choose)')
        for pr in p.presets:
            self.preset_combo.addItem(pr.name)
        self.terminal.clear()
        for stream, line in self.svc.output(pid):
            self.terminal.append(line if stream != 'stderr' else line)
        self._update_preview()
        self._update_header()

    def _update_preview(self) -> None:
        if not self.current:
            return
        try:
            argv = self.svc.command_preview(self.current, self.form.enabled_values())
            self.preview.setText(shlex.join(argv))
            self.preview.setCursorPosition(0)
        except Exception as exc:  # noqa: BLE001
            self.preview.setText(f'error: {exc}')

    def _update_header(self) -> None:
        if not self.current:
            return
        try:
            p = self.svc.profile(self.current)
        except KeyError:
            return
        st = self.svc.status(self.current)
        self.header.setText(p.name)
        txt = st.state.value
        if st.pid:
            txt += f'  pid {st.pid}'
        if st.state == ProcessState.RUNNING and st.started_at:
            up = int(time.time() - st.started_at)
            txt += f'  up {up // 3600:d}:{up % 3600 // 60:02d}:{up % 60:02d}'
        if st.exit_code is not None and not st.state.is_active:
            txt += f'  exit code {st.exit_code}'
        if st.message:
            txt += f'  — {st.message}'
        c = theme.color(STATE_COLOR[st.state]).name()
        self.state_label.setText(f'<span style="color:{c}">●</span> {txt}')
        active = st.state.is_active
        ro = self.rt.safety.read_only
        self.start_btn.setEnabled(not active and not ro)
        self.stop_btn.setEnabled(active and not ro)
        self.restart_btn.setEnabled(not ro)
        self.kill_btn.setVisible(bool(st.leftover_pids))
        self.kill_btn.setText(f'Kill leftovers ({len(st.leftover_pids)})')

    # ----------------------------------------------------------------- presets

    def _apply_preset(self, idx: int) -> None:
        if idx <= 0 or not self.current:
            return
        p = self.svc.profile(self.current)
        pr = p.presets[idx - 1]
        self.form.set_values(dict(pr.values))
        self._update_preview()

    def _defaults(self) -> None:
        if self.current:
            self.form.set_values({})
            self._update_preview()

    def _save_preset(self) -> None:
        if not self.current:
            return
        name, ok = QInputDialog.getText(self, 'Save preset', 'Preset name')
        if not ok or not name.strip():
            return
        try:
            path = self.svc.save_preset(self.current, name.strip(), self.form.values())
        except Exception as exc:  # noqa: BLE001
            self.error('Save preset', exc)
            return
        self.message(f'Preset saved to {path}')

    # ----------------------------------------------------------------- processes

    def _start(self) -> None:
        if not self.current or not self.can_write():
            return
        p = self.svc.profile(self.current)
        missing = [a.name for a in p.args if a.required and
                   self.form.enabled_values().get(a.name) in (None, '')]
        if missing:
            info(self, 'Missing arguments', 'Required: ' + ', '.join(missing))
            return
        if not self.confirm_action('Start', f'Start "{p.name}"?', self.preview.text(),
                                   low_risk=True):
            return
        self.terminal.clear()
        try:
            st = self.svc.start_profile(self.current, self.form.enabled_values())
        except Exception as exc:  # noqa: BLE001
            self.error('Start', exc)
            return
        if st.state == ProcessState.FAILED:
            self.error('Start', st.message or 'the process could not start')
        self._update_header()

    def _stop(self) -> None:
        if not self.current:
            return
        pid = self.current
        if not self.confirm_action('Stop', f'Stop "{self.svc.profile(pid).name}"?',
                                   'SIGINT, then SIGTERM and SIGKILL after the timeouts.',
                                   low_risk=True):
            return
        self.call(self.svc.stop_profile(pid), lambda _r: self._update_header(),
                  error_title='Stop')

    def _restart(self) -> None:
        if not self.current or not self.can_write():
            return
        if not self.confirm_action('Restart', f'Restart "{self.svc.profile(self.current).name}"?',
                                   self.preview.text(), low_risk=True):
            return
        self.call(self.svc.restart_profile(self.current, self.form.enabled_values()),
                  lambda _r: self._update_header(), error_title='Restart')

    def _kill_leftovers(self) -> None:
        if not self.current:
            return
        st = self.svc.status(self.current)
        if not confirm(self, 'Kill leftovers', 'Send SIGKILL to these processes?\n' +
                       ', '.join(str(p) for p in st.leftover_pids)):
            return
        killed = self.svc.kill_leftovers(self.current)
        self.message(f'Killed {len(killed)} process(es)')
        self._update_header()

    def _run_scenario(self) -> None:
        sid = self.scenario_combo.currentData()
        if not sid or not self.confirm_action('Scenario', f'Run scenario '
                                              f'"{self.scenario_combo.currentText()}"?'):
            return
        self.call(self.svc.run_scenario(sid), lambda _r: self.message('Scenario started'),
                  error_title='Scenario')

    def _on_state(self, ev: ProcessStateChanged) -> None:
        st = ev.status
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) == st.process_id:
                self._decorate(it, st.state)
        if st.process_id == self.current:
            self._update_header()

    def _on_output(self, ev: ProcessOutput) -> None:
        if ev.process_id == self.current:
            self.terminal.append_lines([line for _s, line in ev.lines])

    def _list_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        p = self.svc.profile(it.data(Qt.UserRole))
        menu = QMenu(self)
        menu.addAction('Copy command', lambda: copy_to_clipboard(
            shlex.join(self.svc.command_preview(p.id, self.svc.last_values(p.id)))))
        menu.addAction(f'Profile file: {p.origin}', lambda: copy_to_clipboard(p.origin))
        menu.exec(self.list.viewport().mapToGlobal(pos))

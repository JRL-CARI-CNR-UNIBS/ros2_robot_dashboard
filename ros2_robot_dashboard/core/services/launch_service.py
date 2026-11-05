"""LaunchService: profiles from YAML, declared arguments, presets, start/stop/restart.

Profiles are read from ``$ROS_HOME/robot_dashboard/launch_profiles/*.yaml`` (shared,
copyable between lab PCs) plus the examples in the package ``config/launch_profiles``
and any directory listed in ``launch.profile_dirs``.
"""

from __future__ import annotations

import collections
import dataclasses
import glob
import os
import shlex
import subprocess
import threading
import time
from concurrent.futures import Future
from typing import Any, Optional

import yaml

from ..config import package_config_dir, user_dir
from ..domain.events import ProcessOutput, ProcessStateChanged, ProfilesChanged
from ..domain.process import (ArgType, LaunchArg, LaunchPreset, ProcessProfile, ProcessState,
                              ProcessStatus, Scenario, ScenarioStep, StopPolicy,
                              compose_launch_command)
from ..futures import try_set_exception, try_set_result
from ..processes.launch_args import check_condition, is_substitution, parse_show_args
from ..processes.process_runner import ProcessHandle, ProcessRunner
from ..runtime import BaseService


def profile_dirs(config) -> list[str]:
    dirs = [os.path.join(user_dir(), 'launch_profiles')]
    for d in config.get('launch.profile_dirs', []) or []:
        dirs.append(os.path.expanduser(d))
    pkg = package_config_dir()
    if pkg and config.get('launch.include_examples', True):
        dirs.append(os.path.join(pkg, 'launch_profiles'))
    return dirs


def parse_profiles_yaml(text: str, origin: str = '') -> tuple[list[ProcessProfile],
                                                              list[Scenario], list[str]]:
    data = yaml.safe_load(text) or {}
    errors: list[str] = []
    profiles: list[ProcessProfile] = []
    scenarios: list[Scenario] = []
    if not isinstance(data, dict):
        return [], [], [f'{origin}: top level must be a mapping']
    for i, p in enumerate(data.get('profiles', []) or []):
        try:
            profiles.append(_profile_from_dict(p, origin))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f'{origin}: profile #{i + 1}: {exc}')
    for i, s in enumerate(data.get('scenarios', []) or []):
        try:
            steps = []
            for st in s.get('steps', []) or []:
                wait = st.get('wait', {}) or {}
                steps.append(ScenarioStep(profile=st['profile'], preset=st.get('preset', ''),
                                          wait_node=wait.get('node', ''),
                                          wait_service=wait.get('service', ''),
                                          wait_s=float(wait.get('seconds', 0.0)),
                                          timeout_s=float(wait.get('timeout_s', 30.0))))
            scenarios.append(Scenario(id=str(s['id']), name=s.get('name', s['id']),
                                      steps=tuple(steps)))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f'{origin}: scenario #{i + 1}: {exc}')
    return profiles, scenarios, errors


def _profile_from_dict(p: dict, origin: str) -> ProcessProfile:
    if 'id' not in p or 'command' not in p:
        raise ValueError("'id' and 'command' are required")
    args = []
    for a in p.get('args', []) or []:
        atype = ArgType(str(a.get('type', 'string')).lower())
        arg = LaunchArg(name=str(a['name']), type=atype, default=a.get('default'),
                        description=a.get('description', ''),
                        choices=tuple(str(c) for c in a.get('choices', []) or []),
                        enabled_if=a.get('enabled_if', ''),
                        required=bool(a.get('required', False)),
                        # Launch substitutions cannot be passed as text: default to not passed.
                        use_default=bool(a.get('use_default', is_substitution(a.get('default')))))
        if arg.default is not None:
            arg = dataclasses.replace(arg, default=arg.parse(arg.default))
        args.append(arg)
    names = {a.name for a in args}
    for a in args:
        if a.enabled_if:
            err = check_condition(a.enabled_if, names)
            if err:
                raise ValueError(f"argument '{a.name}': enabled_if: {err}")
    presets = tuple(LaunchPreset(str(pr['name']), dict(pr.get('values', {}) or {}))
                    for pr in p.get('presets', []) or [])
    stop = p.get('stop', {}) or {}
    return ProcessProfile(
        id=str(p['id']), name=str(p.get('name', p['id'])), command=str(p['command']),
        description=p.get('description', ''), cwd=p.get('cwd', '') or '',
        env={str(k): str(v) for k, v in (p.get('env', {}) or {}).items()},
        source=p.get('source', '') or '', args=tuple(args), presets=presets,
        stop=StopPolicy(float(stop.get('sigint_timeout_s', 10.0)),
                        float(stop.get('sigterm_timeout_s', 5.0))),
        origin=origin)


def profile_to_dict(p: ProcessProfile) -> dict:
    d: dict[str, Any] = {'id': p.id, 'name': p.name, 'command': p.command}
    if p.description:
        d['description'] = p.description
    if p.cwd:
        d['cwd'] = p.cwd
    if p.env:
        d['env'] = dict(p.env)
    if p.source:
        d['source'] = p.source
    if p.args:
        d['args'] = []
        for a in p.args:
            ad: dict[str, Any] = {'name': a.name, 'type': a.type.value}
            if a.default is not None:
                ad['default'] = a.default
            if a.description:
                ad['description'] = a.description
            if a.choices:
                ad['choices'] = list(a.choices)
            if a.enabled_if:
                ad['enabled_if'] = a.enabled_if
            if a.required:
                ad['required'] = True
            if a.use_default:
                ad['use_default'] = True
            d['args'].append(ad)
    if p.presets:
        d['presets'] = [{'name': pr.name, 'values': dict(pr.values)} for pr in p.presets]
    d['stop'] = {'sigint_timeout_s': p.stop.sigint_timeout_s,
                 'sigterm_timeout_s': p.stop.sigterm_timeout_s}
    return d


class LaunchService(BaseService):
    name = 'launch'

    def __init__(self, rt):
        super().__init__(rt)
        self._profiles: dict[str, ProcessProfile] = {}
        self._scenarios: dict[str, Scenario] = {}
        self._errors: list[str] = []
        self._values: dict[str, dict] = {}              # last values used per profile
        self._output: dict[str, collections.deque] = {}
        self._pending: dict[str, list] = {}
        self._lock = threading.Lock()
        self.runner = ProcessRunner(self._on_output, self._on_state)
        self._timer = None

    def start(self) -> None:
        self.reload()
        self._timer = self.rt.create_timer(0.1, self._flush, 'launch.flush')

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)
        # Processes are left running unless the UI asked to stop them (design v2, 8.10).

    # ----------------------------------------------------------------- profiles

    def reload(self) -> None:
        profiles: dict[str, ProcessProfile] = {}
        scenarios: dict[str, Scenario] = {}
        errors: list[str] = []
        for d in profile_dirs(self.config):
            for path in sorted(glob.glob(os.path.join(d, '*.yaml')) + glob.glob(
                    os.path.join(d, '*.yml'))):
                try:
                    with open(path, encoding='utf-8') as f:
                        ps, ss, errs = parse_profiles_yaml(f.read(), path)
                except (OSError, yaml.YAMLError) as exc:
                    errors.append(f'{path}: {exc}')
                    continue
                errors += errs
                for p in ps:
                    if p.id in profiles:
                        # First directory wins (user files override the examples).
                        continue
                    profiles[p.id] = p
                for s in ss:
                    scenarios.setdefault(s.id, s)
        self._profiles, self._scenarios, self._errors = profiles, scenarios, errors
        self.publish(ProfilesChanged(profiles=tuple(profiles.values()),
                                     scenarios=tuple(scenarios.values()), errors=tuple(errors)))

    def profiles(self) -> list[ProcessProfile]:
        return list(self._profiles.values())

    def profile(self, profile_id: str) -> ProcessProfile:
        return self._profiles[profile_id]

    def scenarios(self) -> list[Scenario]:
        return list(self._scenarios.values())

    def errors(self) -> list[str]:
        return list(self._errors)

    def save_profile(self, profile: ProcessProfile, path: Optional[str] = None) -> str:
        """Write (or replace) a profile in a YAML file in the user profile directory."""
        if path is None:
            if profile.origin and os.access(profile.origin, os.W_OK) and \
                    os.path.abspath(profile.origin).startswith(os.path.abspath(user_dir())):
                path = profile.origin
            else:
                path = os.path.join(user_dir(), 'launch_profiles', f'{profile.id}.yaml')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data: dict = {'profiles': [], 'scenarios': []}
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                data = yaml.safe_load(f) or data
        items = [p for p in data.get('profiles', []) or [] if p.get('id') != profile.id]
        items.append(profile_to_dict(profile))
        data['profiles'] = items
        if not data.get('scenarios'):
            data.pop('scenarios', None)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        self.reload()
        return path

    def save_preset(self, profile_id: str, name: str, values: dict) -> str:
        p = self._profiles[profile_id]
        presets = [pr for pr in p.presets if pr.name != name] + [LaunchPreset(name, dict(values))]
        new = ProcessProfile(p.id, p.name, p.command, p.description, p.cwd, p.env, p.source,
                             p.args, tuple(presets), p.stop, p.origin)
        return self.save_profile(new)

    def show_args(self, command: str, source: str = '', timeout: float = 20.0) -> Future:
        """Run ``<command> --show-args`` in a worker; Future of list[LaunchArg]."""
        from ..processes.process_runner import wrap_with_source

        def run():
            argv = wrap_with_source(shlex.split(command) + ['--show-args'], source)
            res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if res.returncode != 0 and 'Arguments' not in res.stdout:
                raise RuntimeError(res.stderr.strip() or res.stdout.strip()
                                   or f'exit code {res.returncode}')
            return parse_show_args(res.stdout)

        return self.rt.submit(run)

    # ----------------------------------------------------------------- processes

    def command_preview(self, profile_id: str, values: Optional[dict] = None) -> list[str]:
        return compose_launch_command(self._profiles[profile_id], values or {})

    def start_profile(self, profile_id: str, values: Optional[dict] = None) -> ProcessStatus:
        p = self._profiles[profile_id]
        self.rt.safety.check_write('launch_start', profile_id)
        values = dict(values or {})
        argv = compose_launch_command(p, values)
        self._values[profile_id] = values
        with self._lock:
            self._output.setdefault(profile_id, collections.deque(
                maxlen=int(self.config.get('launch.output_max_lines', 20000)))).append(
                ('info', f'$ {shlex.join(argv)}'))
        h = self.runner.start(profile_id, argv, env=p.env, cwd=p.cwd or None, source=p.source)
        self.rt.safety.log('launch_start', profile_id, h.state != ProcessState.FAILED,
                           shlex.join(argv) + (f' -> {h.message}' if h.message else ''))
        return h.status()

    def stop_profile(self, profile_id: str) -> Future:
        p = self._profiles.get(profile_id)
        out: Future = Future()
        try:
            self.rt.safety.check_write('launch_stop', profile_id)
        except Exception as exc:  # noqa: BLE001
            out.set_exception(exc)
            return out
        stop = p.stop if p is not None else StopPolicy()
        fut = self.runner.stop(profile_id, stop.sigint_timeout_s, stop.sigterm_timeout_s)

        def done(f: Future) -> None:
            h = self.runner.handle(profile_id)
            self.rt.safety.log('launch_stop', profile_id, True,
                               h.message if h is not None else '')
            try_set_result(out, f.result())

        fut.add_done_callback(done)
        return out

    def restart_profile(self, profile_id: str, values: Optional[dict] = None) -> Future:
        """Stop (if running) and start again with ``values`` (or the last values)."""
        out: Future = Future()
        vals = dict(values if values is not None else self._values.get(profile_id, {}))

        def after_stop(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                try_set_exception(out, exc)
                return
            try:
                try_set_result(out, self.start_profile(profile_id, vals))
            except Exception as e:  # noqa: BLE001
                try_set_exception(out, e)

        self.stop_profile(profile_id).add_done_callback(after_stop)
        return out

    def status(self, profile_id: str) -> ProcessStatus:
        h = self.runner.handle(profile_id)
        if h is None:
            return ProcessStatus(profile_id, ProcessState.STOPPED)
        return h.status()

    def running(self) -> list[str]:
        return [h.process_id for h in self.runner.active()]

    def last_values(self, profile_id: str) -> dict:
        return dict(self._values.get(profile_id, {}))

    def output(self, profile_id: str) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._output.get(profile_id, ()))

    def clear_output(self, profile_id: str) -> None:
        with self._lock:
            self._output.pop(profile_id, None)

    def stop_all(self) -> None:
        self.runner.stop_all()

    def kill_leftovers(self, profile_id: str) -> tuple[int, ...]:
        return self.runner.kill_leftovers(profile_id)

    # ----------------------------------------------------------------- scenarios

    def run_scenario(self, scenario_id: str) -> Future:
        """Start the profiles of a scenario in order, waiting between steps."""
        sc = self._scenarios[scenario_id]
        out: Future = Future()

        def run() -> None:
            try:
                for step in sc.steps:
                    p = self._profiles[step.profile]
                    values = {}
                    if step.preset:
                        values = next((pr.values for pr in p.presets if pr.name == step.preset),
                                      None)
                        if values is None:
                            raise KeyError(f"preset '{step.preset}' not found in {p.id}")
                    st = self.start_profile(step.profile, values)
                    if st.state == ProcessState.FAILED:
                        raise RuntimeError(f'{p.id}: {st.message}')
                    self._wait_step(step)
                try_set_result(out, True)
            except Exception as exc:  # noqa: BLE001
                try_set_exception(out, exc)

        self.rt.submit(run)
        return out

    def _wait_step(self, step: ScenarioStep) -> None:
        if step.wait_s:
            time.sleep(step.wait_s)
        if not (step.wait_node or step.wait_service):
            return
        end = time.time() + step.timeout_s
        while time.time() < end:
            snap = self.rt.graph.poll()
            if step.wait_node and any(n.fqn == step.wait_node for n in snap.nodes):
                return
            if step.wait_service and any(s.name == step.wait_service for s in snap.services):
                return
            time.sleep(0.5)
        raise TimeoutError(f'waiting for {step.wait_node or step.wait_service}: timeout')

    # ----------------------------------------------------------------- callbacks

    def _on_output(self, handle: ProcessHandle, stream: str, line: str) -> None:
        with self._lock:
            self._output.setdefault(handle.process_id, collections.deque(
                maxlen=int(self.config.get('launch.output_max_lines', 20000)))).append(
                (stream, line))
            self._pending.setdefault(handle.process_id, []).append((stream, line))

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for pid, lines in pending.items():
            self.publish(ProcessOutput(process_id=pid, lines=tuple(lines)))

    def _on_state(self, handle: ProcessHandle) -> None:
        self.publish(ProcessStateChanged(status=handle.status()))

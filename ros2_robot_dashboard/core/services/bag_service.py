"""BagService: ``ros2 bag record`` / ``ros2 bag play`` as subprocesses, bag info."""

from __future__ import annotations

import itertools
import os
import shlex
import threading
import time
from concurrent.futures import Future
from typing import Optional

from ..compat.common import _dir_size
from ..domain.bag import BagInfo, BagPlayConfig, BagRecordConfig, RecordingStatus
from ..domain.events import ProcessOutput, ProcessStateChanged, RecordingStatusChanged
from ..domain.process import ProcessState
from ..futures import then
from ..processes.process_runner import ProcessHandle, ProcessRunner
from ..runtime import BaseService


class BagService(BaseService):
    name = 'bags'

    def __init__(self, rt):
        super().__init__(rt)
        self.runner = ProcessRunner(self._on_output, self._on_state)
        self._ids = itertools.count(1)
        self._recordings: dict[str, dict] = {}
        self._players: dict[str, BagPlayConfig] = {}
        self._pending: dict[str, list] = {}
        self._lock = threading.Lock()
        self._timer = None

    def start(self) -> None:
        self._timer = self.rt.create_timer(1.0, self._tick, 'bags.tick')

    def stop(self) -> None:
        self.rt.destroy_timer(self._timer)
        # Close recordings properly: SIGINT lets rosbag2 write metadata.yaml.
        for rid in list(self._recordings):
            if self.runner.handle(rid) and self.runner.handle(rid).state.is_active:
                self.runner.stop(rid, 10.0, 5.0)

    # ----------------------------------------------------------------- record

    def output_path(self, config: BagRecordConfig) -> str:
        base = os.path.expanduser(config.output_dir or self.config.get('bag.default_dir',
                                                                       '~/bags'))
        name = config.name.strip() or 'rosbag'
        if config.add_timestamp:
            name = f'{name}_{time.strftime("%Y%m%d_%H%M%S")}'
        return os.path.join(base, name)

    def record_command(self, config: BagRecordConfig, output: Optional[str] = None) -> list[str]:
        return self.rt.compat.bag_record_command(config, output or self.output_path(config))

    def record(self, config: BagRecordConfig) -> str:
        self.rt.safety.check_write('bag_record', config.name)
        if not config.all_topics and not config.topics and not config.include_regex:
            raise ValueError('select at least one topic (or "all topics")')
        output = self.output_path(config)
        if os.path.exists(output):
            raise FileExistsError(f'{output} already exists')
        os.makedirs(os.path.dirname(output), exist_ok=True)
        rid = f'bag-record-{next(self._ids)}'
        argv = self.record_command(config, output)
        with self._lock:
            self._recordings[rid] = {'config': config, 'output': output, 'started': time.time(),
                                     'info': None}
        h = self.runner.start(rid, argv)
        self.rt.safety.log('bag_record', output, h.state != ProcessState.FAILED, shlex.join(argv))
        if config.stop_after_s > 0:
            self.rt.call_later(config.stop_after_s, lambda: self.stop_record(rid))
        return rid

    def stop_record(self, rid: str) -> Future:
        fut = self.runner.stop(rid, 10.0, 5.0)
        return then(fut, lambda code: self._finish(rid))

    def _finish(self, rid: str) -> RecordingStatus:
        rec = self._recordings.get(rid)
        if rec is None:
            raise KeyError(rid)
        info = None
        msg = ''
        try:
            info = self.rt.compat.bag_info(rec['output'])
        except Exception as exc:  # noqa: BLE001
            msg = f'cannot read bag info: {exc}'
        rec['info'] = info
        st = self._status(rid, info=info, message=msg)
        self.publish(RecordingStatusChanged(status=st))
        return st

    def _status(self, rid: str, info: Optional[BagInfo] = None,
                message: str = '') -> RecordingStatus:
        rec = self._recordings[rid]
        h = self.runner.handle(rid)
        active = h is not None and h.state.is_active
        size = _dir_size(rec['output']) if os.path.exists(rec['output']) else 0
        if h is not None and h.message and not message:
            message = h.message
        counts = {t.name: t.count for t in info.topics} if info else {}
        elapsed = (time.time() if active else (rec.get('ended') or time.time())) - rec['started']
        return RecordingStatus(rid, rec['output'], elapsed, size, active, message, counts, info)

    def recordings(self) -> list[RecordingStatus]:
        return [self._status(r) for r in list(self._recordings)]

    def _tick(self) -> None:
        for rid in list(self._recordings):
            h = self.runner.handle(rid)
            if h is not None and h.state.is_active:
                self.publish(RecordingStatusChanged(status=self._status(rid)))

    # ----------------------------------------------------------------- existing bags

    def info(self, path: str) -> Future:
        return self.rt.submit(self.rt.compat.bag_info, path)

    def play_command(self, config: BagPlayConfig) -> list[str]:
        return self.rt.compat.bag_play_command(config)

    def play(self, config: BagPlayConfig) -> str:
        self.rt.safety.check_write('bag_play', config.path)
        pid = f'bag-play-{next(self._ids)}'
        argv = self.play_command(config)
        self._players[pid] = config
        h = self.runner.start(pid, argv)
        self.rt.safety.log('bag_play', config.path, h.state != ProcessState.FAILED,
                           shlex.join(argv))
        return pid

    def stop_play(self, pid: str) -> Future:
        return self.runner.stop(pid, 5.0, 3.0)

    def _player_service(self, srv: str) -> Future:
        """Call /rosbag2_player/<srv> (pause, resume, toggle_paused)."""
        try:
            import rosbag2_interfaces.srv as rs
        except ImportError:
            f: Future = Future()
            f.set_exception(RuntimeError('rosbag2_interfaces not available'))
            return f
        cls = {'pause': rs.Pause, 'resume': rs.Resume, 'toggle_paused': rs.TogglePaused}[srv]
        return self.rt.call_service(cls, f'/rosbag2_player/{srv}', cls.Request())

    def pause(self) -> Future:
        return self._player_service('pause')

    def resume(self) -> Future:
        return self._player_service('resume')

    # ----------------------------------------------------------------- callbacks

    def _on_output(self, handle: ProcessHandle, stream: str, line: str) -> None:
        with self._lock:
            self._pending.setdefault(handle.process_id, []).append((stream, line))
        # Output is rare (a few lines): publish immediately in small batches.
        self._flush(handle.process_id)

    def _flush(self, pid: str) -> None:
        with self._lock:
            lines = self._pending.pop(pid, [])
        if lines:
            self.publish(ProcessOutput(process_id=pid, lines=tuple(lines)))

    def _on_state(self, handle: ProcessHandle) -> None:
        rec = self._recordings.get(handle.process_id)
        if rec is not None and not handle.state.is_active:
            rec['ended'] = time.time()
            # Process exited by itself (duration limit, crash): read the bag.
            self.rt.submit(self._finish, handle.process_id)
        self.publish(ProcessStateChanged(status=handle.status()))

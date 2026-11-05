"""Run external commands (launch, bag) in their own process group.

Stop sequence: SIGINT to the group, wait, SIGTERM, wait, SIGKILL. After the main
process exits we check that no descendant is left behind and report the PIDs.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..domain.process import ProcessState, ProcessStatus

OutputCallback = Callable[['ProcessHandle', str, str], None]
StateCallback = Callable[['ProcessHandle'], None]


@dataclass(eq=False)
class ProcessHandle:
    process_id: str
    argv: tuple[str, ...]
    popen: Optional[subprocess.Popen] = None
    state: ProcessState = ProcessState.STOPPED
    exit_code: Optional[int] = None
    started_at: float = 0.0
    descendants: dict = field(default_factory=dict)   # pid -> create time
    leftover_pids: tuple[int, ...] = ()
    stop_requested: bool = False
    message: str = ''
    exited: threading.Event = field(default_factory=threading.Event)

    @property
    def pid(self) -> Optional[int]:
        return self.popen.pid if self.popen is not None else None

    def status(self) -> ProcessStatus:
        return ProcessStatus(self.process_id, self.state, self.pid, self.exit_code, self.argv,
                             self.started_at, self.leftover_pids, self.message)


def wrap_with_source(argv: Sequence[str], source: str) -> list[str]:
    """Run ``argv`` after sourcing a setup file, with ``exec`` so signals reach it."""
    if not source:
        return list(argv)
    path = os.path.expanduser(source)
    return ['bash', '-c', f'source {shlex.quote(path)} && exec "$@"', 'bash', *argv]


class ProcessRunner:
    def __init__(self, on_output: Optional[OutputCallback] = None,
                 on_state: Optional[StateCallback] = None):
        self._on_output = on_output
        self._on_state = on_state
        self._handles: dict[str, ProcessHandle] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- start

    def start(self, process_id: str, argv: Sequence[str], env: Optional[dict] = None,
              cwd: Optional[str] = None, source: str = '') -> ProcessHandle:
        with self._lock:
            old = self._handles.get(process_id)
            if old is not None and old.state.is_active:
                raise RuntimeError(f"process '{process_id}' is already running")
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in (env or {}).items()})
        full_env.setdefault('PYTHONUNBUFFERED', '1')
        full_env.setdefault('RCUTILS_LOGGING_BUFFERED_STREAM', '1')
        full_env.setdefault('RCUTILS_COLORIZED_OUTPUT', '0')
        cmd = wrap_with_source(argv, source)
        handle = ProcessHandle(process_id, tuple(cmd), state=ProcessState.STARTING,
                               started_at=time.time())
        with self._lock:
            self._handles[process_id] = handle
        self._emit_state(handle)
        try:
            handle.popen = subprocess.Popen(
                cmd, cwd=os.path.expanduser(cwd) if cwd else None, env=full_env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except (OSError, ValueError) as exc:
            handle.state = ProcessState.FAILED
            handle.message = str(exc)
            handle.exited.set()
            self._emit_state(handle)
            return handle
        handle.state = ProcessState.RUNNING
        self._emit_state(handle)
        readers = [
            threading.Thread(target=self._read, args=(handle, handle.popen.stdout, 'stdout'),
                             daemon=True, name=f'{process_id}-stdout'),
            threading.Thread(target=self._read, args=(handle, handle.popen.stderr, 'stderr'),
                             daemon=True, name=f'{process_id}-stderr'),
        ]
        for t in readers:
            t.start()
        threading.Thread(target=self._watch, args=(handle, readers), daemon=True,
                         name=f'{process_id}-watch').start()
        return handle

    def handle(self, process_id: str) -> Optional[ProcessHandle]:
        return self._handles.get(process_id)

    def handles(self) -> list[ProcessHandle]:
        with self._lock:
            return list(self._handles.values())

    def active(self) -> list[ProcessHandle]:
        return [h for h in self.handles() if h.state.is_active]

    # ----------------------------------------------------------------- io

    def _read(self, handle: ProcessHandle, stream, name: str) -> None:
        try:
            for raw in iter(stream.readline, b''):
                line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                if self._on_output is not None:
                    try:
                        self._on_output(handle, name, line)
                    except Exception:  # noqa: BLE001
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _watch(self, handle: ProcessHandle, readers) -> None:
        popen = handle.popen
        # Track descendants while running (to find leftovers that left the group).
        while True:
            try:
                code = popen.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                handle.descendants.update(_descendants(popen.pid))
        for t in readers:
            t.join(2.0)
        handle.exit_code = code
        if handle.stop_requested:
            # Children may still be handling the signal: give them a moment to exit.
            _wait_gone(lambda: self._leftovers(handle), 5.0)
        handle.leftover_pids = tuple(sorted(self._leftovers(handle)))
        if handle.leftover_pids:
            handle.message = (f'{len(handle.leftover_pids)} child process(es) still running: '
                              + ', '.join(f'{p} ({_cmd(p)})' for p in handle.leftover_pids))
        if handle.stop_requested or code == 0:
            handle.state = ProcessState.EXITED if not handle.stop_requested else (
                ProcessState.STOPPED)
        else:
            handle.state = ProcessState.EXITED if code is not None and code >= 0 else (
                ProcessState.FAILED)
            if code is not None and code != 0 and not handle.message:
                handle.message = f'exited with code {code}'
        handle.exited.set()
        self._emit_state(handle)

    def _leftovers(self, handle: ProcessHandle) -> set[int]:
        pgid = handle.pid
        alive = set()
        for pid, created in list(handle.descendants.items()):
            if _alive(pid, created):
                alive.add(pid)
        try:
            import psutil
            for p in psutil.process_iter(['pid']):
                try:
                    if os.getpgid(p.pid) == pgid and p.pid != pgid:
                        alive.add(p.pid)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
        except ImportError:
            pass
        return alive

    def _emit_state(self, handle: ProcessHandle) -> None:
        if self._on_state is not None:
            try:
                self._on_state(handle)
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- stop

    def stop(self, process_id: str, sigint_timeout: float = 10.0,
             sigterm_timeout: float = 5.0) -> Future:
        """Future of the exit code. Runs the escalation in a background thread."""
        out: Future = Future()
        handle = self._handles.get(process_id)
        if handle is None or handle.popen is None or not handle.state.is_active:
            out.set_result(handle.exit_code if handle else None)
            return out
        handle.stop_requested = True
        handle.state = ProcessState.STOPPING
        self._emit_state(handle)

        def run() -> None:
            pgid = handle.pid
            handle.descendants.update(_descendants(pgid))
            for sig, timeout in ((signal.SIGINT, sigint_timeout),
                                 (signal.SIGTERM, sigterm_timeout), (signal.SIGKILL, 3.0)):
                _killpg(pgid, sig)
                if handle.exited.wait(timeout):
                    break
            # Main process gone: make sure the rest of the group follows.
            if self._leftovers(handle):
                _killpg(pgid, signal.SIGTERM)
                if not _wait_gone(lambda: self._leftovers(handle), 1.0):
                    for pid in self._leftovers(handle):
                        _kill(pid, signal.SIGKILL)
                    _wait_gone(lambda: self._leftovers(handle), 2.0)
                handle.leftover_pids = tuple(sorted(self._leftovers(handle)))
                if handle.leftover_pids:
                    handle.message = ('could not stop: ' + ', '.join(
                        f'{p} ({_cmd(p)})' for p in handle.leftover_pids))
                else:
                    handle.message = 'leftover child processes were killed'
                self._emit_state(handle)
            out.set_result(handle.exit_code)

        threading.Thread(target=run, daemon=True, name=f'{process_id}-stop').start()
        return out

    def kill_leftovers(self, process_id: str) -> tuple[int, ...]:
        handle = self._handles.get(process_id)
        if handle is None:
            return ()
        for pid in self._leftovers(handle):
            _kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        handle.leftover_pids = tuple(sorted(self._leftovers(handle)))
        self._emit_state(handle)
        return handle.leftover_pids

    def stop_all(self, sigint_timeout: float = 10.0, sigterm_timeout: float = 5.0,
                 wait: bool = True) -> None:
        futs = [self.stop(h.process_id, sigint_timeout, sigterm_timeout) for h in self.active()]
        if wait:
            for f in futs:
                try:
                    f.result(timeout=sigint_timeout + sigterm_timeout + 5.0)
                except Exception:  # noqa: BLE001
                    pass

    def send_signal(self, process_id: str, sig: int) -> None:
        handle = self._handles.get(process_id)
        if handle is not None and handle.state.is_active and handle.pid:
            _killpg(handle.pid, sig)


def _killpg(pgid: Optional[int], sig: int) -> None:
    if not pgid:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_gone(get_pids, timeout: float) -> bool:
    """Poll until ``get_pids()`` is empty; True if it became empty in time."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not get_pids():
            return True
        time.sleep(0.1)
    return not get_pids()


def _cmd(pid: int) -> str:
    try:
        import psutil
        return ' '.join(psutil.Process(pid).cmdline())[:80] or '?'
    except Exception:  # noqa: BLE001
        return '?'


def _alive(pid: int, created: Optional[float] = None) -> bool:
    """True if ``pid`` runs and, when ``created`` is given, is still the same process."""
    try:
        import psutil
        p = psutil.Process(pid)
        if created is not None and abs(p.create_time() - created) > 0.01:
            return False                     # PID reused by an unrelated process
        return p.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except Exception:  # noqa: BLE001 - NoSuchProcess, AccessDenied
        return False


def _descendants(pid: Optional[int]) -> dict[int, Optional[float]]:
    """Children of ``pid`` (recursive) with their create time, to detect PID reuse."""
    if not pid:
        return {}
    try:
        import psutil
        out: dict[int, Optional[float]] = {}
        for c in psutil.Process(pid).children(recursive=True):
            try:
                out[c.pid] = c.create_time()
            except Exception:  # noqa: BLE001 - exited meanwhile
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}

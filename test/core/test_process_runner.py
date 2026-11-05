"""ProcessRunner: output, graceful stop, escalation, leftover children."""

import sys
import textwrap
import time

import pytest

from ros2_robot_dashboard.core.domain.process import ProcessState
from ros2_robot_dashboard.core.processes.process_runner import ProcessRunner, wrap_with_source

from conftest import wait_until


def _runner():
    out, states = [], []
    r = ProcessRunner(lambda h, s, line: out.append((s, line)),
                      lambda h: states.append(h.state))
    return r, out, states


def _script(tmp_path, body: str) -> str:
    p = tmp_path / 'proc.py'
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_output_and_exit_code(tmp_path):
    r, out, states = _runner()
    script = _script(tmp_path, """
        import sys
        print('hello'); print('err', file=sys.stderr); sys.exit(3)
    """)
    h = r.start('p', [sys.executable, script])
    assert h.exited.wait(10)
    assert ('stdout', 'hello') in out and ('stderr', 'err') in out
    assert h.exit_code == 3 and h.state == ProcessState.EXITED
    assert states[0] == ProcessState.STARTING and ProcessState.RUNNING in states


def test_graceful_sigint(tmp_path):
    r, out, _ = _runner()
    script = _script(tmp_path, """
        import signal, sys, time
        def stop(*a):
            print('got sigint', flush=True); sys.exit(0)
        signal.signal(signal.SIGINT, stop)
        print('ready', flush=True)
        while True: time.sleep(0.1)
    """)
    h = r.start('p', [sys.executable, script])
    assert wait_until(lambda: ('stdout', 'ready') in out, 10)
    t0 = time.time()
    r.stop('p', sigint_timeout=5, sigterm_timeout=2).result(15)
    assert time.time() - t0 < 4
    assert ('stdout', 'got sigint') in out
    assert h.state == ProcessState.STOPPED and h.exit_code == 0


def test_escalation_to_sigterm(tmp_path):
    r, out, _ = _runner()
    script = _script(tmp_path, """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print('ready', flush=True)
        while True: time.sleep(0.1)
    """)
    h = r.start('p', [sys.executable, script])
    assert wait_until(lambda: ('stdout', 'ready') in out, 10)
    r.stop('p', sigint_timeout=0.5, sigterm_timeout=3).result(15)
    assert h.state == ProcessState.STOPPED and h.exit_code == -15


def test_children_are_stopped_with_the_group(tmp_path):
    pytest.importorskip('psutil')
    r, out, _ = _runner()
    child = _script(tmp_path, """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(0.1)
    """)
    parent = tmp_path / 'parent.py'
    parent.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        c = subprocess.Popen([sys.executable, {child!r}])
        print('child', c.pid, flush=True)
        while True: time.sleep(0.1)
    """))
    h = r.start('p', [sys.executable, str(parent)])
    line = wait_until(lambda: next((ln for s, ln in out if ln.startswith('child')), None), 10)
    child_pid = int(line.split()[1])
    r.stop('p', sigint_timeout=0.5, sigterm_timeout=0.5).result(20)
    import psutil
    assert wait_until(lambda: not psutil.pid_exists(child_pid)
                      or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE, 5)
    assert h.leftover_pids == ()


def test_start_failure_and_double_start(tmp_path):
    r, _, _ = _runner()
    h = r.start('bad', ['/nonexistent/binary'])
    assert h.state == ProcessState.FAILED and h.message
    script = _script(tmp_path, 'import time\ntime.sleep(30)\n')
    r.start('p', [sys.executable, script])
    with pytest.raises(RuntimeError):
        r.start('p', [sys.executable, script])
    r.stop_all(1, 1)


def test_wrap_with_source(tmp_path):
    env_file = tmp_path / 'setup.sh'
    env_file.write_text('export DASH_TEST_VAR=sourced\n')
    r, out, _ = _runner()
    h = r.start('p', ['bash', '-c', 'echo $DASH_TEST_VAR'], source=str(env_file))
    assert h.exited.wait(10)
    assert ('stdout', 'sourced') in out
    assert wrap_with_source(['a'], '')[0] == 'a'

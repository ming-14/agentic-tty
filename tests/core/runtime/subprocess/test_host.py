from __future__ import annotations

import sys
import time

import pytest

from agentic_tty.core.ports import SUBPROCESS, SessionSpec
from agentic_tty.core.runtime.errors import HostSpawnError, MonitorUnavailable
from agentic_tty.core.runtime.subprocess.host import SubprocessHost


def _spec(argv: list[str]) -> SessionSpec:
    return SessionSpec(mode=SUBPROCESS, argv=argv)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _wait_exit(host: SubprocessHost, timeout: float) -> int | None:
    """带超时地等退出码。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = host.try_wait()
        if code is not None:
            return code
        time.sleep(0.01)
    return None


def test_reads_stdout_and_stderr_separately():
    host = SubprocessHost(_spec(_py("import sys;sys.stdout.write('out');sys.stderr.write('err')")))
    try:
        assert host.read(timeout=None) == b"out"
        assert host.read_stderr(timeout=None) == b"err"
    finally:
        host.kill()
        host.close()


def test_blocking_read_returns_eof_after_exit():
    host = SubprocessHost(_spec(_py("print('done')")))
    try:
        assert host.read(timeout=None).strip() == b"done"  # Windows 文本模式带 \r
        assert host.read(timeout=None) == b""  # EOF
        assert _wait_exit(host, 2.0) == 0
    finally:
        host.close()


def test_timed_read_returns_empty_without_data():
    host = SubprocessHost(_spec(_py("import time;time.sleep(5)")))
    try:
        assert host.read(timeout=0.05) == b""
    finally:
        host.kill()
        host.close()


def test_write_and_close_stdin():
    host = SubprocessHost(_spec(_py("import sys;sys.stdout.write(sys.stdin.read())")))
    try:
        host.write(b"hello stdin")
        host.close_stdin()
        assert host.read(timeout=None) == b"hello stdin"
    finally:
        host.close()


def test_nonzero_exit_code():
    host = SubprocessHost(_spec(_py("import sys;sys.exit(7)")))
    try:
        assert host.read(timeout=None) == b""
        assert _wait_exit(host, 2.0) == 7
    finally:
        host.close()


def test_kill_terminates_tree():
    host = SubprocessHost(_spec(_py("import time;time.sleep(30)")))
    try:
        assert host.try_wait() is None
        host.kill()
        assert _wait_exit(host, 3.0) is not None
    finally:
        host.close()


def test_env_is_passed_through():
    spec = SessionSpec(
        mode=SUBPROCESS,
        argv=_py("import os;print(os.environ.get('AGENTIC_TTY_TEST',''))"),
        env={"AGENTIC_TTY_TEST": "42"},
    )
    host = SubprocessHost(spec)
    try:
        assert host.read(timeout=None).strip() == b"42"  # Windows 文本模式会带 \r
    finally:
        host.close()


def test_spawn_failure_raises():
    with pytest.raises(HostSpawnError):
        SubprocessHost(_spec(["definitely-not-a-real-program-xyz"]))


def test_job_assignment_failure_falls_back_to_pid_termination(monkeypatch):
    """入作业失败后不能再拿作业当身份：空作业会让 kill 变成没杀、成员观测谎报空列表。"""
    if sys.platform != "win32":
        pytest.skip("作业对象是 Windows 专有路径")
    from agentic_tty.core.runtime.subprocess import host as module

    monkeypatch.setattr(module, "assign_job", lambda job, pid: False)
    host = SubprocessHost(_spec(_py("import time;time.sleep(30)")))
    try:
        with pytest.raises(MonitorUnavailable):
            host.descendants()  # 没有作业身份，就必须显式报"观测不到"
        host.kill()
        assert _wait_exit(host, 3.0) is not None
    finally:
        host.close()

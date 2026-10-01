from __future__ import annotations

import sys

from agentic_tty.core.ports import SessionMode, SessionSpec
from agentic_tty.runtime.subprocess_host import SubprocessHost


def _spec(argv: list[str]) -> SessionSpec:
    return SessionSpec(mode=SessionMode.PROCESS, argv=argv)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


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
        assert host.wait_exit(2.0) == 0
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
        assert host.wait_exit(2.0) == 7
    finally:
        host.close()


def test_kill_terminates_tree():
    host = SubprocessHost(_spec(_py("import time;time.sleep(30)")))
    try:
        assert host.try_wait() is None
        host.kill()
        assert host.wait_exit(3.0) is not None
    finally:
        host.close()


def test_env_is_passed_through():
    spec = SessionSpec(
        mode=SessionMode.PROCESS,
        argv=_py("import os;print(os.environ.get('AGENTIC_TTY_TEST',''))"),
        env={"AGENTIC_TTY_TEST": "42"},
    )
    host = SubprocessHost(spec)
    try:
        assert host.read(timeout=None).strip() == b"42"  # Windows 文本模式会带 \r
    finally:
        host.close()


def test_spawn_failure_raises():
    import pytest

    from agentic_tty.runtime.errors import HostSpawnError

    with pytest.raises(HostSpawnError):
        SubprocessHost(_spec(["definitely-not-a-real-program-xyz"]))

"""端到端：起会话 → 驱动读循环 → 读视图 → 关闭。

返回条件（等待引擎）属于命令层，不在核心层，所以这里只验证核心层本身：
输出进日志、视图正确、退出与排空被正确识别。
"""

from __future__ import annotations

import time

from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.runtime.runner import SessionRunner
from agentic_tty.core.session.base import Session
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.state import SessionState
from agentic_tty.example.core_test.runtime_fakehost.fake_host import FakeHost, FakeProgram


def _registry(program: FakeProgram) -> SessionRegistry:
    return SessionRegistry(lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16)


def _open(registry: SessionRegistry, spec: SessionSpec) -> tuple[Session, SessionRunner]:
    session = registry.create(spec)
    runner = SessionRunner(session, read_timeout=0.02)
    runner.start()
    return session, runner


def _pump_until_output(
    runner: SessionRunner, session: Session, needle: bytes, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in session.read_all(Stream.STDOUT):
            return True
        if runner.pump():
            break
        time.sleep(0.01)
    return needle in session.read_all(Stream.STDOUT)


def test_subprocess_runs_to_completion():
    program = FakeProgram(
        chunks=((0.0, b"step 1\n"), (0.02, b"step 2\n"), (0.04, b"DONE\n")),
        exit_after=0.1,
        exit_code=0,
    )
    registry = _registry(program)
    session, runner = _open(registry, SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        assert runner.run_until_drained(time.monotonic() + 3.0)
        assert session.exit_code == 0
        assert session.state is SessionState.EXITED
        assert session.drained
        assert session.read_all(Stream.STDOUT).splitlines()[-1] == b"DONE"
    finally:
        session.close()
        runner.stop()


def test_subprocess_streams_stay_independent():
    program = FakeProgram(
        chunks=((0.0, b"out\n"),),
        stderr_chunks=((0.0, b"err\n"),),
        exit_after=0.05,
    )
    registry = _registry(program)
    session, runner = _open(registry, SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        runner.run_until_drained(time.monotonic() + 3.0)
        assert session.read_all(Stream.STDOUT) == b"out\n"
        assert session.read_all(Stream.STDERR) == b"err\n"
    finally:
        session.close()
        runner.stop()


def test_terminal_wait_prompt_then_send_then_read_echo():
    program = FakeProgram(
        chunks=((0.0, b"ready\n> "),),
        echo_input=True,
        respond=lambda line: b"ok:" + line.strip() + b"\n> ",
    )
    registry = _registry(program)
    session, runner = _open(registry, SessionSpec(mode=PTY, argv=("x",)))
    try:
        assert _pump_until_output(runner, session, b"> ", 2.0)
        session.send(b"ping\n")
        assert _pump_until_output(runner, session, b"ok:ping", 2.0)
        assert "ok:ping" in session.screen_text()
    finally:
        session.close()
        runner.stop()


def test_run_for_stops_early_when_session_ends():
    registry = _registry(FakeProgram(exit_after=0.05, exit_code=0))
    session, runner = _open(registry, SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        started = time.monotonic()
        runner.run_for(5.0)  # 会话 0.05s 就结束，不该真的跑 5 秒
        assert time.monotonic() - started < 2.0
        assert session.drained
    finally:
        session.close()
        runner.stop()


def test_registry_close_all_releases_hosts():
    registry = _registry(FakeProgram(exit_after=None))
    first = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    second = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    registry.close_all()
    assert registry.list() == []
    assert first.host is None and second.host is None

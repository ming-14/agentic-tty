"""SessionRunner：读线程 → 有界桥 → 所有者侧摄入。"""

from __future__ import annotations

import time

from agentic_tty.core.ports import SessionMode, SessionSpec, Stream
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.state import SessionState
from agentic_tty.example.fake_host import FakeHost, FakeProgram
from agentic_tty.runtime.runner import SessionRunner


def _open(program: FakeProgram, mode: SessionMode = SessionMode.PROCESS):
    registry = SessionRegistry(lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16)
    session = registry.create(SessionSpec(mode=mode, argv=("x",)))
    session.start()
    runner = SessionRunner(session, read_timeout=0.02)
    runner.start()
    return registry, session, runner


def test_pump_drains_bridge_and_detects_exit():
    program = FakeProgram(chunks=((0.0, b"a\n"), (0.05, b"b\n")), exit_after=0.1, exit_code=0)
    _registry, session, runner = _open(program)
    try:
        assert runner.run_until_drained(time.monotonic() + 3.0)
        assert session.exit_code == 0
        assert session.drained
        assert session.state is SessionState.EXITED
        assert session.read_all(Stream.STDOUT) == b"a\nb\n"
    finally:
        session.close()
        runner.stop()


def test_two_streams_are_read_by_two_readers():
    program = FakeProgram(
        chunks=((0.0, b"out\n"),), stderr_chunks=((0.0, b"err\n"),), exit_after=0.05
    )
    _registry, session, runner = _open(program)
    try:
        runner.run_until_drained(time.monotonic() + 3.0)
        assert session.read_all(Stream.STDOUT) == b"out\n"
        assert session.read_all(Stream.STDERR) == b"err\n"
    finally:
        session.close()
        runner.stop()


def test_pump_marks_eof_only_after_all_streams_drained():
    program = FakeProgram(exit_after=0.02, exit_code=0)
    _registry, session, runner = _open(program)
    try:
        assert runner.run_until_drained(time.monotonic() + 3.0)
        assert session.eof_streams == frozenset({Stream.STDOUT, Stream.STDERR})
    finally:
        session.close()
        runner.stop()


def test_running_session_never_drains():
    _registry, session, runner = _open(FakeProgram(exit_after=None))
    try:
        for _ in range(20):
            assert runner.pump() is False
            time.sleep(0.005)
        assert session.exit_code is None
        assert not session.drained
    finally:
        session.close()
        runner.stop()


def test_submit_input_goes_through_write_thread():
    """输入经写线程写出（唯一写者），不占用所有者线程。"""
    program = FakeProgram(chunks=((0.0, b"> "),), echo_input=True, exit_after=None)
    _registry, session, runner = _open(program, SessionMode.PTY)
    try:
        assert runner.submit_input(b"ping\n")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            runner.pump()
            if b"ping" in session.read_all(Stream.STDOUT):
                break
            time.sleep(0.01)
        assert b"ping" in session.read_all(Stream.STDOUT)
    finally:
        session.close()
        runner.stop()


def test_submit_input_ignores_empty_payload():
    _registry, session, runner = _open(FakeProgram(exit_after=None))
    try:
        assert runner.submit_input(b"") is True
    finally:
        session.close()
        runner.stop()


def test_stop_is_idempotent_and_does_not_hang():
    _registry, session, runner = _open(FakeProgram(exit_after=None))
    try:
        runner.stop()
        runner.stop()
    finally:
        session.close()

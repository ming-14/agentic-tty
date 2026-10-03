"""SessionRunner：读线程 → 有界桥 → 所有者侧摄入。"""

from __future__ import annotations

import time

from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.runtime.bridge import Wakeup
from agentic_tty.core.runtime.runner import SessionRunner
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.state import SessionState
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram


def _open(program: FakeProgram, mode: str = SUBPROCESS):
    registry = SessionRegistry(lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16)
    session = registry.create(SessionSpec(mode=mode, argv=("x",)))
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
    """两路输出都排空才算结束：drained 要求 STDOUT 与 STDERR 都投过 EOF。"""
    program = FakeProgram(exit_after=0.02, exit_code=0)
    _registry, session, runner = _open(program)
    try:
        assert runner.run_until_drained(time.monotonic() + 3.0)
        assert session.drained
    finally:
        session.close()
        runner.stop()


def test_model_response_is_written_back_to_host():
    """终端模型的应答（DSR / 焦点应答等）必须经写线程回写，否则模型永远收不到回复。"""
    program = FakeProgram(
        chunks=((0.0, b"\x1b[6n"),), ingest_response=b"\x1b[1;1R", exit_after=None
    )
    _registry, session, runner = _open(program, PTY)
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            runner.pump()
            if session.host.received_input:
                break
            time.sleep(0.01)
        assert session.host.received_input == b"\x1b[1;1R"
    finally:
        session.close()
        runner.stop()


def test_reader_signals_wakeup_when_data_arrives():
    """读线程拿到数据后经唤醒通道通知驱动方——驱动方因此不必定时轮询。"""
    program = FakeProgram(chunks=((0.0, b"hi"),), exit_after=None)
    registry = SessionRegistry(
        lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16
    )
    session = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    wakeup = Wakeup()
    runner = SessionRunner(session, wakeup=wakeup, read_timeout=0.02)
    runner.start()
    try:
        assert wakeup.wait(timeout=2.0) == session.uid
    finally:
        runner.stop()
        session.close()


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
    _registry, session, runner = _open(program, PTY)
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

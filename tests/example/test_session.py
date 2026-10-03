from __future__ import annotations

import pytest

from agentic_tty.core.errors import CoreError
from agentic_tty.core.ports import PTY, SUBPROCESS, HostLifecycle, SessionSpec, Stream
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.session.base import Session
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.state import SessionState
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram


def _registry(program: FakeProgram | None = None) -> SessionRegistry:
    program = program or FakeProgram()
    return SessionRegistry(lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16)


def _create(registry: SessionRegistry, mode: str) -> Session:
    return registry.create(SessionSpec(mode=mode, argv=("x",)))


def _bare_session(mode: str = SUBPROCESS) -> Session:
    """未启动的会话——注册表里只放活着的会话，所以这里直接构造。"""
    return ProcessSession(
        "uid-bare",
        SessionSpec(mode=mode, argv=("x",)),
        lambda spec: FakeHost(spec, FakeProgram()),
        journal_budget_bytes=1 << 16,
    )


class _SlowExitHost(FakeHost):
    """强杀后退出码要过几次查询才可见（Windows Job Object 强杀后的真实行为）。"""

    def __init__(self, spec: SessionSpec, program: FakeProgram, blind_calls: int = 1) -> None:
        super().__init__(spec, program)
        self._blind = blind_calls

    def try_wait(self) -> int | None:
        if self._blind > 0:
            self._blind -= 1
            return None
        return super().try_wait()


def _registry_with(host: FakeHost) -> SessionRegistry:
    return SessionRegistry(lambda spec: host, journal_budget_bytes=1 << 16)


def test_ingest_advances_offsets_and_journal():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    first = session.ingest_stream(Stream.STDOUT, b"hello")
    assert (first.start_offset, first.end_offset) == (0, 5)
    assert first.stream is Stream.STDOUT
    second = session.ingest_stream(Stream.STDOUT, b" world")
    assert (second.start_offset, second.end_offset) == (5, 11)
    assert session.read_all(Stream.STDOUT) == b"hello world"
    session.close()


def test_streams_are_visible_per_session_type():
    registry = _registry()
    pty = _create(registry, PTY)
    process = _create(registry, SUBPROCESS)
    assert pty.streams() == (Stream.STDOUT,)
    assert process.streams() == (Stream.STDOUT, Stream.STDERR)
    pty.close()
    process.close()


def test_process_session_keeps_streams_separate():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    session.ingest_stream(Stream.STDOUT, b"out1")
    session.ingest_stream(Stream.STDERR, b"err1")
    assert session.read_all(Stream.STDOUT) == b"out1"
    assert session.read_all(Stream.STDERR) == b"err1"
    assert session.journal.end_offset == 4
    assert session.journal_for(Stream.STDERR).end_offset == 4  # 各自独立的 offset 空间
    session.close()


def test_terminal_session_has_single_stream():
    registry = _registry()
    session = _create(registry, PTY)
    with pytest.raises(CoreError):
        session.ingest_stream(Stream.STDERR, b"x")
    with pytest.raises(CoreError):
        session.read_all(Stream.STDERR)
    session.close()


def test_lifecycle_transitions_and_idempotent_close():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    assert session.state is SessionState.RUNNING
    session.stop()
    assert session.state is SessionState.EXITED
    session.close()
    assert session.state is SessionState.CLOSED
    session.close()  # 幂等
    assert session.state is SessionState.CLOSED


def test_close_without_start_is_allowed():
    session = _bare_session()
    assert session.state is SessionState.CREATED
    session.close()
    assert session.state is SessionState.CLOSED


def test_start_failure_records_error_and_leaves_no_residue():
    """宿主建不起来：会话记下错误并关死，注册表也不留残骸。"""

    def _boom(spec: SessionSpec) -> HostLifecycle:
        raise RuntimeError("no host")

    session = ProcessSession(
        "u1", SessionSpec(mode=SUBPROCESS, argv=("x",)), _boom, journal_budget_bytes=1 << 16
    )
    with pytest.raises(CoreError):
        session.start()
    assert session.error is not None
    assert session.state is SessionState.CLOSED

    registry = SessionRegistry(_boom, journal_budget_bytes=1 << 16)
    with pytest.raises(CoreError):
        registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    assert registry.list() == []


def test_ingest_after_close_is_rejected():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    session.close()
    with pytest.raises(CoreError):
        session.ingest_stream(Stream.STDOUT, b"x")


def test_refresh_detects_exit():
    registry = _registry(FakeProgram(exit_after=0.0, exit_code=3))
    session = _create(registry, SUBPROCESS)
    assert session.exit_code is None
    session.refresh()
    assert session.exit_code == 3
    assert session.state is SessionState.EXITED
    session.close()


def test_eof_is_not_exit():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    session.mark_eof(Stream.STDOUT)
    assert session.state is SessionState.RUNNING
    session.refresh()
    assert session.state is SessionState.RUNNING  # EOF 不代表退出
    session.close()


def test_drained_requires_all_streams_eof_when_externally_driven():
    registry = _registry(FakeProgram(exit_after=0.0, exit_code=0))
    session = _create(registry, SUBPROCESS)
    session.refresh()
    assert session.exit_code == 0
    assert session.drained  # 没有外部驱动声明时：退出即结束

    driven = _create(registry, SUBPROCESS)
    driven.expect_eof()
    driven.refresh()
    assert not driven.drained  # 有外部驱动：等所有流 EOF
    driven.mark_eof(Stream.STDOUT)
    assert not driven.drained
    driven.mark_eof(Stream.STDERR)
    assert driven.drained
    session.close()
    driven.close()


def test_stop_records_immediately_visible_exit_code():
    registry = _registry(FakeProgram(exit_after=None, exit_code=7))
    session = _create(registry, SUBPROCESS)
    session.expect_eof()  # 有外部驱动：drained 还要求所有流 EOF
    assert not session.drained

    session.stop()
    assert session.state is SessionState.EXITED
    # 同步查一次就能拿到（假宿主强杀后立刻可见）
    assert session.exit_code == 7
    for stream in session.streams():
        session.mark_eof(stream)
    assert session.drained
    session.close()


def test_stop_does_not_wait_for_late_exit_code():
    """core 不做时序等待：stop 只同步查一次，晚到的退出码交给驱动方的 refresh。"""
    host = _SlowExitHost(SessionSpec(mode=SUBPROCESS, argv=("x",)), FakeProgram(exit_code=9))
    session = _create(_registry_with(host), SUBPROCESS)
    session.stop()
    assert session.state is SessionState.EXITED
    assert session.exit_code is None  # 首次查询拿不到就不等
    assert session.drained  # drained 以状态为准，不受退出码影响
    session.refresh()
    assert session.exit_code == 9
    session.close()


def test_refresh_fills_exit_code_missed_by_stop():
    host = _SlowExitHost(SessionSpec(mode=SUBPROCESS, argv=("x",)), FakeProgram(exit_code=9))
    session = _create(_registry_with(host), SUBPROCESS)
    session.stop()  # 首次查询拿不到就不等
    assert session.exit_code is None
    session.refresh()  # 由驱动循环补拿
    assert session.exit_code == 9
    session.close()


def test_drained_once_closed():
    registry = _registry(FakeProgram(exit_after=None))
    session = _create(registry, SUBPROCESS)
    session.expect_eof()
    session.close()
    assert session.drained  # 宿主已释放，不可能再有输出


def test_descendants_forward_to_host():
    host = FakeHost(SessionSpec(mode=SUBPROCESS, argv=("x",)), FakeProgram())
    session = _create(_registry_with(host), SUBPROCESS)
    assert session.descendants() == ()
    host.descendants_pids = (101, 202)  # 模拟子进程起来了
    assert session.descendants() == (101, 202)
    session.close()


def test_descendants_before_start_is_rejected():
    with pytest.raises(CoreError):
        _bare_session().descendants()


def test_terminal_resize_updates_both_sides():
    registry = _registry()
    session = _create(registry, PTY)
    session.resize(120, 40)
    assert (session.cols, session.rows) == (120, 40)
    assert session.host.size == (120, 40)
    session.close()


def test_process_session_rejects_screen_api():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    with pytest.raises(CoreError):
        session.resize(10, 10)
    with pytest.raises(CoreError):
        session.rebuild_bytes()
    with pytest.raises(CoreError):
        session.screen_text()
    with pytest.raises(CoreError):
        session.full_text()
    with pytest.raises(CoreError):
        session.screen_cells()
    with pytest.raises(CoreError):
        session.render_svg()
    with pytest.raises(CoreError):
        session.render_image()
    session.close()


def test_terminal_screen_views_forward_to_host():
    registry = _registry()
    session = _create(registry, PTY)
    session.ingest_stream(Stream.STDOUT, b"hello\nworld")
    assert session.screen_text() == "hello\nworld"
    assert session.full_text() == "hello\nworld"
    assert session.screen_cells() == (("h", "e", "l", "l", "o"), ("w", "o", "r", "l", "d"))
    assert session.rebuild_bytes() == b"hello\nworld"
    session.close()


def test_send_reaches_host():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    session.send(b"input\n")
    assert session.host.received_input == b"input\n"
    session.close()


def test_close_stdin_only_supported_for_process():
    registry = _registry()
    process = _create(registry, SUBPROCESS)
    process.close_stdin()
    assert process.host.stdin_closed
    process.close()

    pty = _create(registry, PTY)
    with pytest.raises(CoreError):
        pty.close_stdin()
    pty.close()


def test_attach_plan_uses_journal_of_stream():
    registry = _registry()
    session = _create(registry, SUBPROCESS)
    session.ingest_stream(Stream.STDOUT, b"abc")
    assert session.attach_plan(None).from_offset == 0
    assert session.attach_plan(2).from_offset == 2
    session.close()

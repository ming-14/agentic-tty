"""会话运行时：会话与驱动成对管理，两阶段释放。"""

from __future__ import annotations

import time

from agentic_tty.core.ports import SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.runtime.input_queue import InputVerdict
from agentic_tty.core.runtime.runtime import Runtime
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram


def _runtime(program: FakeProgram | None = None) -> Runtime:
    program = program or FakeProgram()
    registry = SessionRegistry(
        lambda spec: FakeHost(spec, program), journal_budget_bytes=1 << 16
    )
    return Runtime(registry)


def test_create_pairs_session_with_its_driver():
    runtime = _runtime(FakeProgram(chunks=((0.0, b"hi"),), exit_after=None))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        assert runtime.get(session.uid) is session
        assert runtime.runner(session.uid) is not None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not session.read_all(Stream.STDOUT):
            runtime.pump_all()
            time.sleep(0.01)
        assert session.read_all(Stream.STDOUT) == b"hi"
    finally:
        runtime.close_all()


def test_pump_all_reports_events_by_uid():
    runtime = _runtime(FakeProgram(chunks=((0.0, b"abc"),), exit_after=0.05))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        seen: set[str] = set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not session.drained:
            seen.update(runtime.pump_all())
            time.sleep(0.005)
        assert session.uid in seen
    finally:
        runtime.close_all()


def test_close_detaches_synchronously():
    """两阶段释放：摘除是同步的（列表立刻空），宿主关闭交给别的线程。"""
    runtime = _runtime(FakeProgram(exit_after=None))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    runtime.close(session.uid)
    assert runtime.list() == []
    assert runtime.find(session.uid) is None
    assert runtime.runner(session.uid) is None
    runtime.close_all()


def test_send_input_reaches_the_driver():
    runtime = _runtime(FakeProgram(chunks=((0.0, b"> "),), echo_input=True, exit_after=None))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        assert runtime.send_input(session.uid, b"hi\n") is InputVerdict.QUEUED
    finally:
        runtime.close_all()

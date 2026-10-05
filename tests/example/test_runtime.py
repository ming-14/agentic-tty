"""会话运行时：会话与驱动成对管理，两阶段释放。"""

from __future__ import annotations

import time

import pytest

from agentic_tty.core.errors import SessionNotFound
from agentic_tty.core.ports import SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.runtime.input_queue import InputVerdict
from agentic_tty.core.runtime.runner import SessionRunner
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


def test_close_unknown_uid_raises():
    """未知 uid 报 `SessionNotFound`，不静默当成功。"""
    runtime = _runtime()
    with pytest.raises(SessionNotFound):
        runtime.close("no-such-uid")


class _BoomRunner:
    """`start()` 必炸的驱动替身。"""

    def __init__(self) -> None:
        self.stopped = False

    def start(self) -> None:
        raise RuntimeError("boom")

    def stop(self) -> None:
        self.stopped = True


def test_create_stops_the_driver_when_start_fails():
    """`start()` 半途抛错：会话收掉，驱动也要停——已起的读写线程不能没人收。"""
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram(exit_after=None)))
    runner = _BoomRunner()
    runtime = Runtime(registry, runner_factory=lambda session: runner)
    with pytest.raises(RuntimeError):
        runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    runtime.close_all()  # 释放走的是别的线程，这里等它落地
    assert runtime.list() == []
    assert runner.stopped


def test_send_input_reaches_the_driver():
    runtime = _runtime(FakeProgram(chunks=((0.0, b"> "),), echo_input=True, exit_after=None))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        assert runtime.send_input(session.uid, b"hi\n") is InputVerdict.QUEUED
    finally:
        runtime.close_all()


def test_runner_factory_takes_over_the_driver():
    """给了 `runner_factory` 就由它造驱动——示例层正是靠它注入小水位。

    默认硬上限是 1 MiB，101 字节本该照收；这里被工厂压到 100B，于是拒收——证明工厂生效。
    """
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram(exit_after=None)))
    runtime = Runtime(
        registry,
        runner_factory=lambda session: SessionRunner(
            session, input_max_bytes=100, input_high_watermark=60, input_low_watermark=20
        ),
    )
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        assert runtime.send_input(session.uid, b"a" * 101) is InputVerdict.REJECTED
    finally:
        runtime.close_all()


def test_pump_advances_only_the_named_session():
    """`pump(uid)` 只推那一个——别的会话就算桥里有数据也原地不动。"""
    runtime = _runtime(FakeProgram(chunks=((0.0, b"hi"),), exit_after=None))
    first = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    second = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("y",)))
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not first.read_all(Stream.STDOUT):
            runtime.pump(first.uid)
            time.sleep(0.01)
        assert first.read_all(Stream.STDOUT) == b"hi"
        assert second.read_all(Stream.STDOUT) == b""  # 从没被推过
    finally:
        runtime.close_all()


def test_pump_of_an_unknown_uid_is_not_an_error():
    """唤醒信号可能晚到一步（会话刚被摘掉）——那不是错。"""
    runtime = _runtime()
    assert runtime.pump("no-such-uid") == []


def test_refresh_all_picks_up_an_exit_without_draining():
    """兜底那遍**只问退出**：把退出码收进来，不碰桥。"""
    runtime = _runtime(FakeProgram(exit_after=0.05))
    session = runtime.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and session.exit_code is None:
            runtime.refresh_all()
            time.sleep(0.005)
        assert session.exit_code is not None
    finally:
        runtime.close_all()

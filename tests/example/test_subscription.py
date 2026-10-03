"""订阅者游标：按游标从日志补齐；落后到裁剪区间时走重建字节。"""

from __future__ import annotations

import pytest

from agentic_tty.core.errors import OffsetAhead, OffsetTrimmed
from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.subscription import Subscription
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram


def _registry(program: FakeProgram | None = None, budget: int = 1 << 16) -> SessionRegistry:
    program = program or FakeProgram()
    return SessionRegistry(lambda spec: FakeHost(spec, program), journal_budget_bytes=budget)


def _session(registry: SessionRegistry, mode: str = SUBPROCESS):
    return registry.create(SessionSpec(mode=mode, argv=("x",)))


def test_subscription_pulls_incrementally():
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"abc")
    sub = Subscription(session, cursor=0)
    assert sub.next_offset == 0
    assert sub.pull() == b"abc"
    assert sub.next_offset == 3
    session.ingest_stream(Stream.STDOUT, b"de")
    assert sub.pull() == b"de"
    assert sub.next_offset == 5
    assert sub.pull() == b""  # 没有新数据
    session.close()


def test_subscription_from_none_starts_at_zero():
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"xy")
    sub = Subscription(session)  # cursor=None → 全新订阅者，语义上就是游标 0
    assert sub.next_offset == 0
    assert sub.pull() == b"xy"
    session.close()


def test_subscription_behind_trim_point_rebuilds_for_terminal():
    """终端会话游标落后：从终端模型重建完整快照，不丢内容。"""
    session = _session(_registry(budget=4), mode=PTY)
    session.ingest_stream(Stream.STDOUT, b"0123456789")  # 超预算 → 裁掉头部
    journal = session.journal_for(Stream.STDOUT)
    assert journal.start_offset > 0

    sub = Subscription(session, cursor=0)
    assert not sub.lossy
    assert sub.next_offset == journal.end_offset
    assert sub.pull() == b"0123456789"  # 假宿主：重建字节就是它攒的屏幕缓冲
    assert sub.pull() == b""
    session.close()


def test_subscription_is_lossy_for_process_session():
    """子进程会话没有模型可重建：只能从保留区起点给，被裁的那段找不回。"""
    session = _session(_registry(budget=4))  # SUBPROCESS
    session.ingest_stream(Stream.STDOUT, b"0123456789")
    journal = session.journal_for(Stream.STDOUT)
    assert journal.start_offset > 0

    sub = Subscription(session, cursor=0)
    assert sub.lossy
    assert sub.pull() == journal.read(journal.start_offset)  # 只剩保留区
    assert sub.pull() == b""
    session.close()


def test_subscription_pull_raises_when_cursor_gets_trimmed():
    """构造之后日志又被裁（订阅者太慢）：显式报错，不静默从保留区起点给。"""
    session = _session(_registry(budget=8))
    session.ingest_stream(Stream.STDOUT, b"abcd")
    sub = Subscription(session, cursor=0)
    session.ingest_stream(Stream.STDOUT, b"0123456789")  # 超预算 → 裁掉头部
    assert session.journal_for(Stream.STDOUT).start_offset > 0
    with pytest.raises(OffsetTrimmed):
        sub.pull()
    session.close()


def test_subscription_rejects_cursor_ahead():
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"ab")
    with pytest.raises(OffsetAhead):
        Subscription(session, cursor=99)
    session.close()


def test_subscription_keeps_streams_separate():
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"out")
    session.ingest_stream(Stream.STDERR, b"err")
    out = Subscription(session, Stream.STDOUT, cursor=0)
    err = Subscription(session, Stream.STDERR, cursor=0)
    assert out.pull() == b"out"
    assert err.pull() == b"err"
    assert out.next_offset == 3 and err.next_offset == 3  # 各自独立的 offset 空间
    session.close()

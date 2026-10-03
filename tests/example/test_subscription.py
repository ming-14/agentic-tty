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
    assert sub.pull().data == b"abc"
    assert sub.next_offset == 3
    session.ingest_stream(Stream.STDOUT, b"de")
    assert sub.pull().data == b"de"
    assert sub.next_offset == 5
    assert sub.pull().data == b""  # 没有新数据
    session.close()


def test_subscription_from_none_starts_at_zero():
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"xy")
    sub = Subscription(session)  # cursor=None → 全新订阅者，语义上就是游标 0
    assert sub.next_offset == 0
    assert sub.pull().data == b"xy"
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
    assert sub.pull().data == b"0123456789"  # 假宿主：重建字节就是它攒的屏幕缓冲
    assert sub.pull().data == b""
    session.close()


def test_subscription_is_lossy_for_process_session():
    """子进程会话没有模型可重建：只能从保留区起点给，被裁的那段找不回。"""
    session = _session(_registry(budget=4))  # SUBPROCESS
    session.ingest_stream(Stream.STDOUT, b"0123456789")
    journal = session.journal_for(Stream.STDOUT)
    assert journal.start_offset > 0

    sub = Subscription(session, cursor=0)
    assert sub.lossy
    assert sub.pull().data == journal.read(journal.start_offset)  # 只剩保留区
    assert sub.pull().data == b""
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
    assert out.pull().data == b"out"
    assert err.pull().data == b"err"
    assert out.next_offset == 3 and err.next_offset == 3  # 各自独立的 offset 空间
    session.close()


def test_pull_respects_max_bytes():
    """批量上限：设了就是分片拉取，剩下的留在日志里，下次接着给。"""
    session = _session(_registry())
    session.ingest_stream(Stream.STDOUT, b"0123456789")
    sub = Subscription(session, cursor=0)
    first = sub.pull(max_bytes=4)
    assert (first.start, first.data) == (0, b"0123")
    assert sub.next_offset == 4
    assert sub.pull(max_bytes=4).data == b"4567"
    assert sub.pull(max_bytes=4).data == b"89"
    assert sub.pull(max_bytes=4).data == b""
    session.close()


def test_pull_splits_the_rebuild_snapshot_too():
    """重建快照同样受批量上限约束，否则"重同步"那一次仍是一次性全量。"""
    session = _session(_registry(budget=4), mode=PTY)
    session.ingest_stream(Stream.STDOUT, b"0123456789")  # 超预算 → 走重建
    sub = Subscription(session, cursor=0)
    assert not sub.lossy
    assert sub.pull(max_bytes=3).data == b"012"
    assert sub.pull(max_bytes=3).data == b"345"
    assert sub.pull(max_bytes=3).data == b"678"
    assert sub.pull(max_bytes=3).data == b"9"
    assert sub.pull(max_bytes=3).data == b""
    session.close()


def test_pull_carries_resize_events_in_the_byte_offset_space():
    """尺寸变更与字节共用 offset 空间：订阅者据此知道从哪个字节起换新尺寸。

    少了它，落后的 raw 订阅者会拿新尺寸去解释旧字节。
    """
    session = _session(_registry(), mode=PTY)
    session.ingest_stream(Stream.STDOUT, b"aaaa")
    session.resize(100, 30)
    session.ingest_stream(Stream.STDOUT, b"bbbb")

    sub = Subscription(session, cursor=0)
    first = sub.pull()
    assert first.data == b"aaaabbbb"
    assert [(e.offset, e.cols, e.rows) for e in first.resizes] == [(4, 100, 30)]
    assert sub.pull().resizes == ()  # 已交付过的不重复给
    session.close()


def test_process_session_has_no_resize_events():
    """没有屏幕的会话恒为空——resize 对它本来就不成立。"""
    session = _session(_registry())
    assert session.resize_events() == ()
    session.close()

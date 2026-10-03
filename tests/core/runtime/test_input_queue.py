"""输入队列：按字节计量的双水位。"""

from __future__ import annotations

import pytest

from agentic_tty.core.runtime.input_queue import InputVerdict, WriteQueue


def _queue(**overrides: int) -> WriteQueue:
    kwargs = {"max_bytes": 100, "high_watermark": 60, "low_watermark": 20}
    kwargs.update(overrides)
    return WriteQueue(**kwargs)


def test_rejects_over_hard_limit_without_taking_a_partial_block():
    q = _queue()
    assert q.put(b"a" * 60) is InputVerdict.QUEUED  # 60 不 > 60
    assert q.put(b"b" * 41) is InputVerdict.REJECTED  # 101 > 100
    assert q.depth_bytes == 60  # 拒收不留半个


def test_holds_above_high_watermark_and_releases_at_low():
    q = _queue()
    assert q.put(b"a" * 40) is InputVerdict.QUEUED
    assert q.drained
    assert q.put(b"b" * 30) is InputVerdict.HOLD  # 70 > 60
    assert not q.drained
    assert q.get(timeout=0.1) == b"a" * 40  # 70 - 40 = 30，仍在低水位之上
    assert not q.drained
    assert q.get(timeout=0.1) == b"b" * 30  # 0 ≤ 20 → 放行
    assert q.drained


def test_wait_drained_blocks_until_the_queue_falls_back():
    q = _queue()
    q.put(b"a" * 70)  # 越过高水位
    assert q.wait_drained(timeout=0.01) is False
    q.get(timeout=0.1)
    assert q.wait_drained(timeout=0.1) is True


def test_watermarks_must_be_ordered():
    with pytest.raises(ValueError):
        WriteQueue(max_bytes=10, high_watermark=20, low_watermark=5)


def test_get_times_out_when_empty():
    assert _queue().get(timeout=0.01) is None

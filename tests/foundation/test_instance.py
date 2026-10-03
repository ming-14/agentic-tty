"""本机实例互斥：取锁、拒第二个、**查询不留副作用**。"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from agentic_tty.config.names import lock_name
from agentic_tty.foundation.instance import InstanceLock, is_held


def _instance() -> str:
    return f"agentic-tty-test-{uuid.uuid4().hex[:8]}"


def _lock(directory: Path, instance: str) -> InstanceLock:
    """按"实例名 + 目录"算出锁名再建锁——与守护进程走的是同一条路。"""
    return InstanceLock(lock_name(instance, directory))


def test_acquire_release_reacquire(tmp_path: Path):
    instance = _instance()
    first = _lock(tmp_path, instance)
    assert first.acquire() is True
    assert first.acquired is True
    first.release()
    assert first.acquired is False

    second = _lock(tmp_path, instance)
    assert second.acquire() is True
    second.release()


def test_second_instance_is_rejected(tmp_path: Path):
    instance = _instance()
    holder = _lock(tmp_path, instance)
    assert holder.acquire() is True
    try:
        other = _lock(tmp_path, instance)
        assert other.acquire() is False
        assert other.acquired is False
    finally:
        holder.release()


def test_release_is_idempotent(tmp_path: Path):
    lock = _lock(tmp_path, _instance())
    lock.acquire()
    lock.release()
    lock.release()
    assert lock.acquired is False


def test_creates_runtime_dir(tmp_path: Path):
    """POSIX 上锁名**就是路径**，所以取锁前得让它的目录在。"""
    if sys.platform == "win32":
        pytest.skip("Windows 的锁名是全局互斥体名，不是路径")
    nested = tmp_path / "a" / "b"
    lock = _lock(nested, _instance())
    assert lock.acquire() is True
    try:
        assert nested.is_dir()
    finally:
        lock.release()


def test_is_held_answers_without_side_effects(tmp_path: Path):
    """查询**只读**：没人占时不许建文件，有人占时答 True，放掉之后答 False。"""
    full = lock_name(_instance(), tmp_path)
    assert is_held(full) is False
    if sys.platform != "win32":
        assert not Path(full).exists()  # 查询不许 O_CREAT

    holder = InstanceLock(full)
    assert holder.acquire() is True
    try:
        assert is_held(full) is True
    finally:
        holder.release()
    assert is_held(full) is False

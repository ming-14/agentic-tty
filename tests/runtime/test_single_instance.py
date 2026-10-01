from __future__ import annotations

import uuid
from pathlib import Path

from agentic_tty.runtime.platform.single_instance import SingleInstance


def _name() -> str:
    return f"agentic-tty-test-{uuid.uuid4().hex[:8]}"


def test_acquire_release_reacquire(tmp_path: Path):
    name = _name()
    first = SingleInstance(name, tmp_path)
    assert first.acquire() is True
    assert first.acquired is True
    first.release()
    assert first.acquired is False

    second = SingleInstance(name, tmp_path)
    assert second.acquire() is True
    second.release()


def test_second_instance_is_rejected(tmp_path: Path):
    name = _name()
    holder = SingleInstance(name, tmp_path)
    assert holder.acquire() is True
    try:
        other = SingleInstance(name, tmp_path)
        assert other.acquire() is False
        assert other.acquired is False
    finally:
        holder.release()


def test_release_is_idempotent(tmp_path: Path):
    instance = SingleInstance(_name(), tmp_path)
    instance.acquire()
    instance.release()
    instance.release()
    assert instance.acquired is False


def test_creates_runtime_dir(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    instance = SingleInstance(_name(), nested)
    assert instance.acquire() is True
    try:
        assert nested.is_dir()
    finally:
        instance.release()

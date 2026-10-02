from __future__ import annotations

import pytest

from agentic_tty.core.errors import CoreError, SessionNotFound
from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.session.base import Session
from agentic_tty.core.session.registry import SessionKind, SessionRegistry
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.example.runtime_fakehost.fake_host import FakeHost, FakeProgram


def _registry() -> SessionRegistry:
    return SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))


def test_create_picks_implementation_by_mode():
    registry = _registry()
    pty = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    process = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    assert isinstance(pty, TerminalSession)
    assert isinstance(process, ProcessSession)
    assert pty.uid != process.uid


def test_unknown_mode_is_rejected():
    with pytest.raises(CoreError):
        _registry().create(SessionSpec(mode="nope", argv=("x",)))


def test_kinds_are_injectable():
    """模式标签是开放的：接入方可自带 `标签 → 会话形态`（会话类 + 宿主工厂）映射。"""
    registry = SessionRegistry(
        lambda spec: FakeHost(spec, FakeProgram()), kinds={"custom": SessionKind(Session)}
    )
    session = registry.create(SessionSpec(mode="custom", argv=("x",)))
    assert type(session) is Session


def test_get_find_list():
    registry = _registry()
    session = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    assert registry.get(session.uid) is session
    assert registry.find(session.uid) is session
    assert registry.find("missing") is None
    assert registry.list() == [session]


def test_get_missing_raises():
    registry = _registry()
    with pytest.raises(SessionNotFound):
        registry.get("missing")


def test_close_removes_and_reports_missing():
    registry = _registry()
    session = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    registry.close(session.uid)
    assert registry.find(session.uid) is None
    with pytest.raises(SessionNotFound):
        registry.close(session.uid)


def test_close_all_clears_registry():
    registry = _registry()
    first = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    second = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    registry.close_all()
    assert registry.list() == []
    assert first.host is None and second.host is None

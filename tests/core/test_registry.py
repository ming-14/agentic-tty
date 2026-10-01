from __future__ import annotations

import pytest

from agentic_tty.core.errors import SessionNotFound
from agentic_tty.core.ports import SessionMode, SessionSpec
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.example.fake_host import FakeHost, FakeProgram


def _registry() -> SessionRegistry:
    return SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))


def test_create_picks_implementation_by_mode():
    registry = _registry()
    pty = registry.create(SessionSpec(mode=SessionMode.PTY, argv=("x",)))
    process = registry.create(SessionSpec(mode=SessionMode.PROCESS, argv=("x",)))
    assert isinstance(pty, TerminalSession)
    assert isinstance(process, ProcessSession)
    assert pty.uid != process.uid


def test_get_find_list():
    registry = _registry()
    session = registry.create(SessionSpec(mode=SessionMode.PROCESS, argv=("x",)))
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
    session = registry.create(SessionSpec(mode=SessionMode.PROCESS, argv=("x",)))
    registry.close(session.uid)
    assert registry.find(session.uid) is None
    with pytest.raises(SessionNotFound):
        registry.close(session.uid)


def test_close_all_clears_registry():
    registry = _registry()
    first = registry.create(SessionSpec(mode=SessionMode.PROCESS, argv=("x",)))
    second = registry.create(SessionSpec(mode=SessionMode.PTY, argv=("x",)))
    registry.close_all()
    assert registry.list() == []
    assert first.host is None and second.host is None

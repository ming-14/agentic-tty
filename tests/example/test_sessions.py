"""example 的模式 → 会话装配映射：fake 是脚本化子进程，pty 走真终端宿主。"""

from __future__ import annotations

import pytest

from agentic_tty.core.errors import CoreError
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.example.core_test_console.runtime_fakehost import FakeHost, FakeProgram
from agentic_tty.example.core_test_console.sessions import ExampleMode, create_registry, session_spec


def _registry() -> SessionRegistry:
    """模式映射照旧，但宿主换成假宿主——测类型不必真的起进程。"""
    return create_registry(host_factory=lambda spec: FakeHost(spec, FakeProgram()))


def _create(mode: ExampleMode, argv: tuple[str, ...]):
    return _registry().create(session_spec(mode, argv))


def test_fake_mode_uses_process_session():
    assert isinstance(_create(ExampleMode.FAKE, ("build",)), ProcessSession)


def test_pty_mode_uses_terminal_session():
    assert isinstance(_create(ExampleMode.PTY, ("cmd",)), TerminalSession)


def test_subprocess_mode_uses_process_session():
    assert isinstance(_create(ExampleMode.SUBPROCESS, ("cmd",)), ProcessSession)


def test_fake_mode_rejects_unknown_program():
    """未知假程序：创建即失败（启动在 create 里），异常抛出且不入表。"""
    registry = _registry()
    with pytest.raises(CoreError):
        registry.create(session_spec(ExampleMode.FAKE, ("nope",)))
    assert registry.list() == []

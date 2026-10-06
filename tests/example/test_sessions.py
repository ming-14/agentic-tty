"""example 的模式 → 会话装配映射：fake 是脚本化子进程，pty 走真终端宿主。"""

from __future__ import annotations

import pytest

from agentic_tty.core.errors import CoreError
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.session.registry import DEFAULT_KINDS, SessionRegistry
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.example.core_test_console.runtime_fakehost import FakeHost, FakeProgram
from agentic_tty.example.core_test_console.sessions import (
    ExampleMode,
    create_registry,
    session_spec,
)
from agentic_tty.sandbox import SANDBOX_PTY


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


def test_session_spec_carries_the_working_directory():
    """工作目录是会话级参数：给了就传下去，没给就是 `None`（宿主自己退到当前目录）。"""
    assert session_spec(ExampleMode.PTY, ("cmd",)).cwd is None
    assert session_spec(ExampleMode.PTY, ("cmd",), cwd="/work").cwd == "/work"


def test_sandbox_mode_uses_terminal_session():
    """沙箱与 `localpty` 同形（终端会话），但标签是装配处并进来的，不在 core 内置里。

    不真的建会话——沙箱的宿主工厂是它自己的，注入假宿主换不掉它。
    """
    kinds = create_registry()._kinds
    assert SANDBOX_PTY in kinds
    assert kinds[SANDBOX_PTY].session_class is TerminalSession
    assert SANDBOX_PTY not in DEFAULT_KINDS


def test_fake_mode_rejects_unknown_program():
    """未知假程序：创建即失败（启动在 create 里），异常抛出且不入表。"""
    registry = _registry()
    with pytest.raises(CoreError):
        registry.create(session_spec(ExampleMode.FAKE, ("nope",)))
    assert registry.list() == []

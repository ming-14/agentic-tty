"""example 的模式 → 会话装配映射：fake 是脚本化子进程，pty 走真终端宿主。"""

from __future__ import annotations

import pytest

from agentic_tty.core.errors import CoreError
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.example.sessions import ExampleMode, create_session


def test_fake_mode_uses_process_session():
    assert isinstance(create_session(ExampleMode.FAKE, ("build",)), ProcessSession)


def test_pty_mode_uses_terminal_session():
    assert isinstance(create_session(ExampleMode.PTY, ("cmd",)), TerminalSession)


def test_subprocess_mode_uses_process_session():
    assert isinstance(create_session(ExampleMode.SUBPROCESS, ("cmd",)), ProcessSession)


def test_fake_mode_rejects_unknown_program():
    session = create_session(ExampleMode.FAKE, ("nope",))
    with pytest.raises(CoreError):
        session.start()
    assert session.error is not None

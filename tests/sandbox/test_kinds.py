"""沙箱模式标签与平台分派：装配处拿到的就是这份 `标签 → 形态`。

依赖是**按模式**算的：本模块在哪个平台都导入得动、也都能注册，缺实现是**建会话**那一步
的事——所以别的模式不会被它拖下水。Windows / Linux 都跑得起来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_tty.core.ports import SessionSpec
from agentic_tty.core.runtime.errors import DependencyMissing
from agentic_tty.core.session.registry import DEFAULT_KINDS
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.sandbox import SANDBOX_PTY, kinds, pty_host_factory, sandbox_kinds


def test_the_label_is_not_a_core_mode():
    """沙箱是核心层之外的能力实现——core 的内置形态里不该有它。"""
    assert SANDBOX_PTY not in DEFAULT_KINDS


def test_kinds_pairs_the_terminal_session_with_its_own_factory():
    """会话类与宿主工厂**成对**：terminal 会话 + 沙箱自己的工厂（可写档要闭包带进去）。"""
    kind = sandbox_kinds()[SANDBOX_PTY]
    assert kind.session_class is TerminalSession
    assert kind.host_factory is not None


def test_registration_does_not_depend_on_the_platform(monkeypatch):
    """注册不挑平台：缺实现是建会话那一步的事，不是注册的事。"""
    monkeypatch.setattr(kinds, "sys", SimpleNamespace(platform="linux"))
    assert SANDBOX_PTY in sandbox_kinds()


def test_a_platform_without_an_implementation_fails_loudly(monkeypatch):
    """没有实现就明确失败——不静默退回别的宿主。"""
    monkeypatch.setattr(kinds, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(DependencyMissing):
        pty_host_factory()(SessionSpec(mode=SANDBOX_PTY, argv=("sh",)))

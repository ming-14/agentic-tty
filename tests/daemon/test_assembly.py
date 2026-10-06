"""装配层：默认那份会话形态表是 core 的内置形态 ＋ 沙箱扩展拼出来的。

沙箱不在 core 里（core 反过来不认识它），所以"这台守护进程提供哪些模式"只能由装配处定。
"""

from __future__ import annotations

from agentic_tty.config import DaemonConfig
from agentic_tty.core.session.registry import DEFAULT_KINDS
from agentic_tty.daemon import __main__ as entry
from agentic_tty.daemon.assembly import default_registry
from agentic_tty.sandbox import SANDBOX_PTY


def test_the_sandbox_is_not_a_core_mode():
    """core 不认识沙箱——它是能力实现，只能由装配处并进来。"""
    assert SANDBOX_PTY not in DEFAULT_KINDS


def test_default_registry_adds_the_sandbox_on_top_of_the_core_modes(tmp_path):
    """注册表的 `kinds` 是**替换**不是合并：并完之后 core 那几种一个都不能少。"""
    registry = default_registry(DaemonConfig(name="assembly-test", directory=tmp_path))
    kinds = registry._kinds  # 形态表没有公开读法，这一条要看的就是它
    assert SANDBOX_PTY in kinds
    assert set(DEFAULT_KINDS) <= set(kinds)


def test_the_read_only_flag_is_the_deployment_switch(monkeypatch):
    """`--sandbox-read-only` 就是那个部署级开关的入口：它关掉工作区可写再交给装配。"""
    seen: list[DaemonConfig] = []
    monkeypatch.setattr(entry, "run", lambda config: seen.append(config) or 0)
    assert entry.main([]) == 0
    assert entry.main(["--sandbox-read-only"]) == 0
    assert [config.sandbox_workspace_write for config in seen] == [True, False]

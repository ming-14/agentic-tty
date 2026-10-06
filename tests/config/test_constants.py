"""本机对象的命名：两端各算一次必须一致，且 Windows 与 POSIX 行为对齐。"""

from __future__ import annotations

import sys
from pathlib import Path

from agentic_tty.config.constants import (
    PREFIX,
    endpoint_name,
    lock_name,
    resolve_endpoint,
    runtime_dir,
)


def test_endpoint_name_carries_the_prefix():
    assert endpoint_name("daemon-test") == f"{PREFIX}daemon-test"


def test_resolve_endpoint_prefers_the_explicit_name():
    """显式给的是**完整名字**（不加前缀）；没给才由实例名派生。"""
    assert resolve_endpoint("daemon-test") == f"{PREFIX}daemon-test"
    assert resolve_endpoint("daemon-test", "my-pipe") == "my-pipe"


def test_runtime_dir_is_per_instance(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert runtime_dir("daemon-test") == tmp_path / f"{PREFIX}daemon-test"


def test_lock_name_follows_the_platform(tmp_path: Path):
    name = lock_name("daemon-test", tmp_path)
    if sys.platform == "win32":
        # 互斥体名是**全局**的——不把目录并进去，"同名不同目录"的两份配置会互撞
        assert name.startswith("Local\\")
        assert lock_name("daemon-test", tmp_path / "other") != name
    else:
        assert name == str(tmp_path / f"{PREFIX}daemon-test.lock")


def test_same_inputs_give_the_same_name(tmp_path: Path):
    """两端各算一次、结果必然一样——这是"不需要发现协议"的前提。"""
    assert lock_name("x", tmp_path) == lock_name("x", tmp_path)
    assert endpoint_name("x") == endpoint_name("x")

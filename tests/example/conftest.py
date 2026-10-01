"""示例层测试的共享 fixture。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agentic_tty.daemon.server import Daemon

from .wire import has_native_host, serve


@pytest.fixture
def demo_daemon(tmp_path) -> Iterator[Daemon]:
    """一个跑在后台线程里的真守护进程（演示请求处理层 + loopback）。"""
    if not has_native_host():
        pytest.skip("需要 vendor 里的 pywezterm（真实 PTY）")
    with serve(tmp_path) as daemon:
        yield daemon

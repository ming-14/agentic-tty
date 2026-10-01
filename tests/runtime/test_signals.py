"""终止信号 → 回调。

守护进程靠这个把 SIGINT / SIGTERM 变成"请求停止"，所以它得真的能装上、真的会被调用。
"""

from __future__ import annotations

import signal

import pytest

from agentic_tty.runtime.platform.signals import install_shutdown_handler


@pytest.fixture
def restore_sigint():
    """信号处理器是进程级的，测完要还回去，别影响后面的用例。"""
    previous = signal.getsignal(signal.SIGINT)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def test_handler_is_installed_and_invoked(restore_sigint):
    seen: list[int] = []
    installed = install_shutdown_handler(seen.append)
    assert installed, "主线程里应该至少能装上 SIGINT"
    assert int(signal.SIGINT) in installed

    signal.raise_signal(signal.SIGINT)
    assert seen == [int(signal.SIGINT)]

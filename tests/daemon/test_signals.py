"""终止信号 → 回调。

守护进程靠这个把 SIGINT / SIGTERM 变成"请求停止"，所以它得真的能装上、真的会被调用。
"""

from __future__ import annotations

import signal

import pytest

from agentic_tty.daemon.platform.signals import install_shutdown_handler


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
    assert installed.numbers, "主线程里应该至少能装上 SIGINT"
    assert int(signal.SIGINT) in installed.numbers

    signal.raise_signal(signal.SIGINT)
    assert seen == [int(signal.SIGINT)]


def test_restore_puts_the_previous_handler_back(restore_sigint):
    """信号处理器是进程级的——装完不还原就会泄漏到调用方进程里。"""
    before = signal.getsignal(signal.SIGINT)
    installed = install_shutdown_handler(lambda _signum: None)
    assert signal.getsignal(signal.SIGINT) is not before
    installed.restore()
    assert signal.getsignal(signal.SIGINT) is before

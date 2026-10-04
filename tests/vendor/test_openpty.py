"""`vendor/openpty` 的契约测试：真起 openpty 会话。

只在 Linux 上跑——本模块依赖 `fcntl` / `termios` / `os.forkpty`。
"""

from __future__ import annotations

import os
import sys
import time

import pytest

if sys.platform != "linux":
    pytest.skip("openpty 只在 Linux 上实现", allow_module_level=True)

import openpty

from agentic_tty.core.runtime.process_tree import ProcessTree

_WAIT_LIMIT = 30.0
"""等条件的时限（排空 / 进程退出）。

判定看条件，秒表只用来兜真正的回归：机器忙时进程起得慢，按固定秒表卡会把“慢”
误报成“回归”。
"""

_LONG_RUNNING = [sys.executable, "-c", "import time; time.sleep(30)"]


def _read_until_eof(pty, deadline_s: float = _WAIT_LIMIT) -> str:
    """读到宿主说排空为止——与生产驱动循环（`StreamReader`）用的是同一条判据。"""
    deadline = time.monotonic() + deadline_s
    out = bytearray()
    while time.monotonic() < deadline:
        chunk = pty.read(65536, timeout=0.2)
        if chunk:
            out.extend(chunk)
            continue
        if pty.poll_eof():
            break
    return out.decode("utf-8", "replace")


def _drain_for(pty, seconds: float) -> str:
    """读固定时长，不等排空——交互用例要的是“这段时间里说了什么”。"""
    deadline = time.monotonic() + seconds
    out = bytearray()
    while time.monotonic() < deadline:
        chunk = pty.read(65536, timeout=0.1)
        if chunk:
            out.extend(chunk)
    return out.decode("utf-8", "replace")


def _wait_exit(pty, deadline_s: float = _WAIT_LIMIT) -> int | None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        code = pty.try_wait()
        if code is not None:
            return code
        time.sleep(0.05)
    return None


def test_reads_output_and_reports_exit():
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn(["/bin/sh", "-c", "echo openpty-marker"])
        assert "openpty-marker" in _read_until_eof(pty)
        assert pty.try_wait() == 0
    finally:
        pty.close()


def test_child_is_a_session_leader_with_controlling_terminal():
    """回归：子进程必须拿到**控制终端**。

    只 `setsid()` 而不 `login_tty`（TIOCSCTTY）的话，子进程是新会话却没有控制终端，
    交互式 shell 的 job control 会失效、Ctrl-C 也传不到前台进程组。
    """
    code = (
        "import os;"
        "print('isatty', os.isatty(0));"
        "print('leader', os.getsid(0) == os.getpid());"
        "print('ctty', os.tcgetpgrp(0) == os.getpgrp())"
    )
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn([sys.executable, "-c", code])
        out = _read_until_eof(pty)
        assert "isatty True" in out, out
        assert "leader True" in out, out
        assert "ctty True" in out, out
    finally:
        pty.close()


def test_spawn_failure_is_reported():
    """命令不存在要**同步**报错，不能变成一个「先进注册表、随即死掉」的会话。"""
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        with pytest.raises(OSError, match="no-such-program"):
            pty.spawn(["no-such-program"])
    finally:
        pty.close()


def test_read_timeout_returns_empty_without_eof():
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn(_LONG_RUNNING)
        _drain_for(pty, 0.5)
        assert pty.read(65536, timeout=0.2) == b""
        assert not pty.poll_eof()
        assert pty.try_wait() is None
    finally:
        pty.close()


def test_interactive_roundtrip():
    """写命令 → 读回显。PTY 的“回车”是 LF（POSIX 行规程 ICRNL 会处理）。"""
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn(["/bin/sh"])
        _drain_for(pty, 1.0)
        pty.write(b"echo roundtrip-ok\n")
        assert "roundtrip-ok" in _drain_for(pty, 2.0)
    finally:
        pty.close()


def test_resize_keeps_session_usable():
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn(["/bin/sh"])
        _drain_for(pty, 1.0)
        pty.resize(120, 40)
        pty.write(b"echo after-resize\n")
        assert "after-resize" in _drain_for(pty, 2.0)
        assert pty.try_wait() is None
    finally:
        pty.close()


def test_terminating_the_tree_ends_the_session():
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pid = pty.spawn(_LONG_RUNNING)
        _drain_for(pty, 0.5)
        assert pty.try_wait() is None
        ProcessTree(pid, None).kill()
        assert _wait_exit(pty) is not None
    finally:
        pty.close()


def test_poll_eof_becomes_true_after_child_exits():
    pty = openpty.OpenPty(cols=80, rows=24)
    try:
        pty.spawn(["/bin/sh", "-c", "echo bye"])
        _read_until_eof(pty)
        assert pty.poll_eof()
    finally:
        pty.close()


def test_close_reaps_the_child():
    """关闭时要补收子进程。

    调用方是「先强杀、再非阻塞查退出码、最后关闭」，查的那一下与关闭之间有个窗口，
    子进程可能刚好在那时退出——漏收的话它会一直留在僵尸态。
    """
    pty = openpty.OpenPty(cols=80, rows=24)
    pid = pty.spawn(_LONG_RUNNING)
    ProcessTree(pid, None).kill()
    pty.close()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_close_is_idempotent():
    pty = openpty.OpenPty(cols=80, rows=24)
    pty.spawn(["/bin/sh", "-c", "echo x"])
    pty.close()
    pty.close()
    assert pty.read(65536, timeout=0.1) == b""
    assert pty.try_wait() is None

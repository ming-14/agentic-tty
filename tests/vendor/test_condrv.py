"""`vendor/condrv` 的契约测试：真起 ConDrv 会话。

需要 Windows 与自带的 conhost 宿主（`vendor/condrv/OpenConsole.exe`，按
`vendor/README.md` 补齐）——缺任一条即整体跳过。
"""

from __future__ import annotations

import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("ConDrv 只在 Windows 上存在", allow_module_level=True)

import condrv

from agentic_tty.core.runtime.process_tree import (
    ProcessTree,
    close_job,
    create_job,
)

try:
    condrv.find_conhost()
except FileNotFoundError as exc:  # pragma: no cover - 取决于本机是否补齐二进制
    pytest.skip(str(exc), allow_module_level=True)


_WAIT_LIMIT = 30.0
"""等条件的时限（排空 / 作业里出现成员）。

判定看条件，秒表只用来兜真正的回归：机器忙时进程起得慢，按固定秒表卡会把“慢”
误报成“回归”。
"""

_LONG_RUNNING = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def job():
    """会话作业对象：子进程在创建时即入作业，强杀与枚举都靠它。"""
    handle = create_job()
    assert handle is not None, "创建作业对象失败"
    try:
        yield handle
    finally:
        close_job(handle)


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


def test_reads_output_and_reports_exit(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pty.spawn(["cmd.exe", "/c", "echo condrv-marker"], job_handle=job)
        assert "condrv-marker" in _read_until_eof(pty)
        assert pty.try_wait() == 0
    finally:
        pty.close()


def test_grandchild_spawned_immediately_stays_in_job(job):
    """回归：子进程必须“创建时即入作业”。

    作业列表靠 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 生效，而属性表只在
    `EXTENDED_STARTUPINFO_PRESENT` 下才被 `CreateProcessW` 处理——标志写错的话
    属性表整张被忽略，子进程立刻 fork 出的孙进程就会逃出作业，从此既枚举不到也
    杀不到。
    """
    code = (
        "import subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']);"
        "print(g.pid, flush=True);"
        "time.sleep(10)"
    )
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pid = pty.spawn([sys.executable, "-c", code], job_handle=job)
        tree = ProcessTree(pid, job)
        members: tuple[int, ...] = ()
        deadline = time.monotonic() + _WAIT_LIMIT
        while not members and time.monotonic() < deadline:
            members = tree.descendants()
            time.sleep(0.05)
        # 作业里恰好是孙进程一个：它没逃逸，conhost 也没混进来
        assert len(members) == 1, f"作业成员 {members}，根 pid {pid}"
    finally:
        pty.close()


def test_read_timeout_returns_empty_without_eof(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pty.spawn(_LONG_RUNNING, job_handle=job)
        _drain_for(pty, 0.5)
        assert pty.read(65536, timeout=0.2) == b""
        assert not pty.poll_eof()
        assert pty.try_wait() is None
    finally:
        pty.close()


def test_interactive_roundtrip(job):
    """提示符 → 写命令 → 读回显。PTY 的“回车”是 CR。"""
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pty.spawn(["cmd.exe"], job_handle=job)
        assert ">" in _drain_for(pty, 2.0)
        pty.write(b"echo roundtrip-ok\r")
        assert "roundtrip-ok" in _drain_for(pty, 2.0)
        pty.write(b"exit\r")
        assert _wait_exit(pty) == 0
    finally:
        pty.close()


def test_resize_keeps_session_usable(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pty.spawn(["cmd.exe"], job_handle=job)
        _drain_for(pty, 1.0)
        pty.resize(120, 40)
        pty.write(b"echo after-resize\r")
        assert "after-resize" in _drain_for(pty, 2.0)
        assert pty.try_wait() is None
    finally:
        pty.close()


def test_terminating_the_job_ends_the_session(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pid = pty.spawn(_LONG_RUNNING, job_handle=job)
        _drain_for(pty, 0.5)
        assert pty.try_wait() is None
        ProcessTree(pid, job).kill()
        assert _wait_exit(pty) is not None
    finally:
        pty.close()


def test_poll_eof_becomes_true_after_exit_and_quiet(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        pty.spawn(["cmd.exe", "/c", "echo bye"], job_handle=job)
        _read_until_eof(pty)
        assert pty.poll_eof()
    finally:
        pty.close()


def test_spawn_failure_is_reported(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    try:
        with pytest.raises(OSError):
            pty.spawn(["no-such-program.exe"], job_handle=job)
    finally:
        pty.close()


def test_close_is_idempotent(job):
    pty = condrv.ConDrvPty(cols=80, rows=24)
    pty.spawn(["cmd.exe", "/c", "echo x"], job_handle=job)
    pty.close()
    pty.close()
    assert pty.read(65536, timeout=0.1) == b""
    assert pty.try_wait() is None

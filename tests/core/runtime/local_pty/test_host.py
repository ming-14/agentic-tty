"""`localpty` 宿主的契约测试：真起一个平台原语会话。

需要本平台的原语可用（Windows 还要有 `vendor/condrv/OpenConsole.exe`），缺任一条即
整体跳过。
"""

from __future__ import annotations

import sys
import time

import pytest

from agentic_tty.core.ports import LOCALPTY, SessionSpec
from agentic_tty.core.runtime.local_pty.console import require_primitive
from agentic_tty.core.runtime.local_pty.host import LocalPtyHost


def _primitive_available() -> bool:
    try:
        primitive = require_primitive()
        if sys.platform == "win32":
            primitive.find_conhost()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _primitive_available(), reason="平台 PTY 原语不可用")

_WAIT_LIMIT = 30.0
"""等条件的时限（排空 / 作业里出现成员）。

判定看条件，秒表只用来兜真正的回归：机器忙时进程起得慢，按固定秒表卡会把“慢”
误报成“回归”。
"""

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _echo_argv() -> list[str]:
    if sys.platform == "win32":
        return ["cmd.exe", "/c", "echo localpty-hello"]
    return ["/bin/sh", "-c", "echo localpty-hello"]


def _host(**kwargs) -> LocalPtyHost:
    return LocalPtyHost(SessionSpec(mode=LOCALPTY, argv=_echo_argv(), **kwargs))


def _drain(host, deadline_s: float = _WAIT_LIMIT) -> bytes:
    """带截止时间地读空——与生产驱动循环（`StreamReader`）用的是同一条判据。"""
    deadline = time.monotonic() + deadline_s
    raw = b""
    while time.monotonic() < deadline:
        chunk = host.read(timeout=0.2)
        if chunk:
            raw += chunk
            host.ingest(chunk)
            continue
        if host.poll_eof():
            break
    return raw


def test_read_feed_and_screen_text():
    host = _host()
    try:
        assert b"localpty-hello" in _drain(host)
        assert "localpty-hello" in host.screen_text()
        assert host.try_wait() == 0
    finally:
        host.kill()
        host.close()


def test_screen_views():
    host = _host(cols=40, rows=6)
    try:
        _drain(host)
        assert "localpty-hello" in host.full_text()
        cells = host.screen_cells()
        assert len(cells) == 6  # 行数等于 rows
        assert any("localpty-hello" in "".join(row) for row in cells)
    finally:
        host.kill()
        host.close()


def test_resize_changes_the_grid():
    host = _host(cols=40, rows=6)
    try:
        _drain(host)
        host.resize(80, 12)
        assert len(host.screen_cells()) == 12
    finally:
        host.kill()
        host.close()


def test_rebuild_bytes_contains_screen_content():
    host = _host()
    try:
        _drain(host)
        rebuild = host.rebuild_bytes()
        assert rebuild.startswith(b"\x1bc")  # RIS 打头
        assert b"localpty-hello" in rebuild
    finally:
        host.kill()
        host.close()


def test_render_svg_contains_screen_text():
    host = _host()
    try:
        _drain(host)
        svg = host.render_svg()
        assert svg.startswith("<svg")
        assert "localpty-hello" in svg
    finally:
        host.kill()
        host.close()


def test_render_image_is_png():
    host = _host()
    try:
        _drain(host)
        assert host.render_image(scale=1.0, fmt="png").startswith(_PNG_MAGIC)
    finally:
        host.kill()
        host.close()


def test_metadata_reports_the_program_title():
    """`localpty` 的模型认 OSC 0/2，拿得到程序设的标题（`pty` 后端拿不到）。"""
    host = _host()
    try:
        _drain(host)
        assert host.metadata().title
    finally:
        host.kill()
        host.close()


def test_read_returns_empty_after_close():
    host = _host()
    host.kill()
    host.close()
    assert host.read(timeout=0.2) == b""


@pytest.mark.skipif(sys.platform != "win32", reason="作业对象只有 Windows 有")
def test_grandchild_spawned_immediately_stays_in_job():
    """回归：PTY 子进程必须“创建时即入作业”。

    入作业若发生在创建之后，子进程先 fork 出的孙进程会逃出作业，从此既枚举不到也杀不到。
    """
    code = (
        "import subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']);"
        "print(g.pid, flush=True);"
        "time.sleep(10)"
    )
    host = LocalPtyHost(SessionSpec(mode=LOCALPTY, argv=[sys.executable, "-c", code]))
    try:
        members: tuple[int, ...] = ()
        deadline = time.monotonic() + _WAIT_LIMIT
        while not members and time.monotonic() < deadline:
            members = host.descendants()
            time.sleep(0.05)
        # 作业里恰好是“根 + 孙进程”：孙进程没逃逸，conhost 也没混进来
        assert len(members) == 1, f"作业成员 {members}，根 pid {host.pid}"
    finally:
        host.kill()
        host.close()

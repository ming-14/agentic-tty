"""pywezterm 宿主测试（需要 vendor 里的原生扩展，缺失则整体跳过）。"""

from __future__ import annotations

import sys
import time

import pytest

from agentic_tty.core.ports import PTY, SessionSpec
from agentic_tty.core.runtime.pywezterm_pty.host import PtyHost


def _pty_available() -> bool:
    try:
        from agentic_tty.core.runtime.pywezterm_pty.host import require_pywezterm

        require_pywezterm()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pty_available(), reason="pywezterm 不可用")


_WAIT_LIMIT = 30.0
"""等条件的时限（排空 / 作业里出现成员）。

判定看条件，秒表只用来兜真正的回归：机器忙时进程起得慢，按固定秒表卡会把"慢"
误报成"回归"。"""


def _echo_argv() -> list[str]:
    if sys.platform == "win32":
        return ["cmd.exe", "/c", "echo pty-hello"]
    return ["/bin/sh", "-c", "echo pty-hello"]


def _drain(host, deadline_s: float = _WAIT_LIMIT) -> bytes:
    """带截止时间地读空。

    PTY 在进程退出后不返回 EOF，不能无限等；排空由宿主判定（`poll_eof`），
    与生产驱动循环（`StreamReader`）用的是同一条判据。
    """
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


def test_pty_read_feed_and_screen_text():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    try:
        raw = _drain(host)
        assert b"pty-hello" in raw
        assert "pty-hello" in host.screen_text()
        assert host.try_wait() == 0
    finally:
        host.kill()
        host.close()


def test_pty_resize_updates_both_sides():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv(), cols=80, rows=24))
    try:
        host.resize(120, 40)
        assert host._pty.get_size() == (120, 40)
    finally:
        host.kill()
        host.close()


def test_pty_rebuild_bytes_contains_screen_content():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    try:
        _drain(host)
        rebuild = host.rebuild_bytes()
        assert rebuild.startswith(b"\x1bc")  # RIS 打头
        assert b"pty-hello" in rebuild
    finally:
        host.kill()
        host.close()


def test_pty_screen_views():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv(), cols=40, rows=6))
    try:
        _drain(host)
        assert "pty-hello" in host.screen_text()
        assert "pty-hello" in host.full_text()

        cells = host.screen_cells()
        assert len(cells) == 6  # 行数等于 rows
        assert any("pty-hello" in "".join(row) for row in cells)
    finally:
        host.kill()
        host.close()


def test_pty_screen_cells_fills_wide_char_continuation_cells():
    """宽字符占两格：续格是空串。行尾空白（含行尾宽字符的续格）不进格栅。"""
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv(), cols=20, rows=6))
    try:
        host.ingest("你好ab".encode())
        host.ingest(b"\r\n")
        host.ingest("ab你好".encode())
        host.ingest(b"\r\n")
        host.ingest(b"ab  ")
        rows = host.screen_cells()
        assert rows[0] == ("你", "", "好", "", "a", "b")
        assert rows[1] == ("a", "b", "你", "", "好")
        assert rows[2] == ("a", "b")
    finally:
        host.kill()
        host.close()


def test_pty_render_svg_contains_screen_text():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    try:
        _drain(host)
        svg = host.render_svg()
        assert svg.startswith("<svg")
        assert "pty-hello" in svg
    finally:
        host.kill()
        host.close()


@pytest.mark.parametrize(
    ("fmt", "magic"),
    [("png", b"\x89PNG\r\n\x1a\n"), ("jpg", b"\xff\xd8\xff"), ("bmp", b"BM")],
)
def test_pty_render_image_formats(fmt, magic):
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    try:
        _drain(host)
        image = host.render_image(scale=1.0, fmt=fmt)
        assert image.startswith(magic)
    finally:
        host.kill()
        host.close()


def test_pty_read_returns_empty_after_close():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    host.kill()
    host.close()
    assert host.read(timeout=0.2) == b""


def test_pty_grandchild_forked_immediately_stays_in_job():
    """回归：PTY 子进程必须"创建时即入作业"。

    入作业若发生在创建之后，子进程先 fork 出的孙进程会逃出作业，从此既枚举不到也杀不到。
    """
    code = (
        "import subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']);"
        "print(g.pid, flush=True);"
        "time.sleep(10)"
    )
    host = PtyHost(SessionSpec(mode=PTY, argv=[sys.executable, "-c", code]))
    try:
        members: tuple[int, ...] = ()
        deadline = time.monotonic() + _WAIT_LIMIT
        while not members and time.monotonic() < deadline:
            members = host.descendants()
            time.sleep(0.05)
        # 作业里恰好是"根 + 孙进程"：孙进程没逃逸，conhost 也没混进来
        assert len(members) == 1, f"作业成员 {members}，根 pid {host.pid}"
    finally:
        host.kill()
        host.close()

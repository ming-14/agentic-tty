"""pywezterm 宿主测试（需要 vendor 里的原生扩展，缺失则整体跳过）。"""

from __future__ import annotations

import sys
import time

import pytest

from agentic_tty.core.ports import PTY, SessionSpec
from agentic_tty.runtime.pty_host import PtyHost


def _pty_available() -> bool:
    try:
        from agentic_tty.runtime.pty_host import require_pywezterm

        require_pywezterm()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pty_available(), reason="pywezterm 不可用")


def _echo_argv() -> list[str]:
    if sys.platform == "win32":
        return ["cmd.exe", "/c", "echo pty-hello"]
    return ["/bin/sh", "-c", "echo pty-hello"]


def _drain(host, deadline_s: float = 5.0) -> bytes:
    """带截止时间地读空（PTY 在进程退出后不返回 EOF，不能无限等）。"""
    deadline = time.monotonic() + deadline_s
    raw = b""
    while time.monotonic() < deadline:
        chunk = host.read(timeout=0.2)
        if chunk:
            raw += chunk
            host.ingest(chunk)
            continue
        if host.try_wait() is not None:
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


def test_pty_snapshot_contains_screen_content():
    host = PtyHost(SessionSpec(mode=PTY, argv=_echo_argv()))
    try:
        _drain(host)
        snapshot = host.snapshot()
        assert snapshot.startswith(b"\x1bc")  # RIS 打头
        assert b"pty-hello" in snapshot
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

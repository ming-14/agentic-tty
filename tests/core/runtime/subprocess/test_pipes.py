"""`subprocess` 的原语与启动器：裸 fd 读、双管道 IO 面、自带 launcher。

宿主侧的端口语义在 `test_host.py`；这里管 `pipes.py` 自己那层——`read_fd` 的三态
（超时 / 有数据 / EOF）、`PopenPipes` 的读写与关闭、`open_process` 交出的东西。
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from agentic_tty.core.ports import SUBPROCESS, SessionSpec
from agentic_tty.core.runtime.errors import HostSpawnError
from agentic_tty.core.runtime.process_tree import ProcessTree
from agentic_tty.core.runtime.subprocess.pipes import (
    PopenPipes,
    ProcessConsole,
    open_process,
    read_fd,
)


def _spec(argv: list[str]) -> SessionSpec:
    return SessionSpec(mode=SUBPROCESS, argv=argv)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _wait_exit(console: ProcessConsole, timeout: float) -> int | None:
    """带超时地等退出码。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = console.pipes.try_wait()
        if code is not None:
            return code
        time.sleep(0.01)
    return None


# ── read_fd ────────────────────────────────────────────────────────────


def test_read_fd_returns_empty_when_nothing_arrives():
    read_end, write_end = os.pipe()
    try:
        assert read_fd(read_end, 64, 0.05) == b""
    finally:
        os.close(read_end)
        os.close(write_end)


def test_read_fd_returns_what_was_written_then_eof():
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, b"hello")
        assert read_fd(read_end, 64, 0.2) == b"hello"
        os.close(write_end)  # 写端全关：读端下一次就是 EOF
        assert read_fd(read_end, 64, 0.2) == b""
    finally:
        os.close(read_end)


def test_read_fd_blocking_mode_reads_to_eof():
    """`timeout=None` 走阻塞读：数据拿得到，EOF 也一样返回空。"""
    read_end, write_end = os.pipe()
    os.write(write_end, b"abc")
    os.close(write_end)
    try:
        assert read_fd(read_end, 64, None) == b"abc"
        assert read_fd(read_end, 64, None) == b""
    finally:
        os.close(read_end)


# ── PopenPipes ─────────────────────────────────────────────────────────


def test_pipes_read_the_two_streams_separately():
    console = open_process(
        _spec(_py("import sys;sys.stdout.write('out');sys.stderr.write('err')"))
    )
    try:
        assert console.pipes.read(timeout=None) == b"out"
        assert console.pipes.read_stderr(timeout=None) == b"err"
    finally:
        console.tree.kill()
        console.pipes.close()


def test_pipes_write_and_close_stdin():
    console = open_process(_spec(_py("import sys;sys.stdout.write(sys.stdin.read())")))
    try:
        console.pipes.write(b"hello stdin")
        console.pipes.close_stdin()  # 发 EOF，子进程才肯往下走
        assert console.pipes.read(timeout=None) == b"hello stdin"
    finally:
        console.pipes.close()


def test_pipes_stop_answering_after_close():
    console = open_process(_spec(_py("import time;time.sleep(30)")))
    console.tree.kill()
    try:
        assert _wait_exit(console, 3.0) is not None
    finally:
        console.pipes.close()
    assert console.pipes.try_wait() is None
    assert console.pipes.read(timeout=0.05) == b""
    assert console.pipes.read_stderr(timeout=0.05) == b""


# ── open_process ───────────────────────────────────────────────────────


def test_open_process_hands_back_pipes_pid_and_tree():
    console = open_process(_spec(_py("import time;time.sleep(30)")))
    try:
        assert isinstance(console.pipes, PopenPipes)
        assert isinstance(console.tree, ProcessTree)
        assert console.pid > 0
    finally:
        console.tree.kill()
        console.pipes.close()


def test_open_process_reports_a_spawn_failure():
    with pytest.raises(HostSpawnError):
        open_process(_spec(["definitely-not-a-real-program-xyz"]))

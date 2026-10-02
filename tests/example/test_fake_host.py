from __future__ import annotations

from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec
from agentic_tty.example.runtime_fakehost.fake_host import FakeHost, FakeProgram

_MAX = 65536


def _spec(mode: str = SUBPROCESS) -> SessionSpec:
    return SessionSpec(mode=mode, argv=("x",))


def test_scripted_output_arrives_over_time():
    host = FakeHost(_spec(), FakeProgram(chunks=((0.0, b"a"), (0.05, b"b"))))
    assert host.read(_MAX, timeout=0) == b"a"
    assert host.read(_MAX, timeout=0) == b""  # b 还没到期
    assert host.read(_MAX, timeout=0.5) == b"b"


def test_read_respects_max_bytes_and_keeps_remainder():
    host = FakeHost(_spec(), FakeProgram(chunks=((0.0, b"abcdef"),)))
    assert host.read(3, timeout=0) == b"abc"
    assert host.read(_MAX, timeout=0) == b"def"


def test_stderr_is_a_separate_queue():
    host = FakeHost(_spec(), FakeProgram(stderr_chunks=((0.0, b"err"),)))
    assert host.read(_MAX, timeout=0) == b""
    assert host.read_stderr(_MAX, timeout=0) == b"err"


def test_exit_after():
    host = FakeHost(_spec(), FakeProgram(exit_after=0.0, exit_code=7))
    assert host.try_wait() == 7


def test_echo_and_respond_on_write():
    host = FakeHost(
        _spec(),
        FakeProgram(echo_input=True, respond=lambda line: b"got:" + line.strip()),
    )
    host.write(b"hi\n")
    assert host.read(_MAX, timeout=0.5) == b"hi\n"
    assert host.read(_MAX, timeout=0.5) == b"got:hi"


def test_close_stdin_blocks_further_writes():
    host = FakeHost(_spec(), FakeProgram())
    host.close_stdin()
    host.write(b"x")
    assert host.received_input == b""
    assert host.stdin_closed


def test_ingest_accumulates_a_plain_text_screen():
    host = FakeHost(_spec(PTY), FakeProgram(title="demo"))
    host.ingest(b"hello ")
    host.ingest(b"world")
    assert host.rebuild_bytes() == b"hello world"
    assert host.screen_text() == "hello world"
    assert host.full_text() == "hello world"  # 假宿主没有滚动历史
    assert host.metadata().title == "demo"


def test_screen_cells_splits_lines():
    host = FakeHost(_spec(PTY), FakeProgram())
    assert host.screen_cells() == ()  # 空屏幕没有行
    host.ingest(b"ab\nc")
    assert host.screen_cells() == (("a", "b"), ("c",))


def test_kill_marks_exited():
    host = FakeHost(_spec(), FakeProgram())
    assert host.try_wait() is None
    host.kill()
    assert host.try_wait() == 0

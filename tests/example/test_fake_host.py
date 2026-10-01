from __future__ import annotations

import pytest

from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec
from agentic_tty.example.fake_host import FakeHost, FakeProgram

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
    assert host.snapshot() == b"hello world"
    assert host.metadata().title == "demo"


def test_kill_marks_exited():
    host = FakeHost(_spec(), FakeProgram())
    assert host.try_wait() is None
    host.kill()
    assert host.try_wait() == 0


def test_render_svg_wraps_escaped_screen_text():
    host = FakeHost(_spec(PTY), FakeProgram())
    host.ingest(b"a & <b>")
    assert host.render_svg() == (
        '<svg xmlns="http://www.w3.org/2000/svg"><text>a &amp; &lt;b&gt;</text></svg>'
    )


def test_render_image_is_not_supported():
    host = FakeHost(_spec(PTY), FakeProgram())
    with pytest.raises(NotImplementedError):
        host.render_image()

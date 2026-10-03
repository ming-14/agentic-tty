"""daemon_test 的最小请求处理层：把请求翻成核心层操作。"""

from __future__ import annotations

from agentic_tty.core.ports import PTY
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.example.core_test.runtime_fakehost.fake_host import FakeHost, FakeProgram
from agentic_tty.example.daemon_test.handler import Answer, KernelHandler, Request


def _handler(program: FakeProgram | None = None) -> KernelHandler:
    program = program or FakeProgram()
    return KernelHandler(SessionRegistry(lambda spec: FakeHost(spec, program)))


def test_create_list_and_close_round_trip():
    handler = _handler()
    reply = handler.handle(Request("create", sid="t-1", argv=("x",), mode=PTY))
    assert isinstance(reply.answer, Answer)
    assert reply.answer.ok
    listed = handler.handle(Request("list"))
    assert [row["sid"] for row in listed.answer.data["sessions"]] == ["t-1"]
    handler.handle(Request("close", sid="t-1"))
    assert handler.handle(Request("list")).answer.data["sessions"] == []
    handler.shutdown()


def test_detail_reports_resized_dimensions():
    """resize 之后 detail 必须报新尺寸——spec 是 frozen 的，读它会拿到创建时的旧值。"""
    handler = _handler()
    handler.handle(Request("create", sid="t-1", argv=("x",), mode=PTY))
    handler.handle(Request("resize", sid="t-1", cols=100, rows=30))
    data = handler.handle(Request("detail", sid="t-1")).answer.data
    assert (data["cols"], data["rows"]) == (100, 30)
    handler.shutdown()


def test_failure_is_reported_not_raised():
    handler = _handler()
    reply = handler.handle(Request("detail", sid="missing"))
    assert isinstance(reply.answer, Answer)
    assert not reply.answer.ok
    handler.shutdown()


def test_detail_exposes_terminal_metadata():
    """终端标题 / cwd 经 detail 暴露（仅 Pty 会话有）。"""
    handler = _handler(FakeProgram(title="demo"))
    handler.handle(Request("create", sid="t-1", argv=("x",), mode=PTY))
    data = handler.handle(Request("detail", sid="t-1")).answer.data
    assert data["title"] == "demo"
    handler.shutdown()

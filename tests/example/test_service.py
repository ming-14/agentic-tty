"""演示请求处理层：不需要网络、不需要守护进程的那部分。"""

from __future__ import annotations

import pytest

from agentic_tty.core.ports import PTY, SessionSpec
from agentic_tty.core.terminal.session import TerminalSession
from agentic_tty.daemon.handler import RequestHandler
from agentic_tty.example.fake_host import FakeHost, FakeProgram
from agentic_tty.example.service import BadRequest, ExampleService, WaitSpec, _Activity
from agentic_tty.protocol.envelope import make_request
from agentic_tty.protocol.messages import error_of, ok_response


def test_service_satisfies_the_seam():
    assert isinstance(ExampleService(), RequestHandler)


def test_request_without_conditions_is_answered_immediately():
    service = ExampleService()
    reply = service.handle(make_request("get_daemon_status"))
    assert reply is not None
    assert reply.envelope.payload.output["ok"] is True
    assert service.poll() == []


def test_unknown_command_is_reported_not_ignored():
    service = ExampleService()
    reply = service.handle(make_request("nope"))
    assert reply is not None
    failure = error_of(reply.envelope)
    assert failure is not None and failure.code == "UnknownCommand"


def test_conditions_are_rejected_on_commands_that_never_wait():
    service = ExampleService()
    request = make_request("get_daemon_status", condition={"timeout": 1})
    reply = service.handle(request)
    assert reply is not None
    failure = error_of(reply.envelope)
    assert failure is not None and failure.code == "BadRequest"


def test_waiting_needs_an_existing_session():
    """会话不存在要立刻报，不能等超时才说。"""
    service = ExampleService()
    request = make_request("read_terminal", op={"sid": "ghost"}, condition={"timeout": 60})
    reply = service.handle(request)
    assert reply is not None
    failure = error_of(reply.envelope)
    assert failure is not None and failure.code == "NoSuchSession"


def test_unimplemented_condition_is_a_request_error():
    service = ExampleService()
    request = make_request("read_terminal", op={"sid": "s"}, condition={"matched": "x"})
    reply = service.handle(request)
    assert reply is not None
    assert error_of(reply.envelope).code == "BadRequest"


@pytest.mark.parametrize(
    ("condition", "match"),
    [
        ({"nope": 1}, "未知返回条件"),
        ({"timeout": 0}, "正的秒数"),
        ({"timeout": -1}, "正的秒数"),
        ({"idle": "soon"}, "正的秒数"),
        ({"ended": "yes"}, "布尔值"),
    ],
)
def test_bad_condition_shapes_are_rejected(condition, match):
    with pytest.raises(BadRequest, match=match):
        WaitSpec.parse(condition)


def test_empty_condition_means_return_now():
    spec = WaitSpec.parse({})
    assert spec.is_immediate


def test_conditions_round_trip():
    spec = WaitSpec.parse({"ended": True, "idle": 0.5, "timeout": 3})
    assert (spec.ended, spec.crashed, spec.idle, spec.timeout) == (True, False, 0.5, 3.0)
    assert not spec.is_immediate


def test_bad_request_from_a_command_becomes_a_failure_reply():
    service = ExampleService()
    reply = service.handle(make_request("create_terminal", op={"sid": "s"}))
    assert reply is not None
    failure = error_of(reply.envelope)
    assert failure is not None and failure.code == "BadRequest"


def test_remove_session_requires_a_target():
    service = ExampleService()
    reply = service.handle(make_request("remove_session"))
    assert reply is not None
    assert error_of(reply.envelope).code == "BadRequest"


def test_pty_idle_probe_skips_screen_render_without_new_bytes(monkeypatch):
    """空闲检测先比日志偏移：没有新字节就不该整屏渲染（空闲会话每 tick 都在跑）。"""
    service = ExampleService()
    session = TerminalSession(
        "uid-1",
        SessionSpec(mode=PTY, argv=("x",)),
        lambda spec: FakeHost(spec, FakeProgram()),
        journal_budget_bytes=1 << 16,
    )
    session.start()
    try:
        service._activity[session.uid] = _Activity(at=0.0)
        renders = 0
        real = TerminalSession.screen_text

        def counting(self):
            nonlocal renders
            renders += 1
            return real(self)

        monkeypatch.setattr(TerminalSession, "screen_text", counting)
        service._note_activity(session, 1.0)
        service._note_activity(session, 2.0)
        assert renders == 1
    finally:
        session.close()


def test_ok_response_helper_matches_what_the_service_returns():
    """服务用的是 protocol 里的那套响应约定，别在这儿另起一套。"""
    reply = ExampleService().handle(make_request("list_sessions"))
    assert reply is not None
    assert reply.envelope.payload.output["ok"] is True
    assert ok_response("x", "m").payload.output["ok"] is True

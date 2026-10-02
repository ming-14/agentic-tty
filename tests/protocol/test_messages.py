from __future__ import annotations

import pytest

from agentic_tty.protocol.envelope import make_request
from agentic_tty.protocol.errors import MessageError, ProtocolError
from agentic_tty.protocol.messages import (
    Command,
    Condition,
    DaemonStatus,
    Kind,
    ReadMode,
    SessionInfo,
    data_of,
    error_of,
    failed_response,
    is_ok,
    ok_response,
)


def test_command_names_are_the_wire_values():
    assert Command.CREATE_SHELL_TERMINAL == "create_shell_terminal"
    assert Command.CREATE_TERMINAL == "create_terminal"
    assert Command.INPUT_INTO_TERMINAL == "input_into_terminal"
    assert Condition.IDLE == "idle"
    assert ReadMode.BYTES == "bytes"


def test_image_mode_aligns_with_the_rendering_channel():
    """纯客户端没有终端模型，屏幕得由守护进程渲染好送过来。"""
    assert ReadMode.IMAGE.value == Kind.IMAGE.value == "image"


def test_condition_covers_exit_both_ways():
    """`ended` 与 `crashed` 是同一件事的两面，退出码决定命中哪个。"""
    assert Condition.ENDED == "ended"
    assert Condition.CRASHED == "crashed"


def test_unimplemented_conditions_are_absent_so_they_error_out_loudly():
    """未实现的条件不列出——客户端用了要拿到明确报错，而不是被静默忽略。"""
    assert not hasattr(Condition, "MATCHED")
    assert not hasattr(Condition, "NOTIFY")


def test_ok_response_roundtrips_through_data_of():
    response = ok_response("list_sessions", "m1", {"sessions": []})
    assert is_ok(response)
    assert error_of(response) is None
    assert data_of(response) == {"sessions": []}


def test_ok_response_without_data():
    response = ok_response("remove_session", "m1")
    assert data_of(response) == {}


def test_ok_response_carries_presentation_kind():
    response = ok_response("read_terminal", "m1", {"text": "hi"}, kind=Kind.SCREEN)
    assert response.kind == "screen"


def test_failed_response_roundtrips_through_error_of():
    response = failed_response(
        "read_terminal", "m1", "SessionNotFound", "会话不存在", extra={"sid": "s1"}
    )
    assert not is_ok(response)
    failure = error_of(response)
    assert failure is not None
    assert failure.code == "SessionNotFound"
    assert failure.message == "会话不存在"
    assert failure.extra == {"sid": "s1"}


def test_data_of_rejects_failure():
    with pytest.raises(ValueError, match="不是成功响应"):
        data_of(failed_response("x", "m", "E", "boom"))


def test_error_of_tolerates_malformed_error_field():
    request = make_request("x")
    response = ok_response("x", request.mid)
    object.__setattr__(response.payload, "output", {"ok": False})
    failure = error_of(response)
    assert failure is not None
    assert failure.code == "MalformedError"


def test_session_info_roundtrip():
    info = SessionInfo(
        sid="s1",
        mode="pty",
        state="running",
        running=True,
        drained=False,
        exit_code=None,
        tags=("build", "ci"),
        cols=100,
        rows=30,
    )
    restored = SessionInfo.from_dict(info.to_dict())
    assert restored == info
    assert info.to_dict()["tags"] == ["build", "ci"]


def test_session_size_is_optional():
    """只有终端会话有尺寸——客户端靠它把屏幕缩放到自己的画布。"""
    info = SessionInfo.from_dict(
        {"sid": "s", "mode": "subprocess", "state": "running", "running": True, "drained": False}
    )
    assert (info.cols, info.rows) == (None, None)


def test_session_info_never_carries_uid():
    """uid 是核心层内部的，不该出现在核心层之外。"""
    assert (
        "uid"
        not in SessionInfo.from_dict(
            {"sid": "s", "mode": "pty", "state": "running", "running": True, "drained": False}
        ).to_dict()
    )


def test_daemon_status_roundtrip():
    status = DaemonStatus(
        pid=1234,
        started_at="2026-10-01T21:00:00.000",
        uptime=12.5,
        sessions=2,
        listen="tcp://127.0.0.1:1",
    )
    assert DaemonStatus.from_dict(status.to_dict()) == status


def test_session_info_rejects_non_mapping():
    with pytest.raises(ProtocolError):
        SessionInfo.from_dict([])


def test_session_info_rejects_missing_field():
    with pytest.raises(MessageError, match="缺少字段"):
        SessionInfo.from_dict({"sid": "s"})


def test_daemon_status_rejects_missing_field():
    with pytest.raises(MessageError, match="缺少字段"):
        DaemonStatus.from_dict({"pid": 1})


def test_decoding_failures_share_one_base_class():
    """从线上解出来的东西不合法，调用方按 `ProtocolError` 一把兜住即可。"""
    assert issubclass(MessageError, ProtocolError)

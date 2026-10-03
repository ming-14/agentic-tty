"""下游消费者 ↔ 它的客户端：sid 级契约。"""

from __future__ import annotations

import pytest

from agentic_tty.protocol.contracts.consumer_ipc import (
    Command,
    Condition,
    SessionInfo,
    ViewMode,
)
from agentic_tty.protocol.errors import MessageError, ProtocolError


def test_command_names_are_the_wire_values():
    assert Command.CREATE_SHELL_TERMINAL == "create_shell_terminal"
    assert Command.CREATE_TERMINAL == "create_terminal"
    assert Command.INPUT_INTO_TERMINAL == "input_into_terminal"
    assert Command.READ_TERMINAL == "read_terminal"


def test_view_mode_values():
    assert ViewMode.SCREEN == "screen"
    assert ViewMode.BYTES == "bytes"


def test_condition_covers_exit_both_ways():
    """`ended` 与 `crashed` 是同一件事的两面，退出码决定命中哪个。"""
    assert Condition.ENDED == "ended"
    assert Condition.CRASHED == "crashed"


def test_unimplemented_conditions_are_absent_so_they_error_out_loudly():
    """未实现的条件不列出——客户端用了要拿到明确报错，而不是被静默忽略。"""
    assert not hasattr(Condition, "MATCHED")
    assert not hasattr(Condition, "NOTIFY")


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
    """uid 是核心层内部的，不该出现在这一侧。"""
    assert (
        "uid"
        not in SessionInfo.from_dict(
            {"sid": "s", "mode": "pty", "state": "running", "running": True, "drained": False}
        ).to_dict()
    )


def test_session_info_rejects_non_mapping():
    with pytest.raises(ProtocolError):
        SessionInfo.from_dict([])


def test_session_info_rejects_missing_field():
    with pytest.raises(MessageError, match="缺少字段"):
        SessionInfo.from_dict({"sid": "s"})

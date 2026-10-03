"""守护进程 ↔ 下游消费者：uid 级契约。"""

from __future__ import annotations

import pytest

from agentic_tty.core.ports import Stream
from agentic_tty.protocol.contracts.daemon_ipc import (
    STREAM_STDERR,
    STREAM_STDOUT,
    Command,
    DaemonStatus,
    Kind,
    ReadMode,
    SessionRef,
    stream_name,
    stream_tag,
)
from agentic_tty.protocol.errors import MessageError, ProtocolError


def test_uid_level_command_names():
    assert Command.CREATE_SESSION == "create_session"
    assert Command.READ_SESSION == "read_session"
    assert Command.WRITE_SESSION == "write_session"
    assert Command.SUBSCRIBE == "subscribe"


def test_stream_tag_roundtrip():
    assert stream_tag("stdout") == STREAM_STDOUT
    assert stream_tag("stderr") == STREAM_STDERR
    assert stream_name(stream_tag("stderr")) == "stderr"


def test_stream_names_match_the_core_enum():
    """流名必须与 core 的 `Stream` 取值一致——协议层不 import core，靠这条测试对齐。"""
    assert stream_name(STREAM_STDOUT) == Stream.STDOUT.value
    assert stream_name(STREAM_STDERR) == Stream.STDERR.value


def test_unknown_stream_is_rejected():
    with pytest.raises(MessageError, match="未知流"):
        stream_tag("stdin")


def test_unknown_stream_tag_is_rejected():
    with pytest.raises(MessageError, match="未知流标签"):
        stream_name(0x7F)


def test_image_mode_aligns_with_the_rendering_channel():
    """纯客户端没有终端模型，屏幕得由守护进程渲染好送过来。"""
    assert ReadMode.IMAGE.value == Kind.IMAGE.value == "image"


def test_session_ref_roundtrip():
    ref = SessionRef(
        uid="u1",
        command="bash",
        mode="pty",
        state="running",
        running=True,
        drained=False,
        exit_code=None,
        cols=100,
        rows=30,
        members=2,
    )
    assert SessionRef.from_dict(ref.to_dict()) == ref


def test_session_ref_size_is_optional():
    """只有终端会话有尺寸——客户端靠它把屏幕缩放到自己的画布。"""
    ref = SessionRef.from_dict(
        {
            "uid": "u",
            "command": "cat",
            "mode": "subprocess",
            "state": "running",
            "running": True,
            "drained": False,
        }
    )
    assert (ref.cols, ref.rows) == (None, None)
    assert ref.members is None


def test_session_ref_never_carries_sid_or_tags():
    """uid 级快照只有 uid——sid 与 tags 是消费者的语义。"""
    snapshot = SessionRef.from_dict(
        {
            "uid": "u",
            "command": "bash",
            "mode": "pty",
            "state": "running",
            "running": True,
            "drained": False,
        }
    ).to_dict()
    assert "sid" not in snapshot
    assert "tags" not in snapshot


def test_daemon_status_roundtrip():
    status = DaemonStatus(
        pid=1234,
        started_at="2026-10-01T21:00:00.000",
        uptime=12.5,
        sessions=2,
        listen="pipe://agentic-tty",
    )
    assert DaemonStatus.from_dict(status.to_dict()) == status


def test_session_ref_rejects_non_mapping():
    with pytest.raises(ProtocolError):
        SessionRef.from_dict([])


def test_session_ref_rejects_missing_field():
    with pytest.raises(MessageError, match="缺少字段"):
        SessionRef.from_dict({"uid": "u"})


def test_daemon_status_rejects_missing_field():
    with pytest.raises(MessageError, match="缺少字段"):
        DaemonStatus.from_dict({"pid": 1})

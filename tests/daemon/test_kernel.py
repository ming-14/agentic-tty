"""默认请求处理层（`daemon/kernel.py`）：订阅推送的**成帧**。

推送不经过接入点，用假宿主直接驱动 `poll` / `on_reply` 就够——不必起真管道，也不必碰
原生扩展。命令那半边由端到端测试（`tests/example/test_daemon_test.py`）覆盖。
"""

from __future__ import annotations

import logging

import pytest

from agentic_tty.core.ports import PTY, SessionSpec, Stream
from agentic_tty.core.session.base import Session
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.subscription import Subscription
from agentic_tty.daemon.access_point import ByteChunk, WireRequest
from agentic_tty.daemon.handler import Delivery
from agentic_tty.daemon.kernel import KernelHandler, _Sub
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram
from agentic_tty.protocol.contracts.daemon_ipc import Command, Event
from agentic_tty.protocol.envelope import Envelope, make_request

_CONNECTION = object()
"""订阅只拿连接当**键**（认它是什么的是接入点），所以测试里一个裸对象就够。"""


def _session() -> Session:
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))
    return registry.create(SessionSpec(mode=PTY, argv=("x",)))


def _watch(session: Session) -> KernelHandler:
    """把一条订阅直接挂进处理层——只验推送的成帧，不绕接入点。"""
    handler = KernelHandler()
    sub = _Sub(
        request=WireRequest(make_request(Command.SUBSCRIBE), _CONNECTION),
        session=session,
        stream=Stream.STDOUT,
        cursor=Subscription(session, cursor=0),
    )
    handler._subs[sub.request.envelope.mid] = sub
    return handler


def _frames(handler: KernelHandler, limit: int = 8) -> list[object]:
    """驱动 `poll` / `on_reply`，把这条订阅的帧按交付顺序取出来。"""
    frames: list[object] = []
    for _ in range(limit):
        replies = handler.poll()
        if not replies:
            break
        for reply in replies:
            frames.append(reply.answer)
            handler.on_reply(reply.request, Delivery.SENT)
    return frames


def _data(envelope: Envelope) -> dict:
    return dict(envelope.payload.output["data"])


class _Recorder:
    """假的停机通道：记下被调用过。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def request_stop(self) -> None:
        self._events.append("stop")


def test_push_splits_the_chunk_where_the_resize_lands():
    """尺寸变更落在字节段**中间**：按它切开，订阅者不必自己对齐 offset。"""
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"aaaa")
    session.resize(100, 30)
    session.ingest_stream(Stream.STDOUT, b"bbbb")
    handler = _watch(session)
    try:
        frames = _frames(handler)
    finally:
        session.close()

    assert [type(frame).__name__ for frame in frames] == ["ByteChunk", "Envelope", "ByteChunk"]
    assert frames[0].data == b"aaaa"
    assert frames[1].type == Event.RESIZE
    assert _data(frames[1]) == {"offset": 4, "cols": 100, "rows": 30}
    assert frames[2].data == b"bbbb"


def test_resize_at_the_chunk_start_comes_before_the_bytes():
    session = _session()
    session.resize(100, 30)
    session.ingest_stream(Stream.STDOUT, b"bbbb")
    handler = _watch(session)
    try:
        frames = _frames(handler)
    finally:
        session.close()

    assert [type(frame).__name__ for frame in frames] == ["Envelope", "ByteChunk"]
    assert frames[0].type == Event.RESIZE
    assert frames[1].data == b"bbbb"


def test_push_ends_once_the_session_is_drained():
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"hi")
    session.stop()  # 没有外部驱动时，退出即排空
    handler = _watch(session)
    try:
        frames = _frames(handler)
    finally:
        session.close()

    assert [type(frame).__name__ for frame in frames] == ["ByteChunk", "Envelope"]
    assert frames[0].data == b"hi"
    assert frames[1].type == Event.ENDED


def test_push_repeats_the_same_frame_while_the_delivery_is_congested():
    """没收下就原样重发、游标不动——`CONGESTED` 不能当成已交付。"""
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"hi")
    handler = _watch(session)
    try:
        first = handler.poll()
        assert len(first) == 1 and isinstance(first[0].answer, ByteChunk)
        assert first[0].answer.data == b"hi"

        handler.on_reply(first[0].request, Delivery.CONGESTED)

        again = handler.poll()
        assert len(again) == 1 and again[0].answer is first[0].answer
    finally:
        session.close()


def test_dropping_the_connection_drops_its_subscriptions():
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"hi")
    handler = _watch(session)
    try:
        assert handler.poll()  # 没注销之前是有东西推的
        handler.on_disconnected(_CONNECTION)
        assert handler.poll() == []
    finally:
        session.close()


def test_shutdown_daemon_acknowledges_before_stopping():
    """先答复、再置停机标志——客户端拿得到 ack，守护进程随后收尾。"""
    events: list[str] = []
    handler = KernelHandler()
    handler.bind(_Recorder(events))
    wire = WireRequest(make_request(Command.SHUTDOWN_DAEMON), _CONNECTION)

    reply = handler.handle(wire)
    assert reply is not None
    assert _data(reply.answer) == {"stopping": True}
    assert events == ["stop"]


def test_shutdown_daemon_without_a_channel_reports_not_stopping():
    """进程内嵌入 / 单测直接构造本层时没绑通道：记一条日志、照常答复，不抛异常。"""
    handler = KernelHandler()
    wire = WireRequest(make_request(Command.SHUTDOWN_DAEMON), _CONNECTION)
    reply = handler.handle(wire)

    assert reply is not None
    assert _data(reply.answer) == {"stopping": False}


@pytest.mark.parametrize(
    ("label", "op", "command"),
    [
        ("未知 mode", {"mode": "bogus"}, Command.READ_SESSION),
        ("未知 stream", {"mode": "bytes", "stream": "bogus"}, Command.READ_SESSION),
        ("tail 不可转数字", {"mode": "bytes", "tail": [1]}, Command.READ_SESSION),
        ("cols 不可转数字", {"cols": [1], "rows": 24}, Command.RESIZE_SESSION),
        ("未知命令", {}, "no_such_command"),
    ],
)
def test_peer_side_bad_input_becomes_a_failure_answer(label, op, command):
    """对端送来的东西不合法**一律收成失败答复**，不许以裸异常穿透 `handle`。

    `int()` 对列表抛 `TypeError`、枚举对不认识的值抛 `ValueError`——它们看着像程序错误，
    实际都是"对端发错了"。穿透出去会被上层记成守护进程内部故障，归因完全错位。
    """
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))
    handler = KernelHandler(registry)
    session = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    wire = WireRequest(make_request(command, op={"uid": session.uid, **op}), _CONNECTION)
    try:
        reply = handler.handle(wire)
        assert reply is not None, f"{label}: 没有交出失败答复"
        output = reply.answer.payload.output
        assert output["ok"] is False, f"{label}: 竟然成功了"
        assert output["error"]["code"] == "MessageError", f"{label}: 错误类型不对"
    finally:
        session.close()


def test_unexpected_error_still_reports_a_stack(caplog):
    """反过来：真出 bug（非 `AgenticTtyError`）必须留下堆栈——那才是该查的东西。"""
    handler = KernelHandler()
    handler._dispatch = lambda _wire: (_ for _ in ()).throw(RuntimeError("模拟内部 bug"))
    wire = WireRequest(make_request(Command.DAEMON_STATUS), _CONNECTION)
    with caplog.at_level(logging.ERROR, logger="agentic_tty.daemon.kernel"):
        reply = handler.handle(wire)
    assert reply is not None
    assert reply.answer.payload.output["error"]["code"] == "RuntimeError"
    assert any(record.exc_info for record in caplog.records), "意外错误没有记堆栈"


def test_host_spawn_failure_is_expected_not_a_stack(caplog):
    """宿主起不来（命令不存在 / 没权限）是**常见正常情形**，不该在日志里留下堆栈。

    `OSError` 是标准库类型、纳不进本工程的错误体系，但语义与 `AgenticTtyError` 同为
    "操作没做成"，所以并进可预期那一档。
    """
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))
    handler = KernelHandler(registry)
    handler._runtime.create = lambda _spec: (_ for _ in ()).throw(
        FileNotFoundError(2, "找不到文件")
    )
    wire = WireRequest(
        make_request(Command.CREATE_SESSION, op={"mode": PTY, "argv": ["nope"]}), _CONNECTION
    )
    with caplog.at_level(logging.ERROR, logger="agentic_tty.daemon.kernel"):
        reply = handler.handle(wire)
    assert reply is not None
    assert reply.answer.payload.output["error"]["code"] == "FileNotFoundError"
    assert not any(record.exc_info for record in caplog.records), "可预期的失败不该记堆栈"

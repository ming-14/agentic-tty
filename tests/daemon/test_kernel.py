"""默认请求处理层（`daemon/kernel.py`）：订阅推送的**成帧**。

推送不经过接入点，用假宿主直接驱动 `poll` / `on_reply` 就够——不必起真管道，也不必碰
原生扩展。命令那半边由端到端测试（`tests/example/test_daemon_test_console.py`）覆盖。
"""

from __future__ import annotations

import logging

import pytest

from agentic_tty.core.errors import CoreError
from agentic_tty.core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from agentic_tty.core.runtime.input_queue import InputVerdict
from agentic_tty.core.session.base import Session
from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.core.session.subscription import Subscription
from agentic_tty.daemon.access_point import ByteChunk, WireRequest
from agentic_tty.daemon.handler import Delivery, InputAction, Reply
from agentic_tty.daemon.kernel import KernelHandler, _frame_bytes, _Sub
from agentic_tty.example.core_test_console.runtime_fakehost import FakeHost, FakeProgram
from agentic_tty.protocol.contracts.daemon_ipc import Command, Event, ReadMode
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


def _frames(handler: KernelHandler, rounds: int = 8, room: int | None = None) -> list[object]:
    """驱动 `poll` / `on_reply`，把这条订阅的帧按交付顺序取出来。

    `rounds` 是**轮数上限**（一轮现在可以交好几帧）；测成帧顺序的用例只关心切帧对不对，
    所以给足轮数、额度不限额。
    """
    frames: list[object] = []
    for _ in range(rounds):
        replies = handler.poll(lambda _request: room)
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


def test_subscribe_ack_carries_the_baseline_size():
    """订阅 ack 带上起点处生效的尺寸——客户端解释第一段字节靠它。

    尺寸变更史可能已被日志裁剪裁短，随后的 resize 帧推不出基线。
    """
    session = _session()
    session.resize(100, 30)  # offset 0
    handler = KernelHandler()
    handler._runtime.get = lambda _uid: session  # type: ignore[method-assign]
    wire = WireRequest(
        make_request(Command.SUBSCRIBE, op={"uid": session.uid, "stream": Stream.STDOUT.value}),
        _CONNECTION,
    )
    try:
        reply = handler.handle(wire)
    finally:
        session.close()

    assert reply is not None
    payload = _data(reply.answer)
    assert (payload["cols"], payload["rows"]) == (100, 30)
    assert payload["offset"] == session.journal.end_offset


def test_resync_frame_carries_the_baseline_size():
    """游标被裁到保留区外：重同步帧带上基线尺寸，客户端才知道快照按多大解释。"""
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()), journal_budget_bytes=8)
    session = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    session.ingest_stream(Stream.STDOUT, b"abcd")
    session.resize(100, 30)
    handler = _watch(session)  # 游标 0，此刻仍在保留区内
    try:
        session.ingest_stream(Stream.STDOUT, b"x" * 64)  # 撑爆预算，游标被裁掉
        frames = _frames(handler)
    finally:
        session.close()

    resync = next(frame for frame in frames if frame.type == Event.RESYNC)
    assert resync.payload.output["data"]["cols"] == 100
    assert resync.payload.output["data"]["rows"] == 30


def test_subscribe_ack_has_no_size_for_a_stream_session():
    """没有屏幕的会话：尺寸缺省为 `None`，不硬凑一个。"""
    registry = SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))
    session = registry.create(SessionSpec(mode=SUBPROCESS, argv=("x",)))
    handler = KernelHandler()
    handler._runtime.get = lambda _uid: session  # type: ignore[method-assign]
    wire = WireRequest(
        make_request(Command.SUBSCRIBE, op={"uid": session.uid, "stream": Stream.STDOUT.value}),
        _CONNECTION,
    )
    try:
        reply = handler.handle(wire)
    finally:
        session.close()

    assert reply is not None
    payload = _data(reply.answer)
    assert payload["cols"] is None and payload["rows"] is None


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
        first = handler.poll(lambda _request: None)
        assert first and isinstance(first[0].answer, ByteChunk)
        assert first[0].answer.data == b"hi"

        handler.on_reply(first[0].request, Delivery.CONGESTED)

        again = handler.poll(lambda _request: None)
        assert again and again[0].answer is first[0].answer
    finally:
        session.close()


def _queued(handler: KernelHandler, count: int) -> _Sub:
    """手工挂一条订阅，`out` 里放好 `count` 个**内容各不相同**的帧。

    不接会话也不接游标（`done=True`）——这些用例只量"一轮交多少、退帧对不对"，
    不牵扯日志拉取；每个帧带上序号，才能可靠地认出"重发的是哪一帧"。`mid` 由
    `make_request` 自动生成，两条订阅天然不同。
    """
    sub = _Sub(
        request=WireRequest(make_request(Command.SUBSCRIBE), _CONNECTION),
        session=None,
        stream=None,
        cursor=None,
        done=True,
    )
    for index in range(count):
        sub.out.append(ByteChunk(3, "key", bytes([index]) * 8))
    handler._subs[sub.request.envelope.mid] = sub
    return sub


def _tags(replies: list) -> list[bytes]:
    return [reply.answer.data for reply in replies]


def test_a_round_hands_over_several_frames_within_the_room():
    """一轮在额度内**连交几帧**：突发输出几轮就能追平，而不是一帧一轮地磨。"""
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"x" * 200_000)  # 远多于一个 _PUSH_BUDGET
    handler = _watch(session)
    try:
        replies = handler.poll(lambda _request: 1 << 20)  # 额度给足
        assert len(replies) > 1, "一轮只交了一帧，额度没生效"
        for reply in replies:
            handler.on_reply(reply.request, Delivery.SENT)
    finally:
        session.close()


def test_the_room_caps_how_much_a_round_hands_over():
    """额度是**上限**：给得小就少交，不会把整段一次倒出去。"""
    handler = KernelHandler()
    sub = _queued(handler, 6)
    one_frame = _frame_bytes(sub.out[0])

    got = handler.poll(lambda _request: one_frame + 1)  # 只够一帧多一字节

    assert len(got) == 1, f"额度只够一帧却交了 {len(got)} 帧"
    assert _tags(got) == [b"\x00" * 8]


def test_the_frame_a_round_cannot_afford_stays_for_the_next_round():
    """额度不足的那帧**没被取走**——下一轮额度回来时它还在，一帧不丢、顺序不乱。"""
    handler = KernelHandler()
    sub = _queued(handler, 6)
    one_frame = _frame_bytes(sub.out[0])

    first = handler.poll(lambda _request: one_frame + 1)
    assert _tags(first) == [b"\x00" * 8]
    handler.on_reply(first[0].request, Delivery.SENT)

    rest = handler.poll(lambda _request: None)  # 额度放开
    assert _tags(rest) == [bytes([i]) * 8 for i in range(1, 6)], "没交的帧丢了或乱了"


def test_a_batch_congested_in_the_middle_keeps_the_rest_for_the_next_round():
    """批次**中途**被拒：收下的划掉、被拒的和其后的一起留到下一轮。

    只跳过本订阅：不丢帧，也不重复交已经收下的那些。
    """
    handler = KernelHandler()
    _queued(handler, 6)

    first = handler.poll(lambda _request: None)
    assert len(first) == 6, "一轮没把整批交出来，测不出中途被拒"

    handler.on_reply(first[0].request, Delivery.SENT)
    handler.on_reply(first[1].request, Delivery.SENT)
    handler.on_reply(first[2].request, Delivery.CONGESTED)

    again = handler.poll(lambda _request: None)
    assert _tags(again) == [bytes([i]) * 8 for i in range(2, 6)], "重发的不是被拒那帧起的余下几帧"


def test_one_subscription_being_refused_does_not_stall_another():
    """一条订阅全被拒，**不该拖住另一条**——各自记账、各走各的。

    交付一批时逐帧回告：若为一条堵住的连接就中断整批，排在后面的订阅交出去的帧
    拿不到回告，`handed` 会一直顶着上限，那条订阅再也发不出东西。
    """
    handler = KernelHandler()
    a = _queued(handler, 8)
    b = _queued(handler, 8)

    batch = handler.poll(lambda _request: None)

    def _owner(reply: Reply) -> _Sub:
        return next(sub for sub in handler._subs.values() if sub.request is reply.request)

    for reply in batch:  # 全投：A 全拒、B 全收
        handler.on_reply(
            reply.request,
            Delivery.CONGESTED if _owner(reply) is a else Delivery.SENT,
        )

    assert a.handed == 0
    assert a.out, "A 被拒的帧该留着下轮重发"
    assert b.handed == 0, "B 的帧交了却没划掉——它永远发不出下一帧了"
    assert not b.out, "B 交了 8 帧都收下了，队列该空"


def test_dropping_the_connection_drops_its_subscriptions():
    """连接没了，挂在上面的订阅一起注销：那条推送流再没有接收方。"""
    session = _session()
    session.ingest_stream(Stream.STDOUT, b"hi")
    handler = _watch(session)
    try:
        assert handler.poll(lambda _request: None)  # 没注销之前是有东西推的
        handler.on_disconnected(_CONNECTION)
        assert handler.poll(lambda _request: None) == []
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


class _FakeRunner:
    """只回答"排空了没"——测映射不需要真的起写线程。"""

    def __init__(self, drained: bool) -> None:
        self.input_drained = drained


class _FakeRuntime:
    """把 core 的输入判定换成脚本化的：`verdict=None` 表示会话已不在（抛 `CoreError`）。"""

    def __init__(self, verdict: InputVerdict | None, drained: bool = True) -> None:
        self._verdict = verdict
        self._drained = drained

    def send_input(self, uid: str, data: bytes) -> InputVerdict:
        if self._verdict is None:
            raise CoreError(f"会话没有驱动: {uid}")
        return self._verdict

    def runner(self, uid: str) -> _FakeRunner | None:
        return None if self._drained is None else _FakeRunner(self._drained)


@pytest.mark.parametrize(
    ("verdict", "action"),
    [
        (InputVerdict.QUEUED, InputAction.NONE),
        (InputVerdict.HOLD, InputAction.HOLD),
        (InputVerdict.REJECTED, InputAction.DROP),
    ],
)
def test_on_input_turns_the_core_verdict_into_a_connection_action(verdict, action):
    """core 出判定、处理层翻成对连接的动作——守护进程只照动作动手。"""
    handler = KernelHandler()
    handler._runtime = _FakeRuntime(verdict)  # type: ignore[assignment]
    assert handler.on_input("uid-1", b"x", None) is action


def test_on_input_to_a_dead_session_is_not_a_fault():
    """会话刚被关掉：无处可写不是对端的错，别断它的连接。"""
    handler = KernelHandler()
    handler._runtime = _FakeRuntime(None)  # type: ignore[assignment]
    assert handler.on_input("gone", b"x", None) is InputAction.NONE


def test_input_drained_reports_the_queue_state():
    handler = KernelHandler()
    handler._runtime = _FakeRuntime(InputVerdict.QUEUED, drained=False)  # type: ignore[assignment]
    assert handler.input_drained("uid-1") is False

    handler._runtime = _FakeRuntime(InputVerdict.QUEUED, drained=None)  # type: ignore[assignment]
    assert handler.input_drained("uid-1") is True, "会话已不在 = 没什么可等"


class _RenderingHost(FakeHost):
    """会出矢量与位图的假宿主——`FakeHost` 刻意不做屏幕渲染，这里只为把读视图那条路走通。"""

    def render_svg(self) -> str:
        return f'<svg width="80" height="24">{self.screen_text()}</svg>'

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        return f"{fmt}:{scale}:{self.screen_text()}".encode()


@pytest.mark.parametrize("mode", [ReadMode.SCREEN, ReadMode.TEXT, ReadMode.SVG])
def test_read_views_come_back_as_text_plus_the_offset(mode):
    """三个出文本的读视图都走同一张表：回文本，并带上日志末尾的 offset。"""
    registry = SessionRegistry(lambda spec: _RenderingHost(spec, FakeProgram()))
    session = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    session.ingest_stream(Stream.STDOUT, b"hello")
    handler = KernelHandler()
    handler._runtime.get = lambda _uid: session  # type: ignore[method-assign]
    wire = WireRequest(
        make_request(Command.READ_SESSION, op={"uid": session.uid, "mode": mode.value}),
        _CONNECTION,
    )
    try:
        reply = handler.handle(wire)
    finally:
        session.close()

    assert reply is not None
    payload = _data(reply.answer)
    assert "hello" in payload["text"]
    assert payload["offset"] == session.journal.end_offset


def test_read_image_comes_back_as_a_byte_frame():
    """`image` 走字节帧：位图由宿主直接出，缩放从请求里来。"""
    registry = SessionRegistry(lambda spec: _RenderingHost(spec, FakeProgram()))
    session = registry.create(SessionSpec(mode=PTY, argv=("x",)))
    session.ingest_stream(Stream.STDOUT, b"hello")
    handler = KernelHandler()
    handler._runtime.get = lambda _uid: session  # type: ignore[method-assign]
    wire = WireRequest(
        make_request(
            Command.READ_SESSION,
            op={"uid": session.uid, "mode": ReadMode.IMAGE.value, "scale": 0.5},
        ),
        _CONNECTION,
    )
    try:
        reply = handler.handle(wire)
    finally:
        session.close()

    assert reply is not None
    assert reply.answer.data == b"png:0.5:hello"  # type: ignore[attr-defined]

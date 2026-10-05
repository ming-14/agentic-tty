"""接入点：真管道、真连接，但不碰核心层——请求处理层是假的。

接入点只认 `protocol` 的帧与信封，不认识 `sid`、等待引擎、订阅，所以测它完全不需要
核心层与原生扩展。`pump()` / `send()` 都由测试线程驱动（生产里它们都在所有者线程上）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest

from agentic_tty.daemon.access_point import AccessPoint, ByteChunk, WireRequest, _Connection
from agentic_tty.daemon.handler import Delivery, Reply
from agentic_tty.protocol.contracts.daemon_ipc import STREAM_STDIN, STREAM_STDOUT
from agentic_tty.protocol.envelope import Envelope, from_json, make_request, to_json
from agentic_tty.protocol.frame import (
    BytesFrame,
    ControlFrame,
    FrameReader,
    encode_bytes,
    encode_control,
)
from agentic_tty.protocol.response import ok_response
from agentic_tty.transport.errors import ConnectionClosed
from agentic_tty.transport.pipe import PipeTransport, pipe_address
from agentic_tty.transport.stream import Connection, parse_address

_DEADLINE = 2.0


class _Seam:
    """假接缝：记下收到的请求与上行字节；答复由测试直接调 `point.send`。"""

    def __init__(self) -> None:
        self.requests: list[WireRequest] = []
        self.inputs: list[tuple[str, bytes]] = []

    def on_request(self, request: WireRequest) -> None:
        self.requests.append(request)

    def on_input(self, key: str, data: bytes) -> None:
        self.inputs.append((key, data))

    @property
    def envelopes(self) -> list[Envelope]:
        return [request.envelope for request in self.requests]


@pytest.fixture
def point(tmp_path) -> Iterator[tuple[AccessPoint, _Seam]]:
    seam = _Seam()
    address = pipe_address(f"agentic-tty-test-{uuid4().hex[:8]}", tmp_path / "run")
    access_point = AccessPoint(address, on_request=seam.on_request, on_input=seam.on_input)
    access_point.open()
    try:
        yield access_point, seam
    finally:
        access_point.close()


def _connect(access_point: AccessPoint) -> Connection:
    return PipeTransport().connect(parse_address(access_point.address), timeout=_DEADLINE)


def _pump_until(access_point: AccessPoint, predicate: Callable[[], bool]) -> bool:
    """驱动接入点，直到条件成立（或超时）。"""
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        access_point.pump(0.005)
        if predicate():
            return True
    return predicate()


def _read_frames(connection: Connection, count: int = 1) -> list[object]:
    reader = FrameReader(lambda: connection.recv(65536, timeout=0.05))
    frames: list[object] = []
    deadline = time.monotonic() + _DEADLINE
    while len(frames) < count and time.monotonic() < deadline:
        frames.extend(reader.read())
    return frames


def test_request_gets_its_answer_back(point: tuple[AccessPoint, _Seam]):
    access_point, seam = point
    connection = _connect(access_point)
    try:
        connection.send(encode_control(to_json(make_request("ping"))))
        assert _pump_until(access_point, lambda: len(seam.requests) == 1)
        request = seam.requests[0]
        answer = ok_response("ping", request.envelope.mid, {"pong": True})
        assert access_point.send(Reply(request=request, answer=answer)) is Delivery.SENT

        frames = _read_frames(connection)
        assert isinstance(frames[0], ControlFrame)
        assert from_json(frames[0].data).payload.output["data"] == {"pong": True}
    finally:
        connection.close()


def test_bytes_frame_reaches_on_input(point: tuple[AccessPoint, _Seam]):
    access_point, seam = point
    connection = _connect(access_point)
    try:
        connection.send(encode_bytes(STREAM_STDIN, "uid-1", b"hello"))
        assert _pump_until(access_point, lambda: seam.inputs == [("uid-1", b"hello")])
    finally:
        connection.close()


def test_upstream_frame_with_a_downstream_tag_is_rejected(point: tuple[AccessPoint, _Seam]):
    """上行只认"输入"标签——拿下行标签冒充上行是对方的错，不是静默丢弃。"""
    access_point, seam = point
    connection = _connect(access_point)
    try:
        assert _pump_until(access_point, lambda: access_point.connection_count == 1)
        connection.send(encode_bytes(STREAM_STDOUT, "uid-1", b"oops"))
        assert _pump_until(access_point, lambda: access_point.connection_count == 0)
        assert seam.inputs == []
    finally:
        connection.close()


def test_byte_answer_goes_back_as_a_bytes_frame(point: tuple[AccessPoint, _Seam]):
    access_point, seam = point
    connection = _connect(access_point)
    try:
        connection.send(encode_control(to_json(make_request("subscribe"))))
        assert _pump_until(access_point, lambda: len(seam.requests) == 1)
        request = seam.requests[0]
        chunk = ByteChunk(tag=STREAM_STDOUT, key=request.envelope.mid, data=b"\x00\x01raw")
        assert access_point.send(Reply(request=request, answer=chunk)) is Delivery.SENT

        frames = _read_frames(connection)
        assert frames == [
            BytesFrame(tag=STREAM_STDOUT, key=request.envelope.mid, data=b"\x00\x01raw")
        ]
    finally:
        connection.close()


def test_unknown_request_is_not_routed(point: tuple[AccessPoint, _Seam]):
    """不是本接入点发出的请求，`send` 要明确说"不是我的"。"""
    access_point, _seam = point
    assert (
        access_point.send(Reply(request=object(), answer=ok_response("x", "m")))
        is Delivery.NOT_MINE
    )


def test_two_connections_do_not_mix(point: tuple[AccessPoint, _Seam]):
    access_point, seam = point
    first = _connect(access_point)
    try:
        # Windows 命名管道一次只备一个实例：收下第一条才会腾出下一个给第二个客户端。
        assert _pump_until(access_point, lambda: access_point.connection_count == 1)
        second = _connect(access_point)
        try:
            assert _pump_until(access_point, lambda: access_point.connection_count == 2)
            first.send(encode_control(to_json(make_request("a"))))
            second.send(encode_control(to_json(make_request("b"))))
            assert _pump_until(access_point, lambda: len(seam.requests) == 2)
            by_type = {request.envelope.type: request for request in seam.requests}
            for name, connection in (("a", first), ("b", second)):
                request = by_type[name]
                answer = ok_response(name, request.envelope.mid, {"who": name})
                assert access_point.send(Reply(request=request, answer=answer)) is Delivery.SENT
                frames = _read_frames(connection)
                assert from_json(frames[0].data).payload.output["data"] == {"who": name}
        finally:
            second.close()
    finally:
        first.close()


def test_malformed_envelope_drops_the_connection(point: tuple[AccessPoint, _Seam]):
    """信封解不开 = 这条流作废（与 `FrameReader` 的"错位即作废"同一规矩）。"""
    access_point, seam = point
    connection = _connect(access_point)
    try:
        assert _pump_until(access_point, lambda: access_point.connection_count == 1)
        connection.send(encode_control(b"not json"))
        assert _pump_until(access_point, lambda: access_point.connection_count == 0)
        assert seam.requests == []
    finally:
        connection.close()


class _DeadConnection:
    """底层连接：一读就报对端关闭，并记录 `close()` 是否真的被调到。"""

    def __init__(self) -> None:
        self.closed = False

    @property
    def peer(self) -> str:
        return "fake"

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        raise ConnectionClosed("对端断了")

    def send(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_a_broken_connection_closes_its_underlying_handle():
    """读出错时必须真的关掉底层句柄——只置 `closed` 标志会让 `close()` 变空转，句柄泄漏。"""
    underlying = _DeadConnection()
    connection = _Connection(underlying, outbox_bytes=1024)
    assert connection.read() == []
    assert connection.closed is True
    assert underlying.closed is True


class _BlockedConnection:
    """底层连接：写一直阻塞（对端不读），用来把出站队列顶满。"""

    def __init__(self) -> None:
        self.gate = threading.Event()

    @property
    def peer(self) -> str:
        return "blocked"

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return b""

    def send(self, data: bytes) -> None:
        self.gate.wait()

    def close(self) -> None:
        self.gate.set()


def test_a_full_outbox_is_refused_not_blocked():
    """队列满了要**明确拒收**——既不阻塞调用方，也不丢字节。

    字节账要把**正在写的那一帧**算进去，否则写线程一取走队列就空了，上限形同虚设。
    """
    underlying = _BlockedConnection()
    connection = _Connection(underlying, outbox_bytes=8)
    try:
        assert connection.enqueue(b"1234") is True  # 队空：永远收下
        assert connection.enqueue(b"5678") is True  # 4 + 4 = 8，刚好
        assert connection.enqueue(b"9") is False  # 越界，拒收
    finally:
        connection.close()
        connection.join_writer(2.0)


def test_room_reports_what_the_outbox_can_still_take():
    """`room` 是处理层一轮交付的预算：队空时给满，占用后按余量递减，堵死为 0。"""
    underlying = _BlockedConnection()
    connection = _Connection(underlying, outbox_bytes=8)
    try:
        assert connection.room == 8  # 队空 = 至少能收一整条
        connection.enqueue(b"1234")
        assert connection.room == 4
        connection.enqueue(b"5678")
        assert connection.room == 0
    finally:
        connection.close()
        connection.join_writer(2.0)


def test_room_for_a_foreign_request_is_unknown(point: tuple[AccessPoint, _Seam]):
    """不是本接入点发出的请求问不出额度——返回 None，调用方按"不限额"处理。"""
    access_point, _seam = point
    assert access_point.room_for(object()) is None

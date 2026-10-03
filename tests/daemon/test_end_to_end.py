"""端到端：守护进程 ＋ 接入点 ＋ 协议客户端，走真管道跑通请求/答复。

请求处理层是假的（不认识 core），但链路上的每一段都是真的：`pipe://` 监听、accept、
增量解帧、信封解码、答复按请求身份写回、字节帧。
"""

from __future__ import annotations

import threading
import time
from typing import cast
from uuid import uuid4

from agentic_tty.config import DaemonConfig, endpoint_name
from agentic_tty.daemon.access_point import WireRequest
from agentic_tty.daemon.handler import Reply
from agentic_tty.daemon.server import Daemon
from agentic_tty.protocol.contracts.daemon_ipc import STREAM_STDOUT
from agentic_tty.protocol.envelope import from_json, make_request, to_json
from agentic_tty.protocol.frame import ControlFrame, FrameReader, encode_bytes, encode_control
from agentic_tty.protocol.response import data_of, error_of, failed_response, ok_response
from agentic_tty.transport.pipe import PipeTransport, pipe_address
from agentic_tty.transport.stream import Connection, parse_address

_DEADLINE = 5.0


class _EchoHandler:
    """最小请求处理层：认下请求、回一条答复；收到上行字节就记下来。

    它只认 `protocol` 的信封——daemon 对报文不透明，接缝上跑的就是它。
    """

    def __init__(self) -> None:
        self.inputs: list[tuple[str, bytes]] = []

    def handle(self, request: object) -> Reply | None:
        wire = cast(WireRequest, request)
        envelope = wire.envelope
        if envelope.type == "fail":
            answer = failed_response("fail", envelope.mid, "Boom", "故意炸")
        else:
            answer = ok_response(envelope.type, envelope.mid, {"echo": True})
        return Reply(request=wire, answer=answer)

    def poll(self) -> list[Reply]:
        return []

    def pending(self) -> int:
        return 0

    def failure(self, request: object, error: BaseException) -> Reply:
        wire = cast(WireRequest, request)
        return Reply(
            request=wire,
            answer=failed_response("x", wire.envelope.mid, type(error).__name__, str(error)),
        )

    def on_input(self, key: str, data: bytes) -> None:
        self.inputs.append((key, data))

    def pump(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _Running:
    """起一个真守护进程（挂接入点），退出时收尾。"""

    def __init__(self, tmp_path) -> None:
        self.name = f"e2e-{uuid4().hex[:8]}"
        self.runtime_dir = tmp_path / "run"
        self.handler = _EchoHandler()
        self.daemon = Daemon(
            DaemonConfig(
                name=self.name,
                runtime_dir=self.runtime_dir,
                write_log_file=False,
                listen=self.name,
                tick_interval=0.001,
            ),
            lambda: self.handler,
            check_dependencies=lambda: None,
        )
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Running:
        self.daemon.start()
        self._thread = threading.Thread(target=self.daemon.run, name="daemon-run", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.daemon.request_stop()
        if self._thread is not None:
            self._thread.join(_DEADLINE)
        self.daemon.stop(2)

    def connect(self) -> Connection:
        address = pipe_address(endpoint_name(self.name), self.runtime_dir)
        return PipeTransport().connect(parse_address(address), timeout=_DEADLINE)

    def wait_inputs(self) -> list[tuple[str, bytes]]:
        deadline = time.monotonic() + _DEADLINE
        while not self.handler.inputs and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.handler.inputs


def _read_frames(connection: Connection, count: int = 1, budget: float = _DEADLINE) -> list[object]:
    reader = FrameReader(lambda: connection.recv(65536, timeout=0.05))
    frames: list[object] = []
    deadline = time.monotonic() + budget
    while len(frames) < count and time.monotonic() < deadline:
        frames.extend(reader.read())
    return frames


def test_request_answer_roundtrip_over_a_real_pipe(tmp_path):
    with _Running(tmp_path) as running:
        connection = running.connect()
        try:
            request = make_request("ping", op={"n": 1})
            connection.send(encode_control(to_json(request)))
            frames = _read_frames(connection)
            assert isinstance(frames[0], ControlFrame)
            answer = from_json(frames[0].data)
            assert answer.mid == request.mid
            assert data_of(answer) == {"echo": True}
        finally:
            connection.close()


def test_input_bytes_reach_the_handler_and_produce_no_answer(tmp_path):
    with _Running(tmp_path) as running:
        connection = running.connect()
        try:
            connection.send(encode_bytes(STREAM_STDOUT, "uid-1", b"typed\n"))
            assert running.wait_inputs() == [("uid-1", b"typed\n")]
            assert _read_frames(connection, budget=0.2) == []  # 上行字节是单向的
        finally:
            connection.close()


def test_handler_failure_comes_back_as_a_failed_answer(tmp_path):
    with _Running(tmp_path) as running:
        connection = running.connect()
        try:
            request = make_request("fail")
            connection.send(encode_control(to_json(request)))
            frames = _read_frames(connection)
            assert isinstance(frames[0], ControlFrame)
            answer = from_json(frames[0].data)
            assert answer.mid == request.mid
            failure = error_of(answer)
            assert failure is not None
            assert failure.code == "Boom"
        finally:
            connection.close()

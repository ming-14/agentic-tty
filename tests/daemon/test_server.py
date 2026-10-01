"""守护进程端到端：真 loopback、真线程，但请求处理层是假的。

假处理层是刻意的——守护进程的机制只认 `RequestHandler`，它本来就不该知道 `sid`、
等待引擎这些东西，所以测它完全不需要核心层与原生扩展。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from uuid import uuid4

import pytest

from agentic_tty.daemon.config import DaemonConfig
from agentic_tty.daemon.errors import AlreadyRunning, DaemonError, NotStarted
from agentic_tty.daemon.handler import Reply, RequestHandler
from agentic_tty.daemon.server import Daemon
from agentic_tty.protocol.envelope import Envelope, make_request, make_response
from agentic_tty.protocol.frame import BytesFrame, ControlFrame
from agentic_tty.protocol.messages import ok_response
from agentic_tty.transport import registry as transports
from agentic_tty.transport.channel import Channel, decode_control
from agentic_tty.transport.errors import ConnectionClosed, TransportError

_DEADLINE = 5.0


class FakeHandler:
    """最小请求处理层。"""

    def __init__(self) -> None:
        self.inputs: list[tuple[str, bytes]] = []
        self.pumps = 0
        self.shutdown_called = False
        self.shutdown_block: float | None = None
        self.deferred: list[Envelope] = []
        self.binary_on: set[str] = set()
        self.pump_boom = False
        self.poll_boom = False

    def handle(self, envelope: Envelope) -> Reply | None:
        if envelope.type == "defer":
            self.deferred.append(envelope)
            return None
        if envelope.type == "boom":
            raise RuntimeError("故意炸")
        if envelope.type in self.binary_on:
            return Reply(
                ok_response(envelope.type, envelope.mid, {"bytes": 3}),
                stream="stdout",
                binary=b"\x00\x01\x02",
            )
        return Reply(ok_response(envelope.type, envelope.mid, {"type": envelope.type}))

    def poll(self) -> list[Reply]:
        if self.poll_boom:
            raise RuntimeError("poll 故意炸")
        replies = [
            Reply(ok_response(item.type, item.mid, {"deferred": True}), request=item)
            for item in self.deferred
        ]
        self.deferred.clear()
        return replies

    def on_input(self, key: str, data: bytes) -> None:
        self.inputs.append((key, data))

    def pump(self) -> None:
        self.pumps += 1
        if self.pump_boom:
            raise RuntimeError("pump 故意炸")

    def shutdown(self) -> None:
        if self.shutdown_block is not None:
            time.sleep(self.shutdown_block)
        self.shutdown_called = True


class Client:
    """一个最薄的客户端：连上去，发请求，等响应。"""

    def __init__(self, uri: str) -> None:
        self.connection = transports.connect(uri, timeout=_DEADLINE)
        self.channel = Channel(self.connection)
        self.binary: list[BytesFrame] = []

    def send(self, envelope: Envelope) -> None:
        self.channel.send(envelope)

    def pump_frames(self, timeout: float = 0.05) -> list[Envelope]:
        """读一轮并归类：控制帧解成信封，字节帧收进 `binary`。"""
        replies: list[Envelope] = []
        for frame in self.channel.recv(timeout=timeout):
            if isinstance(frame, BytesFrame):
                self.binary.append(frame)
            elif isinstance(frame, ControlFrame):
                replies.append(decode_control(frame))
        return replies

    def request(self, type_: str, **op: object) -> Envelope:
        envelope = make_request(type_, op=op)
        self.send(envelope)
        return self.await_reply(envelope.mid)

    def await_reply(self, mid: str, timeout: float = _DEADLINE) -> Envelope:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for envelope in self.pump_frames():
                if envelope.mid == mid:
                    return envelope
        raise AssertionError(f"没等到 mid={mid} 的响应")

    def wait_for_binary(self, timeout: float = _DEADLINE) -> BytesFrame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.binary:
                return self.binary[0]
            self.pump_frames()
        raise AssertionError("没等到字节帧")

    def close(self) -> None:
        self.channel.close()


@contextlib.contextmanager
def serve(
    tmp_path,
    handler: FakeHandler | None = None,
    *,
    check: Callable[[], None] | None = None,
    **overrides: object,
) -> Iterator[tuple[Daemon, FakeHandler]]:
    handler = handler or FakeHandler()
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
        tick_interval=0.001,
        **overrides,
    )
    daemon = Daemon(config, lambda: handler, check_dependencies=check or (lambda: None))
    daemon.start()
    thread = threading.Thread(target=daemon.run, name="daemon-run", daemon=True)
    thread.start()
    try:
        yield daemon, handler
    finally:
        daemon.request_stop()
        thread.join(_DEADLINE)
        daemon.stop(2)


def test_fake_handler_satisfies_the_seam():
    assert isinstance(FakeHandler(), RequestHandler)


def test_start_reports_the_real_port(tmp_path):
    with serve(tmp_path) as (daemon, _):
        host, port = daemon.address.removeprefix("tcp://").split(":")
        assert host == "127.0.0.1"
        assert int(port) > 0


def test_startup_writes_pid_and_endpoint(tmp_path):
    with serve(tmp_path) as (daemon, _):
        assert daemon.pid_path.read_text(encoding="utf-8").isdigit()
        assert daemon.endpoint_path.read_text(encoding="utf-8") == daemon.address


def test_second_daemon_on_the_same_lock_is_rejected(tmp_path):
    """单实例是硬要求：两个守护进程会各自维护互斥的会话坐标，监听端口也会冲突。

    用同一份配置（同 runtime_dir、同 name）——Windows 上锁名是全局命名空间、Linux 上
    是 runtime_dir 里的锁文件，两边都靠"同一份配置"才能都撞上。
    """
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
    )
    first = Daemon(config, FakeHandler, check_dependencies=lambda: None)
    first.start()
    try:
        other = Daemon(config, FakeHandler, check_dependencies=lambda: None)
        with pytest.raises(AlreadyRunning):
            other.start()
        assert not other.running
    finally:
        first.stop()


def test_start_failure_rolls_back_the_lock(tmp_path):
    """依赖检查失败要能把已取的锁放掉，否则之后永远起不来。"""
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
    )

    def boom() -> None:
        raise RuntimeError("缺原生扩展")

    broken = Daemon(config, FakeHandler, check_dependencies=boom)
    with pytest.raises(RuntimeError):
        broken.start()
    assert not broken.pid_path.exists()

    healthy = Daemon(config, FakeHandler, check_dependencies=lambda: None)
    healthy.start()
    try:
        assert healthy.running
    finally:
        healthy.stop()


def test_run_without_start_is_rejected(tmp_path):
    daemon = Daemon(
        DaemonConfig(runtime_dir=tmp_path / "run", write_log_file=False),
        FakeHandler,
        check_dependencies=lambda: None,
    )
    with pytest.raises(NotStarted):
        daemon.run()


def test_starting_twice_is_rejected(tmp_path):
    with serve(tmp_path) as (daemon, _):
        with pytest.raises(DaemonError, match="已启动"):
            daemon.start()


def test_request_gets_a_reply(tmp_path):
    with serve(tmp_path) as (daemon, _):
        client = Client(daemon.address)
        try:
            response = client.request("ping")
            assert response.mid
            assert response.payload.output["ok"] is True
            assert response.payload.output["data"] == {"type": "ping"}
        finally:
            client.close()


def test_deferred_reply_goes_back_to_the_right_client(tmp_path):
    """处理层返回 None 表示登记等待；稍后 poll 出的响应必须回到发起它的那条连接。"""
    with serve(tmp_path) as (daemon, _):
        first, second = Client(daemon.address), Client(daemon.address)
        try:
            deferred = make_request("defer")
            first.send(deferred)
            assert second.request("ping").payload.output["data"] == {"type": "ping"}
            reply = first.await_reply(deferred.mid)
            assert reply.payload.output["data"] == {"deferred": True}
        finally:
            first.close()
            second.close()


def test_same_mid_on_two_connections_routes_by_request(tmp_path):
    """`mid` 只在一条连接内有意义：两条连接撞上同一个 mid，也不能互相串响应。"""
    with serve(tmp_path) as (daemon, _):
        first, second = Client(daemon.address), Client(daemon.address)
        try:
            first.send(replace(make_request("defer"), mid="same-mid"))
            second.send(replace(make_request("defer"), mid="same-mid"))
            assert first.await_reply("same-mid").payload.output["data"] == {"deferred": True}
            assert second.await_reply("same-mid").payload.output["data"] == {"deferred": True}
        finally:
            first.close()
            second.close()


def test_binary_reply_arrives_as_a_byte_frame(tmp_path):
    handler = FakeHandler()
    handler.binary_on.add("raw")
    with serve(tmp_path, handler) as (daemon, _):
        client = Client(daemon.address)
        try:
            response = client.request("raw")
            assert response.payload.output["data"] == {"bytes": 3}
            frame = client.wait_for_binary()
            assert frame.data == b"\x00\x01\x02"
            assert frame.key == response.mid  # 字节帧靠 mid 挂回它那条请求
        finally:
            client.close()


def test_input_byte_frames_reach_the_handler(tmp_path):
    with serve(tmp_path) as (daemon, handler):
        client = Client(daemon.address)
        try:
            client.channel.send_bytes("stdout", "sid-1", b"hello\n")
            deadline = time.monotonic() + _DEADLINE
            while not handler.inputs and time.monotonic() < deadline:
                time.sleep(0.01)
            assert handler.inputs == [("sid-1", b"hello\n")]
        finally:
            client.close()


def test_handler_crash_becomes_an_error_reply(tmp_path):
    with serve(tmp_path) as (daemon, _):
        client = Client(daemon.address)
        try:
            response = client.request("boom")
            assert response.payload.output["ok"] is False
            assert response.payload.output["error"]["code"] == "InternalError"
        finally:
            client.close()


def test_handler_pump_crash_does_not_kill_the_loop(tmp_path):
    """一次推进异常不能杀死所有者循环：守护进程还得能继续应答。"""
    handler = FakeHandler()
    with serve(tmp_path, handler) as (daemon, _):
        handler.pump_boom = True
        client = Client(daemon.address)
        try:
            assert client.request("ping").payload.output["ok"] is True
        finally:
            client.close()


def test_handler_poll_crash_does_not_kill_the_loop(tmp_path):
    handler = FakeHandler()
    with serve(tmp_path, handler) as (daemon, _):
        handler.poll_boom = True
        client = Client(daemon.address)
        try:
            assert client.request("ping").payload.output["ok"] is True
        finally:
            client.close()


def test_garbage_bytes_drop_the_connection_but_not_the_daemon(tmp_path):
    with serve(tmp_path) as (daemon, _):
        broken = transports.connect(daemon.address, timeout=_DEADLINE)
        try:
            # 声称 17MiB 的负载——超过单帧上限，长度字段是对端给的，不能照单全收。
            broken.send(b"\x01" + (17 << 20).to_bytes(4, "big"))
            deadline = time.monotonic() + _DEADLINE
            while time.monotonic() < deadline:
                try:
                    broken.recv(timeout=0.05)
                except ConnectionClosed:
                    break
            else:
                raise AssertionError("坏帧没有被断开")
        finally:
            broken.close()

        client = Client(daemon.address)
        try:
            assert client.request("ping").payload.output["ok"] is True
        finally:
            client.close()


def test_response_direction_is_rejected(tmp_path):
    with serve(tmp_path) as (daemon, _):
        client = Client(daemon.address)
        try:
            client.send(make_response("ping", "m1", output={"ok": True}))
            deadline = time.monotonic() + _DEADLINE
            while time.monotonic() < deadline:
                try:
                    client.channel.recv(timeout=0.05)
                except ConnectionClosed:
                    break
            else:
                raise AssertionError("响应方向的信封没有被断开")
        finally:
            client.close()


def test_client_disconnect_does_not_stop_the_daemon(tmp_path):
    with serve(tmp_path) as (daemon, _):
        leaving = Client(daemon.address)
        leaving.request("ping")
        leaving.close()

        keep = Client(daemon.address)
        try:
            assert keep.request("ping").payload.output["ok"] is True
            assert daemon.running
        finally:
            keep.close()


def test_run_pumps_the_handler(tmp_path):
    with serve(tmp_path) as (_, handler):
        deadline = time.monotonic() + _DEADLINE
        while handler.pumps < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handler.pumps >= 3


def test_stop_shuts_the_handler_down_and_removes_rendezvous(tmp_path):
    with serve(tmp_path) as (daemon, handler):
        address, pid_path = daemon.address, daemon.pid_path
        assert pid_path.exists()
    assert handler.shutdown_called
    assert not pid_path.exists()
    assert not daemon.endpoint_path.exists()
    assert not daemon.running
    with pytest.raises(TransportError):
        transports.connect(address, timeout=1.0)


def test_stop_reports_timeout_when_shutdown_hangs(tmp_path):
    """会话收尾可能长时间阻塞；stop 必须带超时返回，把强制退出留给入口。"""
    handler = FakeHandler()
    handler.shutdown_block = 5.0
    with serve(tmp_path, handler) as (daemon, _):
        started = time.monotonic()
        finished = daemon.stop(0.5)
        assert finished is False
        assert time.monotonic() - started < 3.0
        assert daemon.running is False


def test_stop_is_idempotent(tmp_path):
    with serve(tmp_path) as (daemon, _):
        assert daemon.stop(1) is True
        assert daemon.stop(1) is True

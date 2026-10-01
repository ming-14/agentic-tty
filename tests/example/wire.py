"""示例层测试的共享接线：一个走网线的最薄客户端，和一个真守护进程。

**不叫 `test_*`**：它不是测试用例，是被两个测试模块共用的样板（守护进程的启动/收尾写
两遍就迟早会不一致）。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from uuid import uuid4

from agentic_tty.daemon.config import DaemonConfig
from agentic_tty.daemon.server import Daemon
from agentic_tty.example.service import ExampleService
from agentic_tty.protocol.envelope import Envelope, make_request
from agentic_tty.protocol.frame import BytesFrame, ControlFrame
from agentic_tty.protocol.messages import data_of
from agentic_tty.transport import registry as transports
from agentic_tty.transport.channel import Channel, decode_control

# e2e 的等待上限：真 PTY + 真守护进程线程，位图渲染这类调用在机器有负载时
# 可以远慢于正常值。这只是上限，正常路径是毫秒级。
DEADLINE = 20.0


def has_native_host() -> bool:
    """本机能不能起真实 PTY（依赖 vendor 里的 pywezterm）。"""
    try:
        from agentic_tty.runtime.pty_host import require_pywezterm

        require_pywezterm()
    except Exception:
        return False
    return True


class WireClient:
    """只用 `protocol + transport` 的客户端——和管理台那种进程内直连正相反。"""

    def __init__(self, uri: str) -> None:
        self.channel = Channel(transports.connect(uri, timeout=DEADLINE))
        self.binary: list[BytesFrame] = []

    def request(self, type_: str, timeout: float = DEADLINE, **op: object) -> Envelope:
        request = make_request(type_, op=op)
        self.channel.send(request)
        return self.await_reply(request.mid, timeout)

    def request_with(self, type_: str, condition: dict, **op: object) -> Envelope:
        """带返回条件的请求：等待时长按条件里的 timeout 放宽，别让测试先超时。"""
        request = make_request(type_, op=op, condition=condition)
        self.channel.send(request)
        return self.await_reply(
            request.mid, timeout=float(condition.get("timeout", DEADLINE)) + 5.0
        )

    def pump(self, timeout: float = 0.05) -> list[Envelope]:
        """读一轮并归类：控制帧解成信封，字节帧收进 `binary`。"""
        replies: list[Envelope] = []
        for frame in self.channel.recv(timeout=timeout):
            if isinstance(frame, BytesFrame):
                self.binary.append(frame)
            elif isinstance(frame, ControlFrame):
                replies.append(decode_control(frame))
        return replies

    def await_reply(self, mid: str, timeout: float = DEADLINE) -> Envelope:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for envelope in self.pump():
                if envelope.mid == mid:
                    return envelope
        raise AssertionError(f"没等到 mid={mid} 的响应")

    def wait_binary(self, timeout: float = DEADLINE) -> BytesFrame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.binary:
                return self.binary.pop(0)
            self.pump()
        raise AssertionError("没等到字节帧")

    def screen_until(self, sid: str, needle: str, timeout: float = DEADLINE) -> str:
        """反复读屏直到出现目标文本——客户端就是这么轮询的（订阅留到以后）。"""
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = data_of(self.request("read_terminal", sid=sid, mode="screen"))["text"]
            if needle in text:
                return text
        raise AssertionError(f"屏幕上一直没出现 {needle!r}，最后读到: {text!r}")

    def close(self) -> None:
        self.channel.close()


def _make_daemon(tmp_path) -> Daemon:
    # 处理层**建得比守护进程早**（它要被注入进去），可状态查询要报监听地址——端口写 0 时
    # 地址只有绑完监听才知道，所以这个 lambda 里引用的 `daemon` 是晚绑定的。
    service = ExampleService(listen=lambda: daemon.address)
    config = DaemonConfig(
        name=f"agentic-tty-demo-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
        tick_interval=0.001,
    )
    daemon = Daemon(config, lambda: service, check_dependencies=lambda: None)
    return daemon


@contextlib.contextmanager
def serve(tmp_path) -> Iterator[Daemon]:
    """起一个跑在后台线程里的真守护进程。"""
    daemon = _make_daemon(tmp_path)
    daemon.start()
    thread = threading.Thread(target=daemon.run, name="demo-daemon", daemon=True)
    thread.start()
    try:
        yield daemon
    finally:
        daemon.request_stop()
        thread.join(DEADLINE)
        daemon.stop(2)

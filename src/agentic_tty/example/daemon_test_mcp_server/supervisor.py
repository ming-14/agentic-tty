"""起一个守护进程、连上它、退出时把它收掉。

实例名随机（`mcp-<8 位>`），管道名 / 锁名 / 运行时目录都随它派生，所以同时开几份互不
相撞。守护进程是本进程的**子进程**：收尾时先请它自己收（会话与监听归它管），超时再硬杀，
最后把随机的运行时目录清掉。

守护进程的 **stdout / stdin 都得从 stdio 传输里摘出来**：stdout 是 MCP 的协议通道，
守护进程往那儿写一个字节，协议流就废了；stdin 是 host 的管道，子进程不该继承。它自己的
日志走 stderr，不受影响。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ...config import runtime_dir
from ...foundation.logs import get_logger
from ...protocol.contracts.daemon_ipc import Command
from ...protocol.envelope import Envelope
from ...protocol.frame import BytesFrame
from . import address
from .client import DaemonClient

_logger = get_logger("example.daemon_test_mcp_server.supervisor")

_READY_TIMEOUT = 20.0
"""等守护进程挂上监听的预算。"""
_SHUTDOWN_TIMEOUT = 5.0
"""请它自己收尾后，等它退出的预算。"""


class Supervisor:
    """一个守护进程的完整生命周期：起、连、收。"""

    def __init__(self) -> None:
        self._instance = f"mcp-{uuid4().hex[:8]}"
        self._address = address(self._instance)
        self._client = DaemonClient(self._address)
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def instance(self) -> str:
        return self._instance

    def call(self, command: str, op: Mapping[str, Any] | None = None) -> Envelope | BytesFrame:
        """转发一条请求；守护进程还没起就先把它起起来。"""
        self._ensure()
        return self._client.call(command, op)

    def write(self, uid: str, data: bytes) -> None:
        """转发一帧上行字节。"""
        self._ensure()
        self._client.write(uid, data)

    def close(self) -> None:
        """收尾：请守护进程自己收 → 超时硬杀 → 清运行时目录。"""
        process, self._process = self._process, None
        if process is None:
            return
        try:
            self._client.call(Command.SHUTDOWN_DAEMON)
        except Exception as exc:  # 连接可能已经没了；收尾不能因此中断
            _logger.info("请守护进程收尾没成功: %s", exc)
        self._client.close()
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            _logger.warning("守护进程收尾超时，硬杀 pid=%s", process.pid)
            process.kill()
            process.wait(timeout=_SHUTDOWN_TIMEOUT)
        shutil.rmtree(runtime_dir(self._instance), ignore_errors=True)

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _ensure(self) -> None:
        if self._process is None:
            self._start()

    def _start(self) -> None:
        _logger.info("起守护进程 实例=%s 地址=%s", self._instance, self._address)
        process = subprocess.Popen(
            [sys.executable, "-m", "agentic_tty.daemon", "--name", self._instance],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        self._process = process
        deadline = time.monotonic() + _READY_TIMEOUT
        while not self._client.try_connect():
            if process.poll() is not None:
                raise RuntimeError(f"守护进程起不来（退出码 {process.returncode}）")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等守护进程就绪超时: {self._address}")
            time.sleep(0.05)
        _logger.info("守护进程已就绪 pid=%s", process.pid)

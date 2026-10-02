"""每路输出一个读线程：读宿主 → 推入有界桥。

线程只做读，**不碰终端模型**（模型由所有者线程独占）。

读用**带超时**的方式而不是纯阻塞：PTY 在子进程退出后并不会返回 EOF，纯阻塞读
会永久挂住一个线程。因此收工条件有两条：宿主已释放（会话已关闭）直接收工；
或宿主自己说这一路已排空（`poll_eof()`）——本线程投一个 `eof=True` 的块，
由所有者线程记账（`mark_eof`）。

排空由宿主判定而不是本线程推测：退出与"最后一段输出到达"之间差多少，只有宿主
知道自己的 IO 形态（PTY 经 conhost 中继、没有真 EOF；子进程管道读空即真 EOF）。
本线程只负责搬与问。
"""

from __future__ import annotations

import threading
import time

from ..core.ports import Stream
from ..core.session.base import Session
from ..foundation.logs import get_logger
from .bridge import Chunk, ThreadBridge

_logger = get_logger("runtime.reader")

_EMPTY_READ_PAUSE = 0.005


class StreamReader:
    """读一路输出。"""

    def __init__(
        self,
        session: Session,
        bridge: ThreadBridge,
        stream: Stream,
        *,
        max_bytes: int = 65536,
        read_timeout: float = 0.2,
    ) -> None:
        self._session = session
        self._bridge = bridge
        self._stream = stream
        self._max_bytes = max_bytes
        self._read_timeout = read_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"reader-{self._session.uid[:8]}-{self._stream}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        uid = self._session.uid
        while not self._stop.is_set():
            host = self._session.host
            if host is None:
                # 宿主已释放（会话已关闭）：收工。
                # （这里只**读** `exit_code` / `host` 两个不可变引用，不改会话状态；
                #   状态改动一律经 EOF 块回到所有者线程做。）
                return
            try:
                chunk = self._session.read_stream(self._stream, self._read_timeout, self._max_bytes)
            except Exception as exc:  # 宿主已关闭/被杀：正常终止路径
                if not self._stop.is_set():
                    _logger.debug("读线程退出 uid=%s stream=%s: %s", uid, self._stream, exc)
                return
            if chunk:
                if not self._bridge.put(
                    Chunk(uid=uid, stream=self._stream, data=chunk),
                    stop_check=self._stop.is_set,
                ):
                    return
                continue
            if host.poll_eof(self._stream):
                self._bridge.put(
                    Chunk(uid=uid, stream=self._stream, data=b"", eof=True),
                    stop_check=self._stop.is_set,
                )
                return
            time.sleep(_EMPTY_READ_PAUSE)

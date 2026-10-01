"""每路输出一个读线程：读宿主 → 推入有界桥。

线程只做读，**不碰终端模型**（模型由所有者线程独占）。

读用**带超时**的方式而不是纯阻塞：PTY 在子进程退出后并不会返回 EOF，纯阻塞读
会永久挂住一个线程。因此收工条件是"**进程已退出（或宿主已关）且本轮读空**"——
退出后输出是有限的，一次空读即表示已排空；排空后投一个 `eof=True` 的块，
由所有者线程记账（`mark_eof`）。
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
            if self._session.exit_code is not None:
                # 进程已退出且本轮读空 → 该路已排空。
                # （这里只**读** `exit_code` / `host` 两个不可变引用，不改会话状态；
                #   状态改动一律经 EOF 块回到所有者线程做。）
                self._bridge.put(
                    Chunk(uid=uid, stream=self._stream, data=b"", eof=True),
                    stop_check=self._stop.is_set,
                )
                return
            if self._session.host is None:
                return
            time.sleep(_EMPTY_READ_PAUSE)

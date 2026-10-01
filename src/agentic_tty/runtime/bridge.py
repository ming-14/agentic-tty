"""读线程 → 所有者线程的有界桥。

队列满时 `put` 阻塞读线程 → 停止从宿主读 → 管道填满 → 子进程写阻塞。
这就是背压真正落到宿主的那一步。

`put` 带短超时重试，因此停止时不会被永久卡住。
"""

from __future__ import annotations

import queue
from dataclasses import dataclass

from ..core.ports import Stream


@dataclass(frozen=True, slots=True)
class Chunk:
    """一段来自某会话某一路的输出；`eof=True` 表示该路已排空。"""

    uid: str
    stream: Stream
    data: bytes
    eof: bool = False


class ThreadBridge:
    """有界队列。"""

    def __init__(self, maxsize: int = 256) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正")
        self._queue: queue.Queue[Chunk] = queue.Queue(maxsize)

    def put(self, chunk: Chunk, *, stop_check, retry_interval: float = 0.1) -> bool:
        """阻塞式投递；`stop_check()` 为真时放弃并返回 False。"""
        while not stop_check():
            try:
                self._queue.put(chunk, timeout=retry_interval)
                return True
            except queue.Full:
                continue
        return False

    def drain(self, limit: int = 128) -> list[Chunk]:
        """取走至多 `limit` 个块（非阻塞）。"""
        out: list[Chunk] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def pending(self) -> int:
        return self._queue.qsize()

"""读线程 → 所有者线程的有界桥。

队列满时 `put` 阻塞读线程 → 停止从宿主读 → 管道填满 → 子进程写阻塞。
这就是背压真正落到宿主的那一步。

`put` 带短超时重试，因此停止时不会被永久卡住。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from ..ports import Stream


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


class Wakeup:
    """跨会话的唤醒通道：读线程有数据时投一个信号，驱动方阻塞等它。

    只传"哪个会话有活"，不传数据——数据在各自的桥里，因此**背压仍按会话算**。
    信号是**提示**不是账本：漏掉或重复都不影响正确性（驱动方醒来后照样按游标补齐）。

    **一个 uid 只留一份**（重复投只算一次）：积压的上界是**会话数**，与输出快慢无关。
    驱动方因此拿得住 uid——取出来就能**照它去推进那一个会话**，不必清空扔掉。
    """

    def __init__(self) -> None:
        # 有序：先投的先被取走。用 dict 当集合是为了**既去重又保序**——裸 set 保不了序。
        self._awake: dict[str, None] = {}
        self._cv = threading.Condition()

    def signal(self, uid: str) -> None:
        """读线程调用：通知"这个会话有新数据"。已经在里面的 uid 不重复入。"""
        with self._cv:
            self._awake[uid] = None
            self._cv.notify()

    def wait(self, timeout: float | None = None) -> str | None:
        """驱动方调用：阻塞等到有活（返回会话 uid）；超时返回 None。"""
        with self._cv:
            if not self._awake:
                self._cv.wait(timeout)
            if not self._awake:
                return None
            uid = next(iter(self._awake))  # 先投的先取
            del self._awake[uid]
            return uid

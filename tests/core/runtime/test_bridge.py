from __future__ import annotations

import threading
import time

from agentic_tty.core.ports import Stream
from agentic_tty.core.runtime.bridge import Chunk, ThreadBridge


def test_put_and_drain():
    bridge = ThreadBridge(maxsize=8)
    bridge.put(Chunk("u1", Stream.STDOUT, b"a"), stop_check=lambda: False)
    bridge.put(Chunk("u1", Stream.STDERR, b"b"), stop_check=lambda: False)
    drained = bridge.drain()
    assert [(c.stream, c.data) for c in drained] == [
        (Stream.STDOUT, b"a"),
        (Stream.STDERR, b"b"),
    ]
    assert bridge.drain() == []


def test_drain_respects_limit():
    bridge = ThreadBridge(maxsize=16)
    for i in range(5):
        bridge.put(Chunk("u", Stream.STDOUT, bytes([i])), stop_check=lambda: False)
    assert len(bridge.drain(limit=2)) == 2
    assert bridge.pending == 3


def test_eof_chunk_round_trips():
    bridge = ThreadBridge(maxsize=4)
    bridge.put(Chunk("u", Stream.STDOUT, b"", eof=True), stop_check=lambda: False)
    chunk = bridge.drain()[0]
    assert chunk.eof and chunk.data == b""


def test_full_bridge_blocks_then_stop_releases():
    bridge = ThreadBridge(maxsize=1)
    bridge.put(Chunk("u", Stream.STDOUT, b"first"), stop_check=lambda: False)

    stop = threading.Event()
    started = threading.Event()

    def producer() -> None:
        started.set()
        bridge.put(
            Chunk("u", Stream.STDOUT, b"second"),
            stop_check=stop.is_set,
            retry_interval=0.01,
        )

    thread = threading.Thread(target=producer)
    thread.start()
    started.wait()
    time.sleep(0.05)
    assert thread.is_alive()  # 队列满 → 背压，producer 被挡住
    stop.set()
    thread.join(1.0)
    assert not thread.is_alive()

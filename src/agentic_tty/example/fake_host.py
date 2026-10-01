"""假宿主：脚本化程序模拟（测试替身）。

按相对时间吐出预置输出，可对输入做回显与应答。它**不是终端模拟器**：
`ingest` 只把字节追加到一个纯文本尾部缓冲，`snapshot` 返回该缓冲——够用来
验证"摄入与日志相邻"和视图链路，不承担 VT 语义。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..core.ports import HostMetadata, SessionSpec

_POLL_INTERVAL = 0.002


@dataclass(frozen=True)
class FakeProgram:
    """一段被模拟的程序行为。时间以宿主创建时刻为原点。"""

    chunks: tuple[tuple[float, bytes], ...] = ()
    stderr_chunks: tuple[tuple[float, bytes], ...] = ()
    exit_code: int = 0
    exit_after: float | None = None
    echo_input: bool = False
    respond: Callable[[bytes], bytes] | None = None
    title: str | None = None


class FakeHost:
    """脚本化宿主：同时满足 TerminalHost 与 ProcessHost 两个端口。"""

    def __init__(self, spec: SessionSpec, program: FakeProgram) -> None:
        self._spec = spec
        self._program = program
        self._t0 = time.monotonic()
        self._out: list[tuple[float, bytes]] = [
            (self._t0 + delay, chunk) for delay, chunk in program.chunks
        ]
        self._err: list[tuple[float, bytes]] = [
            (self._t0 + delay, chunk) for delay, chunk in program.stderr_chunks
        ]
        self._exit_at = None if program.exit_after is None else self._t0 + program.exit_after
        self._exited = False
        self._closed = False
        self._stdin_closed = False
        self._input = bytearray()
        self._screen = bytearray()
        self._cols = spec.cols
        self._rows = spec.rows

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else 4242

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._drain(self._out, max_bytes, timeout)

    def write(self, data: bytes) -> None:
        if self._closed or self._stdin_closed:
            return
        self._input.extend(data)
        now = time.monotonic()
        if self._program.echo_input:
            self._out.append((now, bytes(data)))
        if self._program.respond is not None:
            self._out.append((now + 0.01, self._program.respond(bytes(data))))

    def try_wait(self) -> int | None:
        if not self._exited and self._exit_at is not None and time.monotonic() >= self._exit_at:
            self._exited = True
        return self._program.exit_code if self._exited else None

    def kill(self) -> None:
        self._exited = True

    def close(self) -> None:
        self._closed = True

    # ── TerminalHost ───────────────────────────────────────────

    def ingest(self, data: bytes) -> bytes:
        self._screen.extend(data)
        return b""

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows

    def snapshot(self) -> bytes:
        return bytes(self._screen)

    def metadata(self) -> HostMetadata:
        return HostMetadata(title=self._program.title, cwd=self._spec.cwd)

    # ── ProcessHost ────────────────────────────────────────────

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._drain(self._err, max_bytes, timeout)

    def close_stdin(self) -> None:
        self._stdin_closed = True

    # ── 观测（测试用） ─────────────────────────────────────────

    @property
    def received_input(self) -> bytes:
        return bytes(self._input)

    @property
    def size(self) -> tuple[int, int]:
        return self._cols, self._rows

    @property
    def stdin_closed(self) -> bool:
        return self._stdin_closed

    # ── 内部 ───────────────────────────────────────────────────

    def _take_due(self, queue: list[tuple[float, bytes]], max_bytes: int) -> bytes:
        now = time.monotonic()
        out = bytearray()
        while queue and queue[0][0] <= now and len(out) < max_bytes:
            due_at, chunk = queue[0]
            room = max_bytes - len(out)
            if len(chunk) <= room:
                queue.pop(0)
                out.extend(chunk)
            else:
                queue[0] = (due_at, chunk[room:])
                out.extend(chunk[:room])
        return bytes(out)

    def _drain(
        self, queue: list[tuple[float, bytes]], max_bytes: int, timeout: float | None
    ) -> bytes:
        data = self._take_due(queue, max_bytes)
        if data:
            return data
        if timeout is not None and timeout <= 0:
            return b""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._exited and not queue:
                return b""
            if deadline is not None and time.monotonic() >= deadline:
                return b""
            time.sleep(_POLL_INTERVAL)
            data = self._take_due(queue, max_bytes)
            if data:
                return data

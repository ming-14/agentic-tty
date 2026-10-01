"""会话抽象：身份 + 生命周期 + 摄入 + 视图。

Pty 会话与子进程会话共享本类，差异只有两处：

- `_streams()`：单流（Pty）还是双流（子进程）
- `_feed_model()`：是否喂终端模型

## 摄入在核心层闭合

`ingest_stream()` 内部把"喂终端模型"与"追加日志"**相邻执行**（同线程、中间无
IO），因此 `fed_offset == journal.end` 恒成立。屏幕视图与重建字节的正确性建立在
这一点上：读取屏幕（`screen_text()` / `rebuild_bytes()` 等）期间不可能有
`ingest_stream()` 插入，读到的恰是"重放 `[0, end)` 之后"的状态，与对齐点同源。

本类**不含**：连接、推送、订阅者、线程、sid、返回条件与等待引擎（那是命令层的）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ...foundation.logs import get_logger
from ..errors import CoreError
from ..journal import OutputJournal, Rebuild, Resume
from ..journal import plan_attach as _plan_attach
from ..ports import HostFactory, HostLifecycle, SessionSpec, Stream
from .state import SessionState, check_transition

_logger = get_logger("core.session")

# 强杀后轮询退出码的间隔
_EXIT_POLL_INTERVAL = 0.01


@dataclass(frozen=True, slots=True)
class IngestResult:
    """一次摄入的结果：本次写入的 offset 区间，以及模型要回写的应答。"""

    stream: Stream
    start_offset: int
    end_offset: int
    response: bytes = b""


class Session:
    """会话（协调器）。除注明外，所有方法都只允许所有者线程调用。"""

    def __init__(
        self,
        uid: str,
        spec: SessionSpec,
        host_factory: HostFactory,
        *,
        journal_budget_bytes: int,
    ) -> None:
        self.uid = uid
        self.spec = spec
        self.mode: str = spec.mode
        self.state = SessionState.CREATED
        self.exit_code: int | None = None
        self.error: str | None = None
        self.start_time = 0.0

        self._host_factory = host_factory
        self._host: HostLifecycle | None = None
        self._journal = OutputJournal(journal_budget_bytes)
        self._eof: set[Stream] = set()
        self._expect_eof = False

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    @property
    def host(self) -> HostLifecycle | None:
        """宿主句柄（供驱动方读取；不暴露核心层内部状态）。"""
        return self._host

    def expect_eof(self) -> None:
        """声明输出由外部驱动（有读线程）：退出必须等所有流 EOF 才算结束。"""
        self._expect_eof = True

    @property
    def drained(self) -> bool:
        """不再会有输出到达。

        进程退出与尾部输出到达之间有竞态：一退出就当作结束会丢掉最后一段输出，
        所以有外部驱动时必须等所有流都 EOF；没有外部驱动的会话则退出即结束。
        已关闭的会话宿主已释放，不可能再有输出。
        """
        if self.state is SessionState.CLOSED:
            return True
        if self.exit_code is None:
            return False
        if not self._expect_eof:
            return True
        return all(stream in self._eof for stream in self.streams())

    def start(self) -> None:
        """创建宿主并进入运行态。"""
        self._transition(SessionState.STARTING)
        try:
            self._host = self._host_factory(self.spec)
        except Exception as exc:  # 宿主创建失败：会话直接关死，错误留痕
            self.error = str(exc)
            self._transition(SessionState.CLOSED)
            raise CoreError(f"创建宿主失败: {exc}") from exc
        self.start_time = time.monotonic()
        self._transition(SessionState.RUNNING)
        _logger.info("会话已启动 uid=%s mode=%s argv=%s", self.uid, self.mode, list(self.spec.argv))

    def stop(self, timeout: float = 5.0) -> None:
        """强杀进程树并进入退出态；不释放宿主（由 `close` 负责）。

        先杀再关是硬要求：宿主关闭在部分平台上可能长时间阻塞，先终止子进程
        才能让它快速返回。强杀后退出码不会立刻可见（Windows Job Object 尤甚），
        必须等它出现——`exit_code` 留空会让 `drained` 永远为假，读线程也就不投
        EOF，驱动循环停不下来。
        """
        if self.state in (SessionState.CLOSED, SessionState.EXITED):
            return
        if self.state is SessionState.CREATED:
            self._transition(SessionState.CLOSED)  # 从未启动：没有宿主可停
            return
        if self._host is not None:
            try:
                self._host.kill()
            except Exception as exc:
                _logger.warning("终止宿主异常 uid=%s: %s", self.uid, exc)
            self._collect_exit(timeout)
        self._transition(SessionState.EXITED)
        _logger.info("会话已停止 uid=%s exit=%s", self.uid, self.exit_code)

    def _collect_exit(self, timeout: float) -> None:
        """等宿主给出退出码；到点仍拿不到就记警告，不让停止流程无界阻塞。"""
        if self._host is None or self.exit_code is not None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            code = self._host.try_wait()
            if code is not None:
                self.exit_code = code
                return
            if time.monotonic() >= deadline:
                _logger.warning("等待退出码超时 uid=%s，退出码将留空", self.uid)
                return
            time.sleep(_EXIT_POLL_INTERVAL)

    def close(self) -> None:
        """释放宿主并进入关闭态（幂等）。"""
        if self.state is SessionState.CLOSED:
            return
        if self.state is not SessionState.EXITED:
            self.stop()
        if self.state is SessionState.CLOSED:  # stop 可能已把未启动的会话关死
            return
        if self._host is not None:
            try:
                self._host.close()
            except Exception as exc:
                _logger.warning("关闭宿主异常 uid=%s: %s", self.uid, exc)
            self._host = None
        self._transition(SessionState.CLOSED)
        _logger.info("会话已关闭 uid=%s", self.uid)

    def refresh(self) -> None:
        """推进退出检测：同步宿主退出码与状态。**只允许所有者线程调用。**

        已进入退出态但还没拿到退出码（强杀后等超时）时继续补拿——留空会让
        `drained` 永远为假，驱动循环停不下来。
        """
        if self._host is None or self.exit_code is not None:
            return
        if self.state not in (SessionState.RUNNING, SessionState.EXITED):
            return
        code = self._host.try_wait()
        if code is None:
            return
        self.exit_code = code
        if self.state is SessionState.RUNNING:
            self._transition(SessionState.EXITED)
        _logger.info("会话已退出 uid=%s code=%s", self.uid, code)

    def descendants(self) -> tuple[int, ...]:
        """本会话进程树里**除根进程外**的当前成员 pid。**只允许所有者线程调用。**

        轮询式观测：比对前后两次结果即可得出"谁起来了、谁没了"。判定（什么时候算
        "命令跑完了"）属于命令层，核心层只出原料。
        """
        if self._host is None:
            raise CoreError("会话未启动")
        return self._host.descendants()

    # ════════════════════════════════════════════════════════════
    # 输出流
    # ════════════════════════════════════════════════════════════

    def streams(self) -> tuple[Stream, ...]:
        """本会话拥有的输出流。"""
        return self._streams()

    def read_stream(
        self, stream: Stream, timeout: float | None = 0.2, max_bytes: int = 65536
    ) -> bytes:
        """从某一路读一段。**只允许读线程调用**（不碰终端模型）。"""
        if self._host is None:
            return b""
        if stream is Stream.STDOUT:
            return self._host.read(max_bytes, timeout)
        return self._read_secondary(stream, timeout, max_bytes)

    def ingest_stream(self, stream: Stream, data: bytes) -> IngestResult:
        """把某一路的一段字节喂进模型并并入日志。**只允许所有者线程调用。**"""
        if stream not in self.streams():
            raise CoreError(f"{self.mode} 会话没有 {stream} 流")
        if self.state not in (SessionState.RUNNING, SessionState.EXITED):
            raise CoreError(f"会话不在可摄入状态: {self.state}")
        journal = self._journal_for(stream)
        start = journal.end_offset
        # 顺序是强制的：先喂模型，紧接着追加日志，中间不得有任何 IO
        response = self._feed_model(data, stream)
        journal.append(data)
        journal.trim_to_budget()
        return IngestResult(
            stream=stream,
            start_offset=start,
            end_offset=journal.end_offset,
            response=response,
        )

    def ingest(self, data: bytes) -> IngestResult:
        """主输出流的摄入（便捷方法）。"""
        return self.ingest_stream(Stream.STDOUT, data)

    def mark_eof(self, stream: Stream) -> None:
        """标记某一路输出已排空。

        **EOF 不等于进程退出**：程序可以先关掉 stdout/stderr 而继续运行
        （守护进程、`exec`），所以两件事必须分开记。
        """
        self._eof.add(stream)

    @property
    def eof_streams(self) -> frozenset[Stream]:
        return frozenset(self._eof)

    # ════════════════════════════════════════════════════════════
    # 输入
    # ════════════════════════════════════════════════════════════

    def send(self, data: bytes) -> None:
        """写输入。**只允许写线程调用**（写在缓冲写满时会阻塞）。"""
        if self._host is None:
            raise CoreError("会话未启动")
        self._host.write(data)

    def close_stdin(self) -> None:
        """关闭 stdin；只有子进程会话支持。"""
        raise CoreError(f"{self.mode} 会话不支持关闭 stdin")

    # ════════════════════════════════════════════════════════════
    # 视图
    # ════════════════════════════════════════════════════════════

    @property
    def journal(self) -> OutputJournal:
        """主输出流的日志。"""
        return self._journal

    def journal_for(self, stream: Stream) -> OutputJournal:
        return self._journal_for(stream)

    def read_all(self, stream: Stream = Stream.STDOUT) -> bytes:
        return self._journal_for(stream).read(0)

    def read_range(
        self, start: int, end: int | None = None, stream: Stream = Stream.STDOUT
    ) -> bytes:
        journal = self._journal_for(stream)
        if end is None:
            return journal.read(start)
        return journal.read(start, max(0, end - start))

    def attach_plan(self, cursor: int | None, stream: Stream = Stream.STDOUT) -> Resume | Rebuild:
        """给订阅者的对齐决策（纯函数转发）。"""
        journal = self._journal_for(stream)
        return _plan_attach(journal, cursor, journal.end_offset)

    def resize(self, cols: int, rows: int) -> None:
        """改尺寸；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有尺寸")

    def rebuild_bytes(self) -> bytes:
        """重建字节（喂进空终端模型即可还原当前状态）；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    def screen_text(self) -> str:
        """可见屏幕纯文本；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    def full_text(self) -> str:
        """含滚动历史的可见文本；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    def screen_cells(self) -> tuple[tuple[str, ...], ...]:
        """可见屏幕字符格栅；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    def render_svg(self) -> str:
        """可见屏幕的 SVG；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        """可见屏幕的位图；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有屏幕")

    # ════════════════════════════════════════════════════════════
    # 子类接缝
    # ════════════════════════════════════════════════════════════

    def _streams(self) -> tuple[Stream, ...]:
        return (Stream.STDOUT,)

    def _feed_model(self, data: bytes, stream: Stream) -> bytes:
        """把字节喂进终端模型并返回应答；无模型的会话返回空。"""
        return b""

    def _read_secondary(self, stream: Stream, timeout: float | None, max_bytes: int) -> bytes:
        raise CoreError(f"{self.mode} 会话没有 {stream} 流")

    def _journal_for(self, stream: Stream) -> OutputJournal:
        if stream is not Stream.STDOUT:
            raise CoreError(f"{self.mode} 会话没有 {stream} 流")
        return self._journal

    def _transition(self, dst: SessionState) -> None:
        check_transition(self.state, dst)
        self.state = dst

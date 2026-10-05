"""会话抽象：身份 + 生命周期 + 摄入 + 视图。

Pty 会话与子进程会话共享本类，差异只有两处：`_streams()`（单流 / 双流）与
`_feed_model()`（是否喂终端模型）。

`ingest_stream()` 把"喂终端模型"与"追加日志"相邻执行（同线程、中间无 IO），因此
`fed_offset == journal.end` 恒成立——屏幕视图与重建字节的正确性建立在这上面。

本类不含：连接、推送、订阅者、线程、sid、返回条件与等待引擎（那是命令层的）。
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

_logger = get_logger("core.session.base")


@dataclass(frozen=True, slots=True)
class IngestResult:
    """一次摄入的结果：本次写入的 offset 区间，以及模型要回写的应答。"""

    stream: Stream
    start_offset: int
    end_offset: int
    response: bytes = b""


@dataclass(frozen=True, slots=True)
class ResizeEvent:
    """一次尺寸变更。

    `offset` 落在**输出字节日志的 offset 空间**里：该 offset 起（含）的字节按
    `cols × rows` 解释。订阅者据此在字节流里插帧——否则落后的 raw 订阅者会拿新尺寸
    去解释旧字节（见架构设计 §4.5）。
    """

    offset: int
    cols: int
    rows: int


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
        所以有外部驱动时必须等所有流都 EOF；没有外部驱动则退出即结束。已关闭的
        会话宿主已释放，不可能再有输出。

        "已退出"以**状态**为准，而不是退出码——强杀后退出码可能永远拿不到，用它
        判定会让 `drained` 恒为假，等待方死等。
        """
        if self.state is SessionState.CLOSED:
            return True
        if self.state is not SessionState.EXITED:
            return False
        if not self._expect_eof:
            return True
        return all(stream in self._eof for stream in self.streams())

    def start(self) -> None:
        """创建宿主并进入运行态。"""
        self._transition(SessionState.STARTING)
        try:
            self._host = self._host_factory(self.spec)
        except Exception as exc:  # 宿主创建失败：会话关死、错误留痕
            self.error = str(exc)
            self._transition(SessionState.CLOSED)
            # 原样透出：类型就是给上层的判据（缺依赖 / 命令不存在 / 内部缺陷），
            # 重包成 CoreError 反而让它们不可区分。
            raise
        self.start_time = time.monotonic()
        self._transition(SessionState.RUNNING)
        _logger.info("会话已启动 uid=%s mode=%s argv=%s", self.uid, self.mode, list(self.spec.argv))

    def stop(self) -> None:
        """强杀进程树并进入退出态；不释放宿主（由 `close` 负责）。

        先杀再关是硬要求：宿主关闭在部分平台上可能长时间阻塞，先终止子进程才能让它
        快速返回。

        **不在这里等退出码**——等待是时序，属于驱动方（`refresh` 负责补拿）；这里只
        同步查一次，拿到就记下。`drained` 以状态为准，所以退出码晚到不影响它。
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
            code = self._host.try_wait()
            if code is not None:
                self.exit_code = code
        self._transition(SessionState.EXITED)
        _logger.info("会话已停止 uid=%s exit=%s", self.uid, self.exit_code)

    def close(self) -> None:
        """释放宿主并进入关闭态（幂等）。

        **可能长时间阻塞**（内含 `stop()` 的轮询与宿主关闭），不得在所有者线程上直接调用。
        """
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

        退出态但还没拿到退出码（强杀后晚到）时继续补拿。`exit_code` 是给消费者的
        信息，`drained` 不依赖它。
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

        轮询式观测：比对前后两次结果即可得出"谁起来了、谁没了"。判定属于命令层，
        核心层只出原料。
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
        """从某一路读一段。**只允许读线程调用**（不碰终端模型）。

        空返回值表示**本轮无数据（超时）**，不代表 EOF。宿主已释放时明确报错，不与
        "读空"混同——静默返回空会让驱动方把"没有宿主"当成"暂时没输出"而空转。
        """
        if self._host is None:
            raise CoreError(f"没有可读的宿主（会话状态 {self.state}）")
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
        if journal.trim_to_budget():
            self._journal_trimmed(stream)
        return IngestResult(
            stream=stream,
            start_offset=start,
            end_offset=journal.end_offset,
            response=response,
        )

    def mark_eof(self, stream: Stream) -> None:
        """标记某一路输出已排空。

        **EOF 不等于进程退出**：程序可以先关掉 stdout/stderr 而继续运行（守护进程、
        `exec`），所以两件事必须分开记。
        """
        if stream not in self.streams():
            raise CoreError(f"{self.mode} 会话没有 {stream} 流")
        self._eof.add(stream)

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
        return _plan_attach(self._journal_for(stream), cursor)

    def resize(self, cols: int, rows: int) -> None:
        """改尺寸；只有终端会话支持。"""
        raise CoreError(f"{self.mode} 会话没有尺寸")

    def resize_events(self, since: int = 0) -> tuple[ResizeEvent, ...]:
        """尺寸变更事件（按 offset 升序，只给 `offset >= since` 的）。

        事件与输出字节共用同一个 offset 空间，订阅者据此知道"从哪个 offset 起换尺寸"。
        没有屏幕的会话恒为空。
        """
        return ()

    def size_at(self, offset: int) -> tuple[int, int] | None:
        """`offset` 处的字节按多大解释；没有屏幕的会话返回 `None`。

        尺寸变更史会被日志裁剪裁短，所以**得由会话自己作答**——它记得基线（保留区
        起点那一刻的尺寸），而不是让订阅者去猜（见架构设计 §4.5）。
        """
        return None

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

    def _journal_trimmed(self, stream: Stream) -> None:
        """某一路日志刚裁过：派生数据（如尺寸变更史）跟着收敛，别独自无限增长。

        只在**确实裁掉字节**时调——没裁就没有要收敛的东西。
        """

    def _read_secondary(self, stream: Stream, timeout: float | None, max_bytes: int) -> bytes:
        raise CoreError(f"{self.mode} 会话没有 {stream} 流")

    def _journal_for(self, stream: Stream) -> OutputJournal:
        if stream is not Stream.STDOUT:
            raise CoreError(f"{self.mode} 会话没有 {stream} 流")
        return self._journal

    def _transition(self, dst: SessionState) -> None:
        check_transition(self.state, dst)
        self.state = dst

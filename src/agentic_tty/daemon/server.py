"""守护进程本体：生命周期管理，承载核心层。

**它没有业务代码**，也不实现网络。它管两件事：

1. **自身的生命周期**：装配顺序即依赖顺序——取单实例锁 → 依赖检查（由入口注入）→
   数据目录与日志 → 构造请求处理层 → 写 pid → 装信号 → 进循环；每一步都能回滚，
   停止时逆序收尾并带整体超时兜底。
2. **承载核心层**：把 `core` 装进这个进程，并做唯一的**所有者线程**。

**请求与答复对它不透明**：消费者经 `submit` / `submit_input` 把东西投进来（任何线程可
调用），它在所有者线程上交给注入的处理层；处理层交出的答复经构造时注入的 `on_reply`
回调交回消费者。监听、accept、解帧、按连接路由都不在这里。

线程账：所有者循环占一个线程；会话状态只在它上面改动，跨线程只经过一个入站队列。
"""

from __future__ import annotations

import os
import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..foundation.logs import add_rotating_file, get_logger
from ..foundation.paths import default_runtime_dir
from .config import DaemonConfig
from .errors import AlreadyRunning, DaemonError, NotStarted
from .handler import Reply, RequestHandler
from .platform.signals import install_shutdown_handler
from .platform.single_instance import SingleInstance

_logger = get_logger("daemon.server")

_INBOUND_BATCH = 64
"""每轮从入站队列取走的条数上限（公平性：不让投递方独占一轮）。"""
_PUT_TIMEOUT = 0.1
"""入站队列满时的重试步长；也是"已请求停止"的响应粒度。"""


@dataclass(frozen=True, slots=True)
class _Input:
    """一段上行字节（消费者投递，所有者线程消费）。"""

    key: str
    data: bytes


class Daemon:
    """守护进程。"""

    def __init__(
        self,
        config: DaemonConfig,
        handler_factory: Callable[[], RequestHandler],
        *,
        on_reply: Callable[[Reply], None],
        check_dependencies: Callable[[], None],
    ) -> None:
        self._config = config
        self._handler_factory = handler_factory
        self._on_reply = on_reply
        self._check_dependencies = check_dependencies
        self._dir = config.runtime_dir or default_runtime_dir(config.name)

        self._lock: SingleInstance | None = None
        self._handler: RequestHandler | None = None
        self._inbound: queue.Queue[object] = queue.Queue(config.inbound_maxsize)
        self._stop_requested = threading.Event()
        self._started = False

    # ════════════════════════════════════════════════════════════
    # 对外状态
    # ════════════════════════════════════════════════════════════

    @property
    def pid_path(self) -> Path:
        """就绪后写入自身 pid，供外部确认"它起来了吗"。"""
        return self._dir / "daemon.pid"

    @property
    def runtime_dir(self) -> Path:
        """运行时目录（pid / 锁 / 日志）。"""
        return self._dir

    @property
    def running(self) -> bool:
        return self._started and not self._stop_requested.is_set()

    def request_stop(self) -> None:
        """请求停止（信号处理器与外部调用都走这里）。"""
        self._stop_requested.set()

    # ════════════════════════════════════════════════════════════
    # 投递（消费者线程侧）
    # ════════════════════════════════════════════════════════════

    def submit(self, request: object) -> bool:
        """把一条请求投给所有者线程。**任何线程都可以调用**，消费者的读线程用它。

        队列满时投递方等待——背压一路传回消费者，守护进程不无限缓冲。返回 `False`
        表示已被请求停止，调用方应当放弃这条请求。
        """
        return self._offer(request)

    def submit_input(self, key: str, data: bytes) -> bool:
        """把一段上行字节投给所有者线程。**任何线程都可以调用。**"""
        return self._offer(_Input(key, data))

    def _offer(self, item: object) -> bool:
        while not self._stop_requested.is_set():
            try:
                self._inbound.put(item, timeout=_PUT_TIMEOUT)
                return True
            except queue.Full:
                continue
        return False

    # ════════════════════════════════════════════════════════════
    # 启动
    # ════════════════════════════════════════════════════════════

    def start(self) -> None:
        """按固定顺序装配；任一步失败就逆序回滚，绝不留半个进程。"""
        if self._started:
            raise DaemonError("守护进程已启动")
        try:
            self._acquire_lock()
            self._check_dependencies()
            self._prepare_dirs()
            self._build_handler()
            self._write_pid()
            self._install_signals()
        except Exception:
            self._rollback()
            raise
        self._started = True
        _logger.info("守护进程就绪 pid=%s dir=%s", os.getpid(), self._dir)

    def _acquire_lock(self) -> None:
        # 最早取锁：两个进程同时初始化会各自维护互斥的会话坐标。
        lock = SingleInstance(self._config.name, self._dir)
        if not lock.acquire():
            raise AlreadyRunning(f"已有守护进程在运行（锁名 {self._config.name}）")
        self._lock = lock

    def _prepare_dirs(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._config.write_log_file:
            add_rotating_file(
                self._dir / "logs" / f"{self._config.name}.log",
                max_bytes=self._config.log_max_bytes,
                backups=self._config.log_backups,
            )

    def _build_handler(self) -> None:
        self._handler = self._handler_factory()

    def _write_pid(self) -> None:
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _install_signals(self) -> None:
        installed = install_shutdown_handler(self._on_signal)
        if installed:
            names = ", ".join(signal.Signals(number).name for number in installed)
            _logger.info("已装信号处理器: %s", names)

    def _rollback(self) -> None:
        """逆序回滚：装了什么就撤什么。每个撤除都幂等，所以不必记账。"""
        self._remove_pid()
        self._handler = None
        self._release_lock()

    def _on_signal(self, signum: int) -> None:
        _logger.info("收到信号 %s，开始收尾", signum)
        self.request_stop()

    # ════════════════════════════════════════════════════════════
    # 所有者循环
    # ════════════════════════════════════════════════════════════

    def run(self) -> None:
        """所有者循环，阻塞到 `request_stop()`。

        **所有会话状态都只在这个线程上改动**：投递、摄入、推进、答复全在这里，所以
        核心层与请求处理层都不需要锁。
        """
        if not self._started:
            raise NotStarted("守护进程未启动")
        handler = self._handler
        if handler is None:
            raise NotStarted("请求处理层未装配")
        _logger.info("进入事件循环 pid=%s", os.getpid())
        while not self._stop_requested.is_set():
            self._drain_inbound(handler)
            self._pump_handler(handler)
            self._deliver(self._poll_handler(handler))
            time.sleep(self._config.tick_interval)

    def _drain_inbound(self, handler: RequestHandler) -> None:
        """把消费者投进来的请求与上行字节交给处理层。**只在所有者线程上跑。**"""
        for _ in range(_INBOUND_BATCH):
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Input):
                self._on_input(handler, item)
            else:
                reply = self._handle(handler, item)
                if reply is not None:
                    self._deliver_one(reply)

    def _on_input(self, handler: RequestHandler, item: _Input) -> None:
        try:
            handler.on_input(item.key, item.data)
        except Exception as exc:
            # 会话可能刚被关掉；一个坏 key 不值得停下整条循环。
            _logger.warning("上行字节无人接收 key=%s: %s", item.key, exc)

    def _handle(self, handler: RequestHandler, request: object) -> Reply | None:
        """处理一条请求；返回 `None` 表示处理层已登记等待。

        处理层抛异常不该拖垮守护进程，也不该让消费者干等——一律转成一条明确的失败
        答复；失败长什么样由处理层决定（守护进程不认识报文）。
        """
        try:
            return handler.handle(request)
        except Exception as exc:
            _logger.exception("处理请求失败")
            return self._failure(handler, request, exc)

    def _failure(
        self, handler: RequestHandler, request: object, error: BaseException
    ) -> Reply | None:
        try:
            return handler.failure(request, error)
        except Exception:
            _logger.exception("处理层未能交出失败答复")
            return None

    def _pump_handler(self, handler: RequestHandler) -> None:
        """推进处理层。异常在这里隔离——一次推进失败不该杀死整条所有者循环。"""
        try:
            handler.pump()
        except Exception:
            _logger.exception("请求处理层推进失败（已隔离，循环继续）")

    def _poll_handler(self, handler: RequestHandler) -> list[Reply]:
        """取处理层此刻的答复；失败只丢这一轮，循环照常走。"""
        try:
            return handler.poll()
        except Exception:
            _logger.exception("请求处理层交出答复失败（已隔离）")
            return []

    def _deliver(self, replies: list[Reply]) -> None:
        for reply in replies:
            self._deliver_one(reply)

    def _deliver_one(self, reply: Reply) -> None:
        """把答复交回消费者。回调必须不阻塞——它在所有者线程上跑。"""
        try:
            self._on_reply(reply)
        except Exception:
            _logger.exception("交回答复失败（已隔离，循环继续）")

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def stop(self, timeout: float | None = None) -> bool:
        """逆序收尾，返回是否在预算内停干净。

        **强制退出由入口决定**：库不该杀进程，所以这里只报告，剩下的交给调用方。
        """
        if not self._started:
            return True
        budget = self._config.stop_timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        self._stop_requested.set()

        self._drain_inflight(deadline)  # 1 不再接新命令，在途的给一小段时间跑完
        shutdown = self._start_shutdown()  # 2 会话收尾（可能长时间阻塞，放线程里）

        finished = self._join_shutdown(shutdown, deadline)
        self._remove_pid()  # 3 删 pid
        self._release_lock()  # 4 放锁
        self._handler = None
        self._started = False
        if finished:
            _logger.info("守护进程已停止")
        else:
            _logger.error("收尾未在 %.1fs 内完成，仍有线程在跑", budget)
        return finished

    def _drain_inflight(self, deadline: float) -> None:
        """draining：不再处理新命令，只把手上的等待跑完。

        处理层手上还压着等待（`pending()`）才等；一旦都答完就立刻往下走。
        """
        handler = self._handler
        if handler is None:
            return
        limit = min(deadline, time.monotonic() + self._config.drain_timeout)
        while self._pending(handler) and time.monotonic() < limit:
            self._pump_handler(handler)
            self._deliver(self._poll_handler(handler))
            time.sleep(self._config.tick_interval)

    @staticmethod
    def _pending(handler: RequestHandler) -> int:
        try:
            return handler.pending()
        except Exception:
            _logger.exception("请求处理层报不出压着的等待数（当作没有）")
            return 0

    def _start_shutdown(self) -> threading.Thread | None:
        handler = self._handler
        if handler is None:
            return None

        def _shutdown() -> None:
            try:
                handler.shutdown()
            except Exception:
                _logger.exception("请求处理层收尾失败")

        thread = threading.Thread(target=_shutdown, name="handler-shutdown", daemon=True)
        thread.start()
        return thread

    def _join_shutdown(self, shutdown: threading.Thread | None, deadline: float) -> bool:
        if shutdown is None:
            return True
        shutdown.join(max(0.0, deadline - time.monotonic()))
        return not shutdown.is_alive()

    def _remove_pid(self) -> None:
        self.pid_path.unlink(missing_ok=True)

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

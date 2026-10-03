"""守护进程本体：生命周期管理，承载核心层，转发。

**它没有业务代码**，也不实现网络。它管三件事：

1. **自身的生命周期**：装配顺序即依赖顺序——取单实例锁 → 依赖检查（由装配层注入）→
   数据目录与日志 → 构造请求处理层 → 挂接入点 → 装信号 → 进循环；每一步都能回滚，
   停止时逆序收尾并带整体超时兜底。
2. **承载核心层**：把 `core` 装进这个进程，并做唯一的**所有者线程**。
3. **转发**：接入点（本机管道）上的请求进接缝，答复按请求身份写回它来的那条连接。

**请求与答复对它不透明**：接入点把线协议解成 `Envelope` 投进 `submit`；它在所有者线程
上交给注入的处理层。没挂接入点（进程内消费者）时走构造时注入的 `on_reply`。

**收尾也在所有者线程上做**：处理层只许被那一个线程碰（它的契约就是"不需要锁"），
所以 `run()` 看到停止标志后自己 draining + 收尾；`stop()` 只负责"发信号 + 等它做完"。

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
from enum import StrEnum

from ..foundation.logs import add_rotating_file, get_logger
from ..foundation.paths import default_runtime_dir
from ..transport.pipe import pipe_address
from .access_point import AccessPoint
from .config import DaemonConfig
from .errors import AlreadyRunning, DaemonError, NotStarted
from .handler import Reply, RequestHandler
from .platform.signals import InstalledSignals, install_shutdown_handler
from .platform.single_instance import SingleInstance

_logger = get_logger("daemon.server")

_INBOUND_BATCH = 64
"""每轮从入站队列取走的条数上限（公平性：不让投递方独占一轮）。"""
_PUT_TIMEOUT = 0.1
"""入站队列满时的重试步长；也是"已请求停止"的响应粒度。"""


class SubmitOutcome(StrEnum):
    """投递的结果。

    刻意不是布尔：`core.runtime` 的 `submit_input` 返回的是**输入判定**（收下 / 越水位 /
    超上限），含义完全不同。两者同名，若都用布尔，消费者必然看错。
    """

    DELIVERED = "delivered"
    """已进队，所有者线程会处理它。"""
    STOPPING = "stopping"
    """守护进程已请求停止，本条被放弃——调用方应当不再投递。"""


@dataclass(frozen=True, slots=True)
class _Input:
    """一段上行字节（消费者投递，所有者线程消费）。"""

    key: str
    data: bytes


def _safe_shutdown(handler: RequestHandler) -> None:
    try:
        handler.shutdown()
    except Exception:
        _logger.exception("请求处理层收尾失败")


class Daemon:
    """守护进程。"""

    def __init__(
        self,
        config: DaemonConfig,
        handler_factory: Callable[[], RequestHandler],
        *,
        check_dependencies: Callable[[], None],
        on_reply: Callable[[Reply], None] | None = None,
    ) -> None:
        self._config = config
        self._handler_factory = handler_factory
        self._on_reply = on_reply
        self._check_dependencies = check_dependencies
        self._dir = config.runtime_dir or default_runtime_dir(config.name)

        self._lock: SingleInstance | None = None
        self._handler: RequestHandler | None = None
        self._signals: InstalledSignals | None = None
        self._access_point: AccessPoint | None = None
        self._inbound: queue.Queue[object] = queue.Queue(config.inbound_maxsize)

        self._stop_requested = threading.Event()
        self._loop_ran = False
        """`run()` 是否进过循环——决定收尾该由谁做（见 `stop`）。"""
        self._stopped = threading.Event()
        """`run()` 已收尾并返回。"""
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        """收尾只跑一次：`run()` 与 `stop()` 都可能走到那儿，取决于谁先看到停止标志。"""
        self._started = False

    # ════════════════════════════════════════════════════════════
    # 对外状态
    # ════════════════════════════════════════════════════════════

    @property
    def running(self) -> bool:
        return self._started and not self._stop_requested.is_set()

    def request_stop(self) -> None:
        """请求停止（信号处理器与外部调用都走这里）。"""
        self._stop_requested.set()

    # ════════════════════════════════════════════════════════════
    # 投递（消费者线程侧）
    # ════════════════════════════════════════════════════════════

    def submit(self, request: object) -> SubmitOutcome:
        """把一条请求投给所有者线程。**任何线程都可以调用**，消费者的读线程用它。

        队列满时投递方等待——背压一路传回消费者，守护进程不无限缓冲。返回 `STOPPING`
        表示已被请求停止，调用方应当放弃这条请求。
        """
        return self._offer(request)

    def submit_input(self, key: str, data: bytes) -> SubmitOutcome:
        """把一段上行字节投给所有者线程。**任何线程都可以调用。**

        `key` 的语义由消费者定（如会话 uid）；与 `core.runtime` 的 `submit_input` 同名
        但含义不同——后者返回的是输入队列的判定。
        """
        return self._offer(_Input(key, data))

    def _offer(self, item: object) -> SubmitOutcome:
        while not self._stop_requested.is_set():
            try:
                self._inbound.put(item, timeout=_PUT_TIMEOUT)
                return SubmitOutcome.DELIVERED
            except queue.Full:
                continue
        return SubmitOutcome.STOPPING

    # ════════════════════════════════════════════════════════════
    # 启动
    # ════════════════════════════════════════════════════════════

    def start(self) -> None:
        """按固定顺序装配；任一步失败就逆序回滚，绝不留半个进程。"""
        if self._started:
            raise DaemonError("守护进程已启动")
        # 复位：上一次 stop() 之后这些标志还留着，不复位会让 run() 一进去就退出。
        self._stop_requested.clear()
        self._stopped.clear()
        self._shutdown_done = False
        self._loop_ran = False
        try:
            self._acquire_lock()
            self._check_dependencies()
            self._prepare_dirs()
            self._build_handler()
            self._mount_access_point()
            self._install_signals()
        except Exception:
            self._rollback()
            raise
        self._started = True
        _logger.info("守护进程已起来 pid=%s dir=%s", os.getpid(), self._dir)

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

    def _mount_access_point(self) -> None:
        """挂接入点——配置里给了管道名才挂（它是守护进程对外的唯一口子）。

        端点落在**运行时目录**里（与锁同一处），所以地址要带上它。
        """
        name = self._config.listen
        if name is None:
            return
        address = pipe_address(name, self._dir)
        self._access_point = AccessPoint(
            address, on_request=self.submit, on_input=self.submit_input
        )
        self._access_point.open()

    def _install_signals(self) -> None:
        self._signals = install_shutdown_handler(self._on_signal)
        numbers = self._signals.numbers
        if numbers:
            names = ", ".join(signal.Signals(number).name for number in numbers)
            _logger.info("已装信号处理器: %s", names)

    def _restore_signals(self) -> None:
        if self._signals is not None:
            self._signals.restore()
            self._signals = None

    def _rollback(self) -> None:
        """逆序回滚：装了什么就撤什么。每个撤除都幂等，所以不必记账。"""
        self._restore_signals()
        if self._access_point is not None:
            self._access_point.close()
            self._access_point = None
        handler, self._handler = self._handler, None
        if handler is not None:
            self._run_handler_shutdown(handler, self._config.stop_timeout)
        self._release_lock()

    def _on_signal(self, signum: int) -> None:
        _logger.info("收到信号 %s，开始收尾", signum)
        self.request_stop()

    # ════════════════════════════════════════════════════════════
    # 所有者循环
    # ════════════════════════════════════════════════════════════

    def run(self) -> None:
        """所有者循环，阻塞到 `request_stop()`。

        **所有会话状态都只在这个线程上改动**：投递、摄入、推进、答复、收尾全在这里，
        所以核心层与请求处理层都不需要锁。
        """
        if not self._started:
            raise NotStarted("守护进程未启动")
        handler = self._handler
        if handler is None:
            raise NotStarted("请求处理层未装配")
        _logger.info("进入事件循环 pid=%s", os.getpid())
        self._loop_ran = True
        try:
            while not self._stop_requested.is_set():
                self._wait_for_work()
                self._drain_inbound(handler)
                self._pump_handler(handler)
                self._deliver(self._poll_handler(handler))
            self._shutdown(handler, self._config.stop_timeout)
        finally:
            self._stopped.set()

    def _wait_for_work(self) -> None:
        """这一轮的空闲等待。

        接入点在场就在它的 `accept` 上等（顺带读一轮连接，请求当轮就能进队列）；
        没挂接入点（进程内消费者）就纯定时。
        """
        if self._access_point is not None:
            self._access_point.pump(self._config.tick_interval)
        else:
            time.sleep(self._config.tick_interval)

    def _drain_inbound(self, handler: RequestHandler, limit: int | None = _INBOUND_BATCH) -> None:
        """把消费者投进来的请求与上行字节交给处理层。**只在所有者线程上跑。**

        `limit=None` 表示一直取到队空——收尾时用（`submit` 已 ack 的必须处理掉）。
        """
        taken = 0
        while limit is None or taken < limit:
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                return
            taken += 1
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
        """把答复交回消费者：来自接入点的写回那条连接，其余走 `on_reply`。

        整段都兜住异常——它在所有者线程上跑，一次交答复失败不能炸穿循环。
        """
        try:
            if self._access_point is not None and self._access_point.send(reply):
                return
            if self._on_reply is None:
                _logger.warning("答复无处可去（没挂接入点，也没给 on_reply）")
                return
            self._on_reply(reply)
        except Exception:
            _logger.exception("交回答复失败（已隔离，循环继续）")

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def stop(self, timeout: float | None = None) -> bool:
        """逆序收尾，返回是否在预算内停干净。

        **收尾由所有者线程做**（处理层只许被那一个线程碰），所以这里分两种情形：
        `run()` 跑起来了就等它做完；没跑起来（`start()` 之后直接 `stop()`）就由这里代做。

        **强制退出由入口决定**：库不该杀进程，所以这里只报告，剩下的交给调用方。
        """
        if not self._started:
            return True
        budget = self._config.stop_timeout if timeout is None else timeout
        self._stop_requested.set()
        if self._loop_ran:
            # 所有者循环自己收尾，这里只等它——绝不从第二个线程碰处理层。
            finished = self._stopped.wait(budget)
        else:
            handler = self._handler
            finished = True if handler is None else self._shutdown(handler, budget)
        self._restore_signals()
        if self._access_point is not None:
            self._access_point.close()
            self._access_point = None
        self._release_lock()
        self._handler = None
        self._started = False
        if finished:
            _logger.info("守护进程已停止")
        else:
            _logger.error("收尾未在 %.1fs 内完成，仍有线程在跑", budget)
        return finished

    def _shutdown(self, handler: RequestHandler, budget: float) -> bool:
        """收尾：关监听（不再接新连接）→ draining → 会话收尾。

        **只在所有者线程上做**，且只做一次——`run()` 与 `stop()` 都可能走到这儿。
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                return True
            self._shutdown_done = True
        deadline = time.monotonic() + max(0.0, budget)
        if self._access_point is not None:
            self._access_point.stop_accepting()
        self._drain_inflight(handler, deadline)
        return self._run_handler_shutdown(handler, deadline - time.monotonic())

    def _drain_inflight(self, handler: RequestHandler, deadline: float) -> None:
        """draining：不再接新命令，先把已进队的处理掉，再把手上的等待跑完。

        `submit()` 返回 `DELIVERED` 就是承诺"所有者线程会处理它"——队列里已进的必须
        走一遍，不能随收尾一起丢掉。
        """
        self._drain_inbound(handler, limit=None)
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

    def _run_handler_shutdown(self, handler: RequestHandler, budget: float) -> bool:
        """把 `handler.shutdown()` 放线程里跑（宿主关闭可能长时间阻塞），带超时。"""
        thread = threading.Thread(
            target=_safe_shutdown, args=(handler,), name="handler-shutdown", daemon=True
        )
        thread.start()
        thread.join(max(0.0, budget))
        return not thread.is_alive()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

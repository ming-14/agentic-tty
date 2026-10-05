"""守护进程本体：生命周期管理，承载核心层，转发。

装配顺序即依赖顺序——取单实例锁 → 数据目录与日志 → 构造请求处理层 → 挂接入点 →
装信号 → 进循环；每一步都能回滚，停止时逆序收尾。

**两条请求通路**：接入点上的请求已经在所有者线程上，直接交给处理层；跨线程投递
（进程内消费者）走有界入站队列，投递方在队列满时等待、背压传回消费者。两条路都汇到
同一个 `_dispatch`。

**答复必达**：交答复时若连接写缓冲满了（`CONGESTED`），**谁负责重发由处理层说了算**
（`owns_retransmission`）——订阅推送的帧摆在处理层的 `out` 里，它下一轮原样再交，
守护进程不插手；响应型答复处理层没有第二份，就由守护进程按请求攒进 `_backlog`，
下一轮（此时写线程可能已腾出空位）重投，直到收下或连接没了。两边都攒就会重复投递，
所以这条分拣必须**在回告之前**问（回告可能顺手把订阅注销掉）。

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

from ..config import DaemonConfig, endpoint_name, lock_name, runtime_dir
from ..foundation.instance import InstanceLock
from ..foundation.logs import add_rotating_file, get_logger
from ..protocol.contracts.daemon_ipc import Notice
from ..protocol.envelope import make_response
from ..transport.pipe import pipe_address
from .access_point import AccessPoint
from .errors import AlreadyRunning, DaemonError, NotStarted
from .handler import Delivery, InputAction, Reply, RequestHandler
from .platform.signals import InstalledSignals, install_shutdown_handler

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
    """一段上行字节（消费者投递，所有者线程消费）。

    `connection` 是它来的那条连接——**判定要回到发送方去**（越水位下发通知 / 违约断连），
    所以得一路带着。进程内消费者投进来的字节没有连接，为 `None`。
    """

    key: str
    data: bytes
    connection: object | None = None


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
        handler_factory: Callable[[str], RequestHandler],
        *,
        on_reply: Callable[[Reply], None] | None = None,
    ) -> None:
        self._config = config
        self._handler_factory = handler_factory
        self._on_reply = on_reply
        self._dir = config.directory or runtime_dir(config.name)
        # 地址只在这里算一次：装配层与处理层都不再预测它（预测错了只会静默连不上）。
        self._address = (
            pipe_address(endpoint_name(config.name), self._dir) if config.mount_endpoint else ""
        )

        self._lock: InstanceLock | None = None
        self._handler: RequestHandler | None = None
        self._signals: InstalledSignals | None = None
        self._access_point: AccessPoint | None = None
        self._inbound: queue.Queue[object] = queue.Queue(config.inbound_maxsize)
        self._backlog: dict[int, Reply] = {}
        """已被 `CONGESTED` 挡下的答复：下一轮重投，直到收下或连接没了。

        键是 `id(request)`——**不是** `id()` 做身份那一套的反面：这里安全，因为
        `Reply` 自己**强引用着** `request`（`Reply.request`），条目在表里一天，请求对象就
        一天不会被回收，`id()` 也就不会被别处复用。反而不能用 `request` 本身当键：信封
        里带着 dict，请求对象不一定可哈希。
        """

        self._held: dict[object, set[str]] = {}
        """已下发 `INPUT_HOLD` 的连接 → 它被 hold 的那些 key。

        每轮拿它去问处理层"排空了没"，排空就下发 `INPUT_RESUME` 并撤掉那一条。**按连接
        分组、值是一组 key**：同一条连接可以送多个会话的输入，各自的水位互不相干，漏记
        一个就会让那个会话的发送方永远卡在 hold 里。连接对象按身份作键（它本来就是连接）。
        """

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

    @property
    def address(self) -> str:
        """接入点地址（由实例名派生）；不挂接入点时为空串。"""
        return self._address

    def request_stop(self) -> None:
        """请求停止（信号处理器与外部调用都走这里）。"""
        self._stop_requested.set()

    # ════════════════════════════════════════════════════════════
    # 投递（消费者线程侧）
    # ════════════════════════════════════════════════════════════

    def submit(self, request: object) -> SubmitOutcome:
        """**跨线程**把一条请求投给所有者线程（消费者的读线程用它）。

        队列满时投递方等待——背压一路传回消费者，守护进程不无限缓冲。返回 `STOPPING`
        表示已被请求停止，调用方应当放弃这条请求。

        接入点上的请求不走这里（它本来就在所有者线程上，见 `_serve`）。
        """
        return self._offer(request)

    def submit_input(self, key: str, data: bytes) -> SubmitOutcome:
        """**跨线程**把一段上行字节投给所有者线程。

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
        # 复位：上一轮的残留不能带进这一轮——停止标志不复位会让 run() 一进去就退出，
        # 入站队列不清则会把上一轮没排空的请求投给新的处理层。
        self._stop_requested.clear()
        self._stopped.clear()
        self._shutdown_done = False
        self._loop_ran = False
        self._inbound = queue.Queue(self._config.inbound_maxsize)
        self._backlog.clear()
        self._held.clear()
        try:
            self._acquire_lock()
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
        lock = InstanceLock(lock_name(self._config.name, self._dir))
        if not lock.acquire():
            raise AlreadyRunning(f"已有守护进程在运行（实例 {self._config.name}）")
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
        """建请求处理层，并把**本进程的接入点地址**交给它——它要在状态里报这个，猜不得。

        顺手把自己的停机通道也交过去（`Daemon` 本身满足 `StopSignal`）：处理层不能持有
        守护进程，所以只能由这一侧注入。
        """
        self._handler = self._handler_factory(self._address)
        self._handler.bind(self)

    def _mount_access_point(self) -> None:
        if not self._config.mount_endpoint:
            return
        self._access_point = AccessPoint(
            self._address, on_request=self._serve, on_input=self._serve_input
        )
        self._access_point.open()

    def _serve(self, request: object) -> None:
        """接入点来源的请求：**已经在所有者线程上**，直接处理并回写，不经队列。

        进队列会死等——队列唯一的排空方就是本线程的 `_drain_inbound`，它排在接入点
        之后才跑；一轮里投进来的条目一旦超过队列长度就再也出不去。
        """
        self._dispatch(self._handler_ready(), request)

    def _serve_input(self, key: str, data: bytes, connection: object) -> None:
        """接入点来源的上行字节——同样在所有者线程上，直接交给处理层。

        带上连接：判定要回到发送方去（下发 `INPUT_HOLD` 或断连），而这里正是唯一知道
        "字节从哪条连接来"的地方。
        """
        self._dispatch(self._handler_ready(), _Input(key, data, connection))

    def _handler_ready(self) -> RequestHandler:
        handler = self._handler
        if handler is None:  # 接入点在请求处理层建好之后才挂，正常到不了这儿
            raise NotStarted("请求处理层未装配")
        return handler

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
        handler = self._handler_ready()
        _logger.info("进入事件循环 pid=%s", os.getpid())
        self._loop_ran = True
        try:
            while not self._stop_requested.is_set():
                self._wait_for_work(handler)
                self._drain_inbound(handler)
                self._pump_handler(handler)
                self._retry_backlog()
                self._deliver(self._poll_handler(handler))
                self._resume_held(handler)
            self._shutdown(handler, self._config.stop_timeout)
        finally:
            self._stopped.set()

    def _wait_for_work(self, handler: RequestHandler) -> None:
        """这一轮的空闲等待：**被"有活"驱动**，不靠定时器。

        先等处理层的活（会话输出一到就立刻返回）；再**非阻塞**收一轮接入点（新连接 /
        连接上的请求），`tick_interval` 只剩"接入点最多隔多久被看一眼"。断掉的连接在这里
        报给处理层——写线程也会关连接，所以统一由所有者线程转达。

        被堵下的答复（`_backlog`）也靠这一圈转动来补投，最坏间隔就是 `tick_interval`。
        """
        try:
            handler.wait(self._config.tick_interval)
        except Exception:
            _logger.exception("空闲等待失败（已隔离，循环继续）")
        self._access_point_pump(handler)

    def _access_point_pump(self, handler: RequestHandler) -> None:
        """非阻塞收一轮接入点，并把它报的断连转达给处理层。"""
        if self._access_point is None:
            return
        self._access_point.pump(0.0)
        for connection in self._access_point.take_closed():
            try:
                handler.on_disconnected(connection)
            except Exception:
                _logger.exception("注销断连订阅失败（已隔离，循环继续）")

    def _drain_inbound(self, handler: RequestHandler, limit: int | None = _INBOUND_BATCH) -> None:
        """把**跨线程**投进来的请求与上行字节交给处理层。**只在所有者线程上跑。**

        `limit=None` 表示一直取到队空——收尾时用（`submit` 已 ack 的必须处理掉）。
        """
        taken = 0
        while limit is None or taken < limit:
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                return
            taken += 1
            self._dispatch(handler, item)

    def _dispatch(self, handler: RequestHandler, item: object) -> None:
        """处理一条请求或一段上行字节——两条通路（接入点直投 / 队列排空）汇到这里。"""
        if isinstance(item, _Input):
            self._on_input(handler, item)
            return
        reply = self._handle(handler, item)
        if reply is not None:
            self._deliver_one(reply)

    def _on_input(self, handler: RequestHandler, item: _Input) -> None:
        """把一段上行字节交给处理层，并照它给的动作动手。

        处理层消化 core 的判定、回一个动作；**这里只执行，不认识判定本身**（越软水位 /
        超上限都是 core 的事）。执行的是连接上的动作，所以只有接入点来源的字节才有得做。
        """
        try:
            action = handler.on_input(item.key, item.data, item.connection)
        except Exception as exc:
            # 会话可能刚被关掉；一个坏 key 不值得停下整条循环。
            _logger.warning("上行字节无人接收 key=%s: %s", item.key, exc)
            return
        self._apply_input_action(item, action)

    def _apply_input_action(self, item: _Input, action: InputAction) -> None:
        if item.connection is None or self._access_point is None:
            return  # 进程内消费者：没有连接可动
        if action is InputAction.DROP:
            _logger.warning("输入超过硬上限，断开连接 key=%s", item.key)
            self._held.pop(item.connection, None)
            self._access_point.drop(item.connection)
        elif action is InputAction.HOLD:
            # 只有**确实告诉过发送方**才记进 hold 表——没告诉它就没有要放行的人。
            if self._notice(item.connection, Notice.INPUT_HOLD, item.key):
                self._held.setdefault(item.connection, set()).add(item.key)

    def _notice(self, connection: object, notice: Notice, key: str) -> bool:
        """下发一条输入流控通知，返回它有没有进那条连接的出站队列。

        **不像答复那样必达**：连接堵住或没了就发不出去，这里只记一笔。`INPUT_RESUME`
        尤其丢不得——丢了发送方会永远本端排队，所以调用方要照返回值决定要不要下轮再试。
        """
        if self._access_point is None:
            return False
        if self._access_point.notify(connection, make_response(notice.value, key)):
            return True
        _logger.info("输入流控通知没发出去 key=%s notice=%s", key, notice.value)
        return False

    def _resume_held(self, handler: RequestHandler) -> None:
        """把已经排空的连接放行：撤销 `INPUT_HOLD`。

        每轮问一次处理层"这个 key 排空了没"——**不问就不放行**，被 hold 的发送方会一直
        本端排队。通知没发出去就留着，下一轮接着试。连接没了就顺势撤掉（没什么可放行的）。
        """
        if not self._held or self._access_point is None:
            return
        for connection, keys in list(self._held.items()):
            if not self._access_point.is_open(connection):
                del self._held[connection]
                continue
            for key in list(keys):
                try:
                    drained = handler.input_drained(key)
                except Exception:
                    _logger.exception("查询输入队列水位失败（已隔离，循环继续）")
                    continue
                if drained and self._notice(connection, Notice.INPUT_RESUME, key):
                    keys.discard(key)
            if not keys:
                del self._held[connection]

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
            return handler.poll(self._room_of)
        except Exception:
            _logger.exception("请求处理层交出答复失败（已隔离）")
            return []

    def _room_of(self, request: object) -> int | None:
        """这条请求所属连接此刻还能收多少字节；问不出来就返回 None（不限额）。

        处理层拿它当一轮的交付预算，避免"推一帧、被拒、再推"的来回。接入点不在
        （进程内消费者）时返回 None——那条路本来就不按时钟转圈。
        """
        if self._access_point is None:
            return None
        return self._access_point.room_for(request)

    def _deliver(self, replies: list[Reply]) -> None:
        """把一轮的答复依次交出去。

        **不中途停**：一批里的帧可能来自不同连接，为一条堵住就把后面的全推迟，会让别的
        订阅白挨饿。逐帧投、逐帧记账，各归各的——被拒的帧怎么处理由 `_settle` 分拣
        （处理层负责重发的，它自己留着；响应型的，守护进程攒着）。
        """
        for reply in replies:
            self._deliver_one(reply)

    def _deliver_one(self, reply: Reply) -> None:
        """把答复交回消费者：来自接入点的写回那条连接，其余走 `on_reply`。

        **先分拣、再路由、再回告、最后记账**：`owns_retransmission` 必须在 `on_reply`
        **之前**问——`on_reply` 可能把这条订阅注销掉（最后一帧收下了），那时再问就会
        误判成"响应型"，把它的答复塞进待重发表。

        每一段都各自兜住异常——它在所有者线程上跑，一次交答复失败不能炸穿循环。
        """
        try:
            mine = self._handler_ready().owns_retransmission(reply.request)
        except Exception:
            _logger.exception("询问重发归属失败（已隔离，按守护进程负责处理）")
            mine = False
        try:
            delivery = self._route(reply)
        except Exception:
            _logger.exception("投递答复失败（已隔离，循环继续）")
            delivery = Delivery.GONE
        else:
            try:
                self._handler_ready().on_reply(reply.request, delivery)
            except Exception:
                _logger.exception("回告投递结果失败（已隔离，循环继续）")
        self._settle(reply, delivery, mine)

    def _settle(self, reply: Reply, delivery: Delivery, handler_retransmits: bool) -> None:
        """按投递结果维护待重发表：堵住就攒着，收下 / 连接没了就撤掉。

        **只攒"处理层不管的那部分"**：订阅推送被拒时，处理层自己把帧留在 `out` 里下轮
        再交（见 `owns_retransmission`），守护进程再攒一份就是重复投递——同一订阅的几帧
        还共用一个请求对象，在表里会互相顶掉。响应型答复没有第二份，才由这里兜底。
        """
        if delivery is Delivery.CONGESTED and not handler_retransmits:
            self._backlog.setdefault(id(reply.request), reply)
        else:
            self._backlog.pop(id(reply.request), None)

    def _retry_backlog(self) -> None:
        """重投被堵下的答复。**在下一轮开头跑**——上一轮交完的答复这时才有机会腾出空位。

        调用顺序刻意与 `run()` 里各步一致（每轮都过一遍），所以同一连接上答复的相对顺序
        稳定：先看空位的先发，没腾出来的下一轮再来。
        """
        if not self._backlog:
            return
        for reply in list(self._backlog.values()):
            self._deliver_one(reply)

    def _route(self, reply: Reply) -> Delivery:
        """把答复交给消费者，返回投递结果：接入点在场就写回它来的那条连接，否则走 `on_reply`。"""
        if self._access_point is not None:
            delivery = self._access_point.send(reply)
            if delivery is not Delivery.NOT_MINE:
                return delivery
        if self._on_reply is None:
            _logger.warning("答复无处可去（没挂接入点，也没给 on_reply）")
            return Delivery.GONE
        self._on_reply(reply)
        return Delivery.SENT

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
        self._backlog.clear()
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
        走一遍，不能随收尾一起丢掉。被堵下的答复也在这里再给几次机会（写线程可能刚好
        腾出空位），最后交不出去就算了——收尾不该为一条堵住的连接无限期挂着。
        """
        self._drain_inbound(handler, limit=None)
        limit = min(deadline, time.monotonic() + self._config.drain_timeout)
        while (self._pending(handler) or self._backlog) and time.monotonic() < limit:
            self._pump_handler(handler)
            self._retry_backlog()
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

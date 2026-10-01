"""守护进程本体：装配、所有者循环、启动与收尾。

**它没有业务代码**：装配顺序即依赖顺序——取单实例锁 → 依赖检查 → 数据目录与日志 →
构造请求处理层 → 绑定监听器 → 写 pid 与端点 → 装信号 → 进循环，每一步都能回滚。

请求处理层是**注入**的（`RequestHandler`），所以守护进程不知道 `sid`、等待引擎、
订阅的存在。

线程账：一条接入的连接 2 个线程（读 + 写），加上所有者循环自己占的调用线程；
会话的读/写线程由请求处理层自己管。
"""

from __future__ import annotations

import itertools
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..foundation.logs import add_rotating_file, get_logger
from ..protocol.envelope import DIR_REQUEST, Envelope
from ..protocol.errors import ProtocolError
from ..protocol.frame import BytesFrame, ControlFrame
from ..protocol.messages import failed_response
from ..runtime.host_factory import check_dependencies as default_dependency_check
from ..runtime.paths import default_runtime_dir
from ..runtime.platform.signals import install_shutdown_handler
from ..runtime.platform.single_instance import SingleInstance
from ..transport import registry as transports
from ..transport.channel import decode_control
from ..transport.errors import ConnectionClosed
from ..transport.stream import Connection, Listener
from .config import DaemonConfig
from .connections import ClientConnection
from .errors import AlreadyRunning, DaemonError, NotStarted
from .handler import Reply, RequestHandler

_logger = get_logger("daemon.server")

_INBOUND_BATCH = 64
"""每轮从一条连接的入站队列取走的帧数上限（公平性）。"""
_REAP_TIMEOUT = 0.5
"""清理已断连接时等线程收工的时长；到点就放手，别挡循环。"""


class Daemon:
    """守护进程。"""

    def __init__(
        self,
        config: DaemonConfig,
        handler_factory: Callable[[], RequestHandler],
        *,
        check_dependencies: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._handler_factory = handler_factory
        self._check_dependencies = check_dependencies or default_dependency_check
        self._dir = config.runtime_dir or default_runtime_dir(config.name)

        self._lock: SingleInstance | None = None
        self._listener: Listener | None = None
        self._handler: RequestHandler | None = None
        self._connections: dict[int, ClientConnection] = {}
        self._routes: dict[str, ClientConnection] = {}
        self._next_id = itertools.count(1)
        self._stop_requested = threading.Event()
        self._started = False

    # ════════════════════════════════════════════════════════════
    # 对外状态
    # ════════════════════════════════════════════════════════════

    @property
    def address(self) -> str:
        """实际监听地址（配置里端口写 0 时，这里是内核挑的那个）。"""
        return str(self._listener.address) if self._listener is not None else self._config.listen

    @property
    def pid_path(self) -> Path:
        return self._dir / "daemon.pid"

    @property
    def endpoint_path(self) -> Path:
        """就绪后写入实际地址，客户端从这里找它。"""
        return self._dir / "endpoint"

    @property
    def running(self) -> bool:
        return self._started and not self._stop_requested.is_set()

    def request_stop(self) -> None:
        """请求停止（信号处理器与外部调用都走这里）。"""
        self._stop_requested.set()

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
            self._bind_listener()
            self._write_rendezvous()
            self._install_signals()
        except Exception:
            self._rollback()
            raise
        self._started = True
        _logger.info("守护进程就绪 pid=%s listen=%s dir=%s", os.getpid(), self.address, self._dir)

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

    def _bind_listener(self) -> None:
        self._listener = transports.listen(self._config.listen)

    def _write_rendezvous(self) -> None:
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        self.endpoint_path.write_text(self.address, encoding="utf-8")

    def _install_signals(self) -> None:
        installed = install_shutdown_handler(self._on_signal)
        if installed:
            names = ", ".join(signal.Signals(number).name for number in installed)
            _logger.info("已装信号处理器: %s", names)

    def _rollback(self) -> None:
        """逆序回滚：装了什么就撤什么。每个撤除都幂等，所以不必记账。"""
        self._remove_rendezvous()
        self._close_listener()
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

        **所有会话状态都只在这个线程上改动**：接入、摄入、应答、清理全在这里，
        所以守护进程这一层没有任何锁。
        """
        if not self._started:
            raise NotStarted("守护进程未启动")
        handler = self._handler
        if handler is None:
            raise NotStarted("请求处理层未装配")
        _logger.info("进入事件循环 listen=%s", self.address)
        while not self._stop_requested.is_set():
            self._accept_pending()
            self._drain_inbound()
            handler.pump()
            self._dispatch(handler.poll())
            self._reap()
            time.sleep(self._config.tick_interval)

    def _accept_pending(self) -> None:
        listener = self._listener
        if listener is None:
            return
        for _ in range(self._config.accept_batch):
            try:
                connection = listener.accept(timeout=0)
            except ConnectionClosed:  # 收尾途中监听点已关
                return
            if connection is None:
                return
            self._adopt(connection)

    def _adopt(self, connection: Connection) -> None:
        client = ClientConnection(
            connection,
            inbound_maxsize=self._config.inbound_maxsize,
            outbound_maxsize=self._config.outbound_maxsize,
        )
        client.start()
        self._connections[next(self._next_id)] = client
        _logger.info("客户端已接入 peer=%s", client.peer)

    def _drain_inbound(self) -> None:
        for client in list(self._connections.values()):
            for frame in client.take_inbound(_INBOUND_BATCH):
                self._on_frame(client, frame)

    def _on_frame(self, client: ClientConnection, frame: ControlFrame | BytesFrame) -> None:
        if isinstance(frame, BytesFrame):
            self._on_input(frame)
            return
        self._on_control(client, frame)

    def _on_control(self, client: ClientConnection, frame: ControlFrame) -> None:
        try:
            envelope = decode_control(frame)
        except ProtocolError as exc:
            _logger.warning("peer=%s 送来不合法信封，断开: %s", client.peer, exc)
            client.close()
            return
        if envelope.direction != DIR_REQUEST:
            _logger.warning("peer=%s 送来 %s 方向的信封，断开", client.peer, envelope.direction)
            client.close()
            return
        reply = self._handle(envelope)
        if reply is None:
            self._routes[envelope.mid] = client  # 已登记等待，稍后由 poll 交出
        elif not client.submit(reply):
            _logger.warning("响应投不进 peer=%s（客户端不读了），断开", client.peer)
            client.close()

    def _handle(self, envelope: Envelope) -> Reply | None:
        """处理一条请求；返回 `None` 表示处理层已登记等待。

        处理层抛异常不该拖垮守护进程，也不该让客户端干等——一律回一条明确失败。
        """
        handler = self._handler
        if handler is None:  # 运行期不该出现；真出现了也得给个答复
            return Reply(
                failed_response(envelope.type, envelope.mid, "InternalError", "请求处理层未装配")
            )
        try:
            return handler.handle(envelope)
        except Exception:
            _logger.exception("处理请求失败 type=%s", envelope.type)
            return Reply(
                failed_response(envelope.type, envelope.mid, "InternalError", "命令处理异常")
            )

    def _on_input(self, frame: BytesFrame) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            handler.on_input(frame.key, frame.data)
        except Exception as exc:
            # 会话可能刚被关掉；一个坏 sid 不值得断掉整条连接。
            _logger.warning("上行字节无人接收 sid=%s: %s", frame.key, exc)

    def _dispatch(self, replies: list[Reply]) -> None:
        for reply in replies:
            client = self._routes.pop(reply.envelope.mid, None)
            if client is None:
                _logger.warning("响应找不到归属 mid=%s（客户端已断开）", reply.envelope.mid)
                continue
            if not client.submit(reply):
                _logger.warning("响应投不进 peer=%s（客户端不读了），断开", client.peer)
                client.close()

    def _reap(self) -> None:
        for key, client in list(self._connections.items()):
            if not client.closed:
                continue
            del self._connections[key]
            for mid in [m for m, owner in self._routes.items() if owner is client]:
                del self._routes[mid]
            client.close()  # 幂等；确保底层 socket 也收掉，对端能立刻看到断开
            if not client.join(_REAP_TIMEOUT):
                _logger.warning("peer=%s 的线程未在 %.1fs 内收工", client.peer, _REAP_TIMEOUT)
            _logger.info("客户端已断开 peer=%s（会话不受影响）", client.peer)

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

        self._close_listener()  # 1 不再接新连接
        self._drain_inflight(deadline)  # 2 不再接新命令，在途的给一小段时间跑完
        shutdown = self._start_shutdown()  # 3 会话收尾（可能长时间阻塞，放线程里）
        clients = self._close_connections()  # 4 停各连接的读 / 写线程

        finished = self._join_everything(shutdown, clients, deadline)
        self._remove_rendezvous()  # 5 删 pid / 端点
        self._release_lock()  # 6 放锁
        self._handler = None
        self._started = False
        if finished:
            _logger.info("守护进程已停止")
        else:
            _logger.error("收尾未在 %.1fs 内完成，仍有线程在跑", budget)
        return finished

    def _drain_inflight(self, deadline: float) -> None:
        """draining：不再处理新命令，只把手上的等待跑完。

        手上还压着响应（`_routes` 非空）才等；一旦都答完就立刻往下走。
        """
        handler = self._handler
        if handler is None:
            return
        limit = min(deadline, time.monotonic() + self._config.drain_timeout)
        while self._routes and time.monotonic() < limit:
            handler.pump()
            self._dispatch(handler.poll())
            self._reap()
            time.sleep(self._config.tick_interval)

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

    def _close_connections(self) -> list[ClientConnection]:
        clients = list(self._connections.values())
        self._connections.clear()
        self._routes.clear()
        for client in clients:
            client.close()
        return clients

    def _join_everything(
        self,
        shutdown: threading.Thread | None,
        clients: list[ClientConnection],
        deadline: float,
    ) -> bool:
        finished = True
        if shutdown is not None:
            shutdown.join(max(0.0, deadline - time.monotonic()))
            if shutdown.is_alive():
                finished = False
        for client in clients:
            if not client.join(max(0.0, deadline - time.monotonic())):
                finished = False
        return finished

    def _close_listener(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _remove_rendezvous(self) -> None:
        for path in (self.pid_path, self.endpoint_path):
            path.unlink(missing_ok=True)

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

"""守护进程：生命周期与接缝。真线程、真单实例锁，但既不碰网络也不碰核心层。

请求处理层是假的——守护进程的机制只认 `RequestHandler`，它本来就不该知道 `sid`、
等待引擎这些东西，所以测它完全不需要核心层与原生扩展。也因为接缝上的报文对它不透明，
这里的请求就用普通字符串代替，不必牵扯线协议。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest

from agentic_tty.config import DaemonConfig
from agentic_tty.daemon.errors import AlreadyRunning, DaemonError, NotStarted
from agentic_tty.daemon.handler import Delivery, InputAction, Reply, RequestHandler, StopSignal
from agentic_tty.daemon.server import Daemon, SubmitOutcome
from agentic_tty.transport.pipe import PipeTransport
from agentic_tty.transport.stream import parse_address

_DEADLINE = 5.0


class FakeHandler:
    """最小请求处理层：请求与答复对它来说是普通对象。"""

    def __init__(self) -> None:
        self.inputs: list[tuple[str, bytes]] = []
        self.input_connections: list[object | None] = []
        self.input_action = InputAction.NONE
        self.drained = True
        self.stop: StopSignal | None = None
        self.pumps = 0
        self.waits = 0
        self.deliveries: list[tuple[object, Delivery]] = []
        self.disconnects: list[object] = []
        self.shutdown_called = False
        self.shutdown_block: float | None = None
        self.deferred: list[object] = []
        self.failures: list[BaseException] = []
        self.pump_boom = False
        self.poll_boom = False

    def bind(self, stop: StopSignal) -> None:
        self.stop = stop

    def handle(self, request: object) -> Reply | None:
        if request == "defer":
            self.deferred.append(request)
            return None
        if request == "boom":
            raise RuntimeError("故意炸")
        return Reply(request=request, answer=f"answer:{request}")

    def poll(self, room_of: Callable[[object], int | None]) -> list[Reply]:
        if self.poll_boom:
            raise RuntimeError("poll 故意炸")
        replies = [Reply(request=item, answer=f"deferred:{item}") for item in self.deferred]
        self.deferred.clear()
        return replies

    def pending(self) -> int:
        return len(self.deferred)

    def failure(self, request: object, error: BaseException) -> Reply:
        self.failures.append(error)
        return Reply(request=request, answer=f"failed:{request}")

    def on_input(self, key: str, data: bytes, connection: object) -> InputAction:
        self.inputs.append((key, data))
        self.input_connections.append(connection)
        return self.input_action

    def input_drained(self, key: str) -> bool:
        return self.drained

    def pump(self) -> None:
        self.pumps += 1
        if self.pump_boom:
            raise RuntimeError("pump 故意炸")

    def wait(self, timeout: float) -> None:
        self.waits += 1
        time.sleep(timeout)

    def on_reply(self, request: object, delivery: Delivery) -> None:
        self.deliveries.append((request, delivery))

    def owns_retransmission(self, request: object) -> bool:
        return False

    def on_disconnected(self, connection: object) -> None:
        self.disconnects.append(connection)

    def shutdown(self) -> None:
        if self.shutdown_block is not None:
            time.sleep(self.shutdown_block)
        self.shutdown_called = True


def make_daemon(
    tmp_path,
    handler: FakeHandler | None = None,
    **overrides: object,
) -> tuple[Daemon, FakeHandler, list[Reply]]:
    """装一个守护进程；答复收进列表，测试直接检查它。"""
    handler = handler or FakeHandler()
    replies: list[Reply] = []
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        directory=tmp_path / "run",
        write_log_file=False,
        tick_interval=0.001,
        **overrides,
    )
    daemon = Daemon(
        config,
        lambda _endpoint: handler,
        on_reply=replies.append,
    )
    return daemon, handler, replies


@contextlib.contextmanager
def running(
    tmp_path, handler: FakeHandler | None = None, **overrides: object
) -> Iterator[tuple[Daemon, FakeHandler, list[Reply]]]:
    """跑起来（后台线程）再交出去。"""
    daemon, handler, replies = make_daemon(tmp_path, handler, **overrides)
    daemon.start()
    thread = threading.Thread(target=daemon.run, name="daemon-run", daemon=True)
    thread.start()
    try:
        yield daemon, handler, replies
    finally:
        daemon.request_stop()
        thread.join(_DEADLINE)
        daemon.stop(2)


def wait_for(predicate: Callable[[], bool], timeout: float = _DEADLINE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _fake(_endpoint: str) -> FakeHandler:
    """工厂：守护进程把接入点地址交给它，假处理层用不上这个。"""
    return FakeHandler()


def test_fake_handler_satisfies_the_seam():
    assert isinstance(FakeHandler(), RequestHandler)


def test_address_is_empty_when_no_access_point_is_mounted(tmp_path):
    """不挂接入点（进程内嵌入 / 单测）时没有地址——由 `Daemon` 一处算，别处不预测。"""
    daemon, _handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    assert daemon.address == ""


def test_second_daemon_on_the_same_lock_is_rejected(tmp_path):
    """单实例是硬要求：两个守护进程会各自维护互斥的会话坐标。

    用同一份配置（同 directory、同 name）——Windows 上锁名是全局命名空间、Linux 上
    是 directory 里的锁文件，两边都靠"同一份配置"才能都撞上。
    """
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        directory=tmp_path / "run",
        write_log_file=False,
    )
    first = Daemon(config, _fake, on_reply=lambda _r: None)
    first.start()
    try:
        other = Daemon(config, _fake, on_reply=lambda _r: None)
        with pytest.raises(AlreadyRunning):
            other.start()
        assert not other.running
    finally:
        first.stop()


def test_start_failure_rolls_back_the_lock(tmp_path):
    """装配中途失败要能把已取的锁放掉，否则之后永远起不来。"""
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        directory=tmp_path / "run",
        write_log_file=False,
    )

    def boom(_endpoint: str) -> FakeHandler:
        raise RuntimeError("处理层装配失败")

    broken = Daemon(config, boom, on_reply=lambda _r: None)
    with pytest.raises(RuntimeError):
        broken.start()
    assert not broken.running

    healthy = Daemon(config, _fake, on_reply=lambda _r: None)
    healthy.start()
    try:
        assert healthy.running
    finally:
        healthy.stop()


def test_run_without_start_is_rejected(tmp_path):
    daemon, _handler, _replies = make_daemon(tmp_path)
    with pytest.raises(NotStarted):
        daemon.run()


def test_starting_twice_is_rejected(tmp_path):
    with running(tmp_path) as (daemon, _handler, _replies):
        with pytest.raises(DaemonError, match="已启动"):
            daemon.start()


def test_run_pumps_the_handler(tmp_path):
    with running(tmp_path) as (_daemon, handler, _replies):
        assert wait_for(lambda: handler.pumps >= 3)


def test_run_waits_on_the_handler(tmp_path):
    """空闲等待交给处理层——循环由"有活"驱动，不自己定时睡。"""
    with running(tmp_path) as (_daemon, handler, _replies):
        assert wait_for(lambda: handler.waits >= 3)


def test_submit_gets_an_answer_that_carries_the_request(tmp_path):
    """投进去的请求要原样出现在答复里——延迟答复就靠这个身份找回归属。"""
    with running(tmp_path) as (daemon, _handler, replies):
        assert daemon.submit("ping") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert replies[0].request == "ping"
        assert replies[0].answer == "answer:ping"


def test_deferred_answer_arrives_with_its_original_request(tmp_path):
    """处理层返回 None 表示登记等待；稍后 poll 交出的答复必须带回同一个请求对象。"""
    with running(tmp_path) as (daemon, _handler, replies):
        assert daemon.submit("defer") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert replies[0].request == "defer"
        assert replies[0].answer == "deferred:defer"


def test_submit_input_reaches_the_handler(tmp_path):
    with running(tmp_path) as (daemon, handler, _replies):
        assert daemon.submit_input("sid-1", b"hello\n") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: handler.inputs == [("sid-1", b"hello\n")])


def test_delivery_result_is_reported_to_the_handler(tmp_path):
    """投递结果要回告处理层——订阅推送的节奏（没被收下就别再推）建立在这上面。"""
    with running(tmp_path) as (daemon, handler, replies):
        assert daemon.submit("ping") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert handler.deliveries == [("ping", Delivery.SENT)]


def test_a_dropped_connection_is_reported_to_the_handler(tmp_path):
    """连接断了要报给处理层——它靠这个注销挂在上面的订阅。"""
    with running(tmp_path) as (daemon, handler, _replies):
        connection = PipeTransport().connect(parse_address(daemon.address), timeout=_DEADLINE)
        assert wait_for(lambda: daemon._access_point.connection_count == 1)
        connection.close()
        assert wait_for(lambda: bool(handler.disconnects))


def test_handler_crash_becomes_a_failure_answer(tmp_path):
    """处理层抛异常不该让消费者干等——转成一条明确的失败答复，失败长什么样归它自己。"""
    with running(tmp_path) as (daemon, handler, replies):
        assert daemon.submit("boom") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert replies[0].answer == "failed:boom"
        assert len(handler.failures) == 1


def test_handler_pump_crash_does_not_kill_the_loop(tmp_path):
    """一次推进异常不能杀死所有者循环：守护进程还得能继续应答。"""
    handler = FakeHandler()
    with running(tmp_path, handler) as (daemon, _handler, replies):
        handler.pump_boom = True
        assert daemon.submit("ping") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert replies[0].answer == "answer:ping"


def test_handler_poll_crash_does_not_kill_the_loop(tmp_path):
    handler = FakeHandler()
    with running(tmp_path, handler) as (daemon, _handler, replies):
        handler.poll_boom = True
        assert daemon.submit("ping") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies))
        assert replies[0].answer == "answer:ping"


def test_stop_shuts_the_handler_down(tmp_path):
    with running(tmp_path) as (daemon, handler, _replies):
        assert daemon.running
    assert handler.shutdown_called
    assert not daemon.running


def test_draining_waits_for_pending_requests(tmp_path):
    """收尾给在途等待一小段时间跑完：手上压着等待时先把答复交出来。"""
    with running(tmp_path) as (daemon, _handler, replies):
        assert daemon.submit("defer") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(replies) or _handler.pending() == 1)
        daemon.stop(2)
        assert any(reply.answer == "deferred:defer" for reply in replies)


def test_stop_reports_timeout_when_shutdown_hangs(tmp_path):
    """会话收尾可能长时间阻塞；stop 必须带超时返回，把强制退出留给入口。"""
    handler = FakeHandler()
    handler.shutdown_block = 1.0
    with running(tmp_path, handler) as (daemon, _handler, _replies):
        started = time.monotonic()
        finished = daemon.stop(0.5)
        assert finished is False
        assert time.monotonic() - started < 3.0
        assert daemon.running is False


def test_stop_is_idempotent(tmp_path):
    with running(tmp_path) as (daemon, _handler, _replies):
        assert daemon.stop(1) is True
        assert daemon.stop(1) is True


def test_daemon_can_be_restarted_after_stop(tmp_path):
    """`stop()` 之后 `start()` 必须能再跑起来——停止标志不复位的话 `run()` 会一进去就退出。"""
    daemon, handler, _replies = make_daemon(tmp_path)
    daemon.start()
    first = threading.Thread(target=daemon.run, name="daemon-run-1", daemon=True)
    first.start()
    try:
        assert wait_for(lambda: handler.pumps >= 2)
        daemon.request_stop()
        first.join(_DEADLINE)
        assert daemon.stop(2) is True
    finally:
        daemon.request_stop()
        first.join(_DEADLINE)

    daemon.start()
    second = threading.Thread(target=daemon.run, name="daemon-run-2", daemon=True)
    second.start()
    try:
        assert daemon.running
        before = handler.pumps
        assert wait_for(lambda: handler.pumps > before + 2), "重启后循环没在跑"
    finally:
        daemon.request_stop()
        second.join(_DEADLINE)
        daemon.stop(2)


def test_shutdown_drains_requests_that_were_already_acked(tmp_path):
    """`submit` 返回 DELIVERED 就是承诺会处理——收尾时队列里已进的不能丢。"""
    daemon, handler, replies = make_daemon(tmp_path)
    daemon.start()
    # 不起 run()：请求先压进队列，随后由 stop() 在"所有者线程"上排空。
    assert daemon.submit("ping") is SubmitOutcome.DELIVERED
    assert daemon.stop(2) is True
    assert any(reply.answer == "answer:ping" for reply in replies)
    assert handler.shutdown_called


def test_handler_is_bound_to_the_daemon_stop_signal(tmp_path):
    """处理层不能持有 `Daemon`，所以停机通道必须由守护进程注入——这就是那条通道。"""
    with running(tmp_path) as (daemon, handler, _replies):
        assert handler.stop is daemon


# ════════════════════════════════════════════════════════════════
# 答复必达：`CONGESTED` 由守护进程重投，不丢
# ════════════════════════════════════════════════════════════════


def test_congested_goes_into_the_backlog_and_leaves_on_success(tmp_path):
    """单元：`_settle` 把 `CONGESTED` 攒下、`_retry_backlog` 成功后再撤掉。"""
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    try:
        reply = handler.handle("ping")
        assert reply is not None

        daemon._route = lambda _reply: Delivery.CONGESTED
        daemon._deliver_one(reply)
        assert daemon._backlog, "`CONGESTED` 的答复被丢了，没有攒进待重发表"

        daemon._route = lambda _reply: Delivery.SENT
        daemon._retry_backlog()
        assert not daemon._backlog, "重投成功后还没清掉"
        assert handler.deliveries[-1][1] is Delivery.SENT
    finally:
        daemon.stop(2)


def test_the_delivery_result_comes_from_routing_not_from_the_handler(tmp_path):
    """投递结果取的是**路由**的返回，不能取成 `on_reply` 的返回。

    `RequestHandler.on_reply` 按契约无返回；若把它的返回值当投递结果，永远拿到 `None`，
    于是"堵住要不要重发"这件事整个失效——答复会被静默丢掉而没有任何迹象。
    """
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    try:
        reply = handler.handle("ping")
        assert reply is not None

        daemon._route = lambda _reply: Delivery.CONGESTED
        daemon._deliver_one(reply)
        assert handler.deliveries == [(reply.request, Delivery.CONGESTED)]
        assert daemon._backlog, "投递结果没传到 `_settle`"
    finally:
        daemon.stop(2)


def test_backlog_is_cleared_even_if_the_handler_raises_on_reply(tmp_path):
    """回告抛异常不影响记账：答复**已经发出**，不能因为回告失败就留在待重发表里。

    留在表里会下一轮重复投递——客户端收到两遍同一帧。
    """
    handler = FakeHandler()
    daemon, _handler, _replies = make_daemon(tmp_path, handler, mount_endpoint=False)
    daemon.start()
    try:
        # 先让答复进 backlog
        daemon._route = lambda _reply: Delivery.CONGESTED
        reply = handler.handle("ping")
        assert reply is not None
        daemon._deliver_one(reply)
        assert daemon._backlog

        # 重投成功（SENT），但回告炸了
        daemon._route = lambda _reply: Delivery.SENT
        handler.on_reply = lambda *_a: (_ for _ in ()).throw(RuntimeError("回告炸了"))
        daemon._retry_backlog()
        assert not daemon._backlog, "回告失败却把已发出的答复留在了待重发表里"
    finally:
        daemon.stop(2)


def test_a_dropped_connection_frees_the_backlog_slot(tmp_path):
    """连接没了（`GONE`）就该撤掉待重发，不该永远攥着一条死连接的答复。"""
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    try:
        reply = handler.handle("ping")
        assert reply is not None

        daemon._route = lambda _reply: Delivery.CONGESTED
        daemon._deliver_one(reply)
        assert daemon._backlog

        daemon._route = lambda _reply: Delivery.GONE
        daemon._retry_backlog()
        assert not daemon._backlog
    finally:
        daemon.stop(2)


def test_backlog_is_reset_on_restart(tmp_path):
    """上一轮的待重发不能带进下一轮——那批答复属于已经关掉的连接。"""
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    daemon._route = lambda _reply: Delivery.CONGESTED
    reply = handler.handle("ping")
    assert reply is not None
    daemon._deliver_one(reply)
    assert daemon._backlog
    daemon.stop(2)

    daemon.start()
    try:
        assert not daemon._backlog
    finally:
        daemon.stop(2)


def test_the_running_loop_actually_retries_a_congested_answer(tmp_path):
    """整条链路：答复被堵 → 攒下 → 循环下一轮补投 → 交到消费者手里。

    上面那些用 `_deliver_one` / `_retry_backlog` 直接验语义；这一条验循环里**真的调了**
    补投——否则答复照样丢，只是丢在了"没人调用重试"这一步。
    """
    handler = FakeHandler()
    blocked = {"on": True}
    real_route = Daemon._route

    def route(reply: Reply) -> Delivery:
        if blocked["on"]:
            return Delivery.CONGESTED
        return real_route(daemon, reply)

    with running(tmp_path, handler) as (daemon, _handler, replies):
        daemon._route = route
        assert daemon.submit("ping") is SubmitOutcome.DELIVERED
        assert wait_for(lambda: bool(daemon._backlog)), "被堵的答复没有攒下来"

        blocked["on"] = False  # 连接腾出空位
        assert wait_for(lambda: bool(replies)), "循环没有把攒下的答复补投出去"
        assert replies[0].answer == "answer:ping"
        assert not daemon._backlog


def test_a_batch_is_delivered_in_full_even_if_one_is_refused(tmp_path):
    """一批里有一条被拒，**后面的照投**——不能为一条堵住的连接推迟别的订阅。

    被拒的那条自己进待重发表（响应型）；它之后的照常投递、照常回告。
    """
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    try:
        batch = [handler.handle(f"req{n}") for n in range(4)]
        assert all(reply is not None for reply in batch)

        seen: list[str] = []

        def route(reply: Reply) -> Delivery:
            seen.append(reply.answer)
            return Delivery.CONGESTED if reply.answer == "answer:req1" else Delivery.SENT

        daemon._route = route
        daemon._deliver(batch)

        assert seen == [f"answer:req{n}" for n in range(4)], "被拒之后就停手了"
        assert [request for request, _ in handler.deliveries] == [r.request for r in batch]
        assert list(daemon._backlog.values()) == [batch[1]], "被拒的那条没进待重发表"
    finally:
        daemon.stop(2)


def test_a_refused_push_frame_is_not_backlogged_by_the_daemon(tmp_path):
    """处理层自己负责重发的请求被拒时，**守护进程不攒**——攒了就重复投递。

    同一条订阅的几帧共用一个请求对象，在待重发表里还会互相顶掉。
    """
    daemon, handler, _replies = make_daemon(tmp_path, mount_endpoint=False)
    daemon.start()
    try:
        handler.owns_retransmission = lambda _request: True  # 这一条归处理层
        reply = handler.handle("push")
        assert reply is not None

        daemon._route = lambda _reply: Delivery.CONGESTED
        daemon._deliver_one(reply)

        assert not daemon._backlog, "处理层负责重发的答复被守护进程也攒了一份"
    finally:
        daemon.stop(2)


def test_stop_signal_from_the_handler_ends_the_loop(tmp_path):
    """处理层调 `request_stop()` 就等于守护进程自己收尾：循环退出、shutdown 跑过。"""
    handler = FakeHandler()
    with running(tmp_path, handler) as (daemon, _handler, _replies):
        assert daemon.running
        handler.stop.request_stop()
        assert wait_for(lambda: not daemon.running)
    assert handler.shutdown_called

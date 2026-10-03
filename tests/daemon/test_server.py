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
from agentic_tty.daemon.handler import Reply, RequestHandler
from agentic_tty.daemon.server import Daemon, SubmitOutcome

_DEADLINE = 5.0


class FakeHandler:
    """最小请求处理层：请求与答复对它来说是普通对象。"""

    def __init__(self) -> None:
        self.inputs: list[tuple[str, bytes]] = []
        self.pumps = 0
        self.shutdown_called = False
        self.shutdown_block: float | None = None
        self.deferred: list[object] = []
        self.failures: list[BaseException] = []
        self.pump_boom = False
        self.poll_boom = False

    def handle(self, request: object) -> Reply | None:
        if request == "defer":
            self.deferred.append(request)
            return None
        if request == "boom":
            raise RuntimeError("故意炸")
        return Reply(request=request, answer=f"answer:{request}")

    def poll(self) -> list[Reply]:
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

    def on_input(self, key: str, data: bytes) -> None:
        self.inputs.append((key, data))

    def pump(self) -> None:
        self.pumps += 1
        if self.pump_boom:
            raise RuntimeError("pump 故意炸")

    def shutdown(self) -> None:
        if self.shutdown_block is not None:
            time.sleep(self.shutdown_block)
        self.shutdown_called = True


def make_daemon(
    tmp_path,
    handler: FakeHandler | None = None,
    *,
    check: Callable[[], None] | None = None,
    **overrides: object,
) -> tuple[Daemon, FakeHandler, list[Reply]]:
    """装一个守护进程；答复收进列表，测试直接检查它。"""
    handler = handler or FakeHandler()
    replies: list[Reply] = []
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
        tick_interval=0.001,
        **overrides,
    )
    daemon = Daemon(
        config,
        lambda: handler,
        on_reply=replies.append,
        check_dependencies=check or (lambda: None),
    )
    return daemon, handler, replies


@contextlib.contextmanager
def running(tmp_path, handler: FakeHandler | None = None, **overrides: object) -> Iterator[
    tuple[Daemon, FakeHandler, list[Reply]]
]:
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


def test_fake_handler_satisfies_the_seam():
    assert isinstance(FakeHandler(), RequestHandler)


def test_second_daemon_on_the_same_lock_is_rejected(tmp_path):
    """单实例是硬要求：两个守护进程会各自维护互斥的会话坐标。

    用同一份配置（同 runtime_dir、同 name）——Windows 上锁名是全局命名空间、Linux 上
    是 runtime_dir 里的锁文件，两边都靠"同一份配置"才能都撞上。
    """
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
    )
    first = Daemon(config, FakeHandler, on_reply=lambda _r: None, check_dependencies=lambda: None)
    first.start()
    try:
        other = Daemon(
            config, FakeHandler, on_reply=lambda _r: None, check_dependencies=lambda: None
        )
        with pytest.raises(AlreadyRunning):
            other.start()
        assert not other.running
    finally:
        first.stop()


def test_start_failure_rolls_back_the_lock(tmp_path):
    """依赖检查失败要能把已取的锁放掉，否则之后永远起不来。"""
    config = DaemonConfig(
        name=f"agentic-tty-test-{uuid4().hex[:8]}",
        runtime_dir=tmp_path / "run",
        write_log_file=False,
    )

    def boom() -> None:
        raise RuntimeError("缺原生扩展")

    broken = Daemon(
        config, FakeHandler, on_reply=lambda _r: None, check_dependencies=boom
    )
    with pytest.raises(RuntimeError):
        broken.start()
    assert not broken.running

    healthy = Daemon(
        config, FakeHandler, on_reply=lambda _r: None, check_dependencies=lambda: None
    )
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

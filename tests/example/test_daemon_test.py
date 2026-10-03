"""守护进程验证台的端到端：真守护进程 ＋ 真接入点 ＋ 真协议客户端。

请求处理层用的是**守护进程自带的默认那份**（`daemon/kernel.py`），它**真的驱动 core**——
所以这条链上没有一个替身：`pipe://` 监听、解帧、core 操作、答复回写全是真的。界面
（`gui.py`）不在这里测，它只负责把答复翻译成界面状态。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from queue import Empty, Queue
from uuid import uuid4

import pytest

from agentic_tty.config import DaemonConfig
from agentic_tty.daemon.kernel import KernelHandler
from agentic_tty.daemon.server import Daemon
from agentic_tty.example.daemon_test import address
from agentic_tty.example.daemon_test.client import Answer, Client
from agentic_tty.protocol.contracts.daemon_ipc import Command, SessionRef
from agentic_tty.protocol.response import data_of, error_of, is_ok

_DEADLINE = 15.0


@pytest.fixture
def running(tmp_path, monkeypatch) -> Iterator[str]:
    """起一个真守护进程（挂接入点），跑完收尾。

    把平台默认目录指到 `tmp_path`：**两端都按同一套命名算**（`config.constants.runtime_dir`），
    所以测试不往用户的目录里写东西。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    name = f"test-{uuid4().hex[:8]}"
    config = DaemonConfig(name=name, write_log_file=False, listen=name, tick_interval=0.001)
    daemon = Daemon(
        config,
        lambda: KernelHandler(listen=address(name)),
        check_dependencies=lambda: None,  # 测子进程会话，不必碰原生扩展
    )
    daemon.start()
    thread = threading.Thread(target=daemon.run, name="daemon-run", daemon=True)
    thread.start()
    try:
        yield name
    finally:
        daemon.request_stop()
        thread.join(_DEADLINE)
        daemon.stop(2)


class _Session:
    """一个连上守护进程的客户端，外加"按 `mid` 等答复"的小工具。"""

    def __init__(self, address: str) -> None:
        self.answers: Queue[Answer] = Queue()
        self.client = Client(address, on_reply=self.answers.put)

    def __enter__(self) -> _Session:
        self.client.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.client.close()

    def ask(self, command: str, op: dict | None = None) -> Answer:
        mid = self.client.request(command, op)
        deadline = time.monotonic() + _DEADLINE
        while time.monotonic() < deadline:
            try:
                answer = self.answers.get(timeout=0.05)
            except Empty:
                continue
            if answer.mid == mid:
                return answer
        raise AssertionError(f"没等到 {command} 的答复")

    def data(self, command: str, op: dict | None = None) -> dict:
        answer = self.ask(command, op)
        assert answer.envelope is not None and is_ok(answer.envelope), answer
        return data_of(answer.envelope)

    def create(self, *argv: str) -> SessionRef:
        op = {"mode": "subprocess", "argv": list(argv)}
        return SessionRef.from_dict(self.data(Command.CREATE_SESSION, op)["session"])


def test_status_reports_the_endpoint(running: tuple[str, DaemonConfig]):
    name = running
    with _Session(address(name)) as session:
        data = session.data(Command.DAEMON_STATUS)
        assert data["listen"] == address(name)
        assert data["sessions"] == 0


def test_create_list_close(running: tuple[str, DaemonConfig]):
    name = running
    with _Session(address(name)) as session:
        ref = session.create(sys.executable, "-c", "pass")
        assert ref.mode == "subprocess"
        assert ref.command == sys.executable

        listed = session.data(Command.LIST_SESSIONS)["sessions"]
        assert [row["uid"] for row in listed] == [ref.uid]

        session.data(Command.CLOSE_SESSION, {"uid": ref.uid})
        assert session.data(Command.LIST_SESSIONS)["sessions"] == []


def test_input_bytes_and_byte_answer_stay_off_json(running: tuple[str, DaemonConfig]):
    """上行是字节帧，下行字节答复也是字节帧——两头都不进 JSON。"""
    name = running
    with _Session(address(name)) as session:
        program = "import sys; print(sys.stdin.readline().strip().upper(), flush=True)"
        ref = session.create(sys.executable, "-u", "-c", program)
        session.client.write(ref.uid, b"hello\n")

        seen = b""
        deadline = time.monotonic() + _DEADLINE
        while time.monotonic() < deadline:
            answer = session.ask(Command.READ_SESSION, {"uid": ref.uid, "mode": "bytes"})
            assert answer.chunk is not None
            seen = answer.chunk.data
            if b"HELLO" in seen:
                break
        assert b"HELLO" in seen, seen


def test_unknown_command_comes_back_as_a_failed_answer(running: tuple[str, DaemonConfig]):
    """守护进程不认识的命令要明确报错，不能让客户端干等。"""
    name = running
    with _Session(address(name)) as session:
        answer = session.ask("nonsense")
        assert answer.envelope is not None
        assert not is_ok(answer.envelope)
        failure = error_of(answer.envelope)
        assert failure is not None and failure.code == "ValueError"


def _pump(root, predicate, timeout: float = 20.0) -> bool:
    """跑 Tk 的事件循环直到条件成立——`update()` 会把到点的 `after` 回调也跑掉。

    预算给得宽一点：整轮测试里别的用例在起真 PTY、真进程，满载时这条会慢下来。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_gui_turns_answers_into_widget_state(running: tuple[str, DaemonConfig], root):
    """界面把答复翻译对了没有：构造、跑几轮 tick、看状态与树有没有东西。

    控件 API 用错是 ruff 与 import 都查不出的那类错，所以这里真建一次界面。
    """
    pytest.importorskip("resvg_py")
    name = running
    from agentic_tty.example.daemon_test.gui import App  # 拉 tkinter + resvg，按需导入

    app = App(root, name)
    try:
        assert _pump(root, lambda: app._connected), "界面没连上守护进程"

        app._bar.mode.set("subprocess")  # 命令留空 = 平台默认 shell
        app._create_session()
        assert _pump(root, lambda: bool(app._sessions)), "树里没出现会话"
    finally:
        app.on_close()

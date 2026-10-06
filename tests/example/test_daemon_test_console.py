"""守护进程验证台的端到端：真守护进程 ＋ 真接入点 ＋ 真协议客户端。

请求处理层用的是**守护进程自带的默认那份**（`daemon/kernel.py`），它**真的驱动 core**——
所以这条链上没有一个替身：`pipe://` 监听、解帧、core 操作、答复回写全是真的。界面
（`gui.py`）不在这里测，它只负责把答复翻译成界面状态。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from queue import Empty, Queue
from uuid import uuid4

import pytest

from agentic_tty.config import DaemonConfig
from agentic_tty.daemon.assembly import build
from agentic_tty.example.daemon_test_console import address
from agentic_tty.example.daemon_test_console.client import Answer, Client
from agentic_tty.protocol.contracts.daemon_ipc import Command, Event, SessionRef
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
    # 走真装配（`assembly.build`）：这条链上连请求处理层都是默认那一份，没有替身
    daemon = build(DaemonConfig(name=name, write_log_file=False, tick_interval=0.001))
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

    def ask(self, command: str, op: dict | None = None, timeout: float = _DEADLINE) -> Answer:
        mid = self.client.request(command, op)
        deadline = time.monotonic() + timeout
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

    def drain(self, mid: str, until: str, timeout: float = _DEADLINE) -> list[Answer]:
        """收 `mid` 上的所有答复，直到出现 `until` 类型的控制帧。

        订阅的推送与建立订阅那条请求**共用同一个 `mid`**，所以只能这样一路收到结束帧。
        """
        seen: list[Answer] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                answer = self.answers.get(timeout=0.05)
            except Empty:
                continue
            if answer.mid != mid:
                continue
            seen.append(answer)
            if answer.envelope is not None and answer.envelope.type == until:
                return seen
        raise AssertionError(f"没等到 {until}（已收 {len(seen)} 帧）")

    def create(self, *argv: str) -> SessionRef:
        op = {"mode": "subprocess", "argv": list(argv)}
        return SessionRef.from_dict(self.data(Command.CREATE_SESSION, op)["session"])


def test_status_reports_the_endpoint(running: tuple[str, DaemonConfig]):
    name = running
    with _Session(address(name)) as session:
        data = session.data(Command.DAEMON_STATUS)
        assert data["endpoint"] == address(name)
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
    """守护进程不认识的命令要明确报错，不能让客户端干等。

    错误码是 `MessageError`——对端送错了东西属可预期结果，与解消息体字段同一类。
    """
    name = running
    with _Session(address(name)) as session:
        answer = session.ask("nonsense")
        assert answer.envelope is not None
        assert not is_ok(answer.envelope)
        failure = error_of(answer.envelope)
        assert failure is not None and failure.code == "MessageError"


def test_subscribe_pushes_bytes_then_ends(running: tuple[str, DaemonConfig]):
    """订阅：先 ack，再按游标推字节帧（`key` = 订阅 id），会话排空后收到 `ended`。"""
    name = running
    with _Session(address(name)) as session:
        ref = session.create(sys.executable, "-u", "-c", "print('hello', flush=True)")
        mid = session.client.request(Command.SUBSCRIBE, {"uid": ref.uid, "stream": "stdout"})
        answers = session.drain(mid, Event.ENDED)

        ack = answers[0]
        assert ack.envelope is not None and is_ok(ack.envelope)
        assert data_of(ack.envelope)["sub_id"] == mid

        blob = b"".join(answer.chunk.data for answer in answers if answer.chunk is not None)
        assert b"hello" in blob, blob


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
    from agentic_tty.example.daemon_test_console.gui import App  # 拉 tkinter + resvg，按需导入

    app = App(root, name)
    try:
        assert _pump(root, lambda: app._connected), "界面没连上守护进程"

        app._bar.mode.set("subprocess")  # 命令留空 = 平台默认 shell
        app._create_session()
        assert _pump(root, lambda: bool(app._sessions)), "树里没出现会话"
    finally:
        app.on_close()


def test_gui_reports_a_failed_subscribe(running: tuple[str, DaemonConfig], root):
    """订阅失败要在页里明说——它和推送共用同一个 `mid`，不能抛在驱动循环里被吞掉。"""
    pytest.importorskip("resvg_py")
    from agentic_tty.example.daemon_test_console.gui import App
    from agentic_tty.example.ui import Page

    app = App(root, running)
    try:
        assert _pump(root, lambda: app._connected), "界面没连上守护进程"

        app._selected = "no-such-uid"  # 摆一个不存在的会话：订阅必失败
        app._subscribe_selected()

        def page_text() -> str:
            return app._tabs.text(Page.SUB).get("1.0", "end")

        assert _pump(root, lambda: "订阅失败" in page_text()), page_text()
    finally:
        app.on_close()


def test_create_session_runs_in_the_requested_directory(running, tmp_path):
    """请求里的 `cwd` 真的落到子进程上——不是记下来就算。"""
    program = "import os; print(os.getcwd(), flush=True)"
    with _Session(address(running)) as session:
        created = session.data(
            Command.CREATE_SESSION,
            {
                "mode": "subprocess",
                "argv": [sys.executable, "-u", "-c", program],
                "cwd": str(tmp_path),
            },
        )
        uid = SessionRef.from_dict(created["session"]).uid
        seen = b""
        deadline = time.monotonic() + _DEADLINE
        while time.monotonic() < deadline:
            answer = session.ask(Command.READ_SESSION, {"uid": uid, "mode": "bytes"})
            assert answer.chunk is not None
            seen = answer.chunk.data
            if seen.strip():
                break

    printed = seen.decode(errors="replace").strip()
    assert printed, "子进程没有打印工作目录"
    assert os.path.samefile(printed, tmp_path), printed


def test_gui_sends_the_working_directory(running, root, tmp_path, monkeypatch):
    """台子填了工作目录就带上它；留空则不带（让守护进程用自己启动时那个目录）。"""
    pytest.importorskip("resvg_py")
    from agentic_tty.example.daemon_test_console.gui import App  # 拉 tkinter + resvg，按需导入

    app = App(root, running)
    sent: list[dict] = []
    try:
        monkeypatch.setattr(
            app._client, "request", lambda command, op=None: (sent.append(op), "mid")[1]
        )
        app._dir.set_directory(str(tmp_path))
        app._create_session()
        app._dir.set_directory(None)
        app._create_session()
    finally:
        app.on_close()

    assert sent == [
        {"mode": "pty", "argv": [], "cwd": str(tmp_path)},
        {"mode": "pty", "argv": []},
    ]


def test_read_image_over_the_wire(running):
    """跨进程取位图：宿主直接出，客户端拿到字节帧（PNG 头）。

    第一次要初始化渲染器（实测 7–10 s），所以这条的等待窗口比别处宽。
    """
    with _Session(address(running)) as session:
        created = session.data(Command.CREATE_SESSION, {"mode": "pty", "argv": []})
        ref = SessionRef.from_dict(created["session"])
        answer = session.ask(
            Command.READ_SESSION,
            {"uid": ref.uid, "mode": "image", "scale": 1.0},
            timeout=40.0,
        )

    assert answer.chunk is not None
    assert answer.chunk.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_gui_requests_a_bitmap_in_image_format(running, root):
    """「屏幕」页切到 `image`：位图经一次请求往返拿回来，铺到画布上。"""
    pytest.importorskip("resvg_py")
    from agentic_tty.example.daemon_test_console.gui import App
    from agentic_tty.example.ui import FORMAT_IMAGE, Page

    app = App(root, running)
    try:
        assert _pump(root, lambda: app._connected), "界面没连上守护进程"
        app._bar.mode.set("pty")
        app._create_session()
        assert _pump(root, lambda: bool(app._sessions)), "树里没出现会话"

        app._tabs.show_page(Page.SCREEN)
        app._screen.format = FORMAT_IMAGE

        def painted() -> bool:
            return any(app._screen.canvas.type(i) == "image" for i in app._screen.canvas.find_all())

        assert _pump(root, painted, timeout=40.0), "画布上没出现位图"
    finally:
        app.on_close()

"""端到端：客户端 → 守护进程 → 核心层。

真的起一个守护进程、真的走 loopback；请求处理层用演示替身。

命令一律用 `python -c "print(...)"` 这种**输出确定**的东西——比拿真 shell 试提示符时序
稳得多，也不用等"提示符什么时候出现"。
"""

from __future__ import annotations

import sys
import time

from agentic_tty.protocol.messages import data_of, error_of

from .wire import DEADLINE, WireClient


def test_status_reports_a_live_daemon(demo_daemon):
    client = WireClient(demo_daemon.address)
    try:
        data = data_of(client.request("get_daemon_status"))
        assert data["pid"] > 0
        assert data["sessions"] == 0
        assert data["listen"] == demo_daemon.address
    finally:
        client.close()


def test_endpoint_file_points_at_the_daemon(demo_daemon):
    """客户端就是靠这个文件找到守护进程的——端口写 0 时只有守护进程自己知道。"""
    assert demo_daemon.endpoint_path.read_text(encoding="utf-8") == demo_daemon.address


def test_terminal_runs_to_completion_and_we_read_its_bytes(demo_daemon):
    """一整条链路：建会话 → 等它结束 → 把字节流捞回来。"""
    client = WireClient(demo_daemon.address)
    try:
        client.request(
            "create_terminal", sid="job", command=[sys.executable, "-c", "print('marker-7f3a')"]
        )
        reply = client.request_with(
            "read_terminal",
            condition={"ended": True, "timeout": float(DEADLINE)},
            sid="job",
            mode="bytes",
        )
        data = data_of(reply)
        assert data["wait"]["reason"] == "ended"
        assert data["wait"]["exit_code"] == 0
        frame = client.wait_binary()
        assert b"marker-7f3a" in frame.data
        assert frame.key == reply.mid  # 字节帧靠 mid 挂回它那条请求
    finally:
        client.close()


def test_session_list_shows_state_and_size(demo_daemon):
    client = WireClient(demo_daemon.address)
    try:
        client.request("create_shell_terminal", sid="sh", cols=100, rows=30)
        sessions = data_of(client.request("list_sessions"))["sessions"]
        assert [s["sid"] for s in sessions] == ["sh"]
        assert sessions[0]["mode"] == "pty"
        assert (sessions[0]["cols"], sessions[0]["rows"]) == (100, 30)
        assert "uid" not in sessions[0]  # 核心层的标识不出网
    finally:
        client.close()


def test_input_reaches_the_program(demo_daemon):
    client = WireClient(demo_daemon.address)
    try:
        client.request("create_shell_terminal", sid="io")
        client.request("input_into_terminal", sid="io", text="echo hello-there\n")
        assert "hello-there" in client.screen_until("io", "hello-there")
    finally:
        client.close()


def test_raw_bytes_travel_as_byte_frames(demo_daemon):
    """字节帧本身就是一次操作：不需要配对的控制帧。"""
    client = WireClient(demo_daemon.address)
    try:
        client.request("create_shell_terminal", sid="raw")
        client.channel.send_bytes("stdout", "raw", b"echo from-raw-bytes\n")
        assert "from-raw-bytes" in client.screen_until("raw", "from-raw-bytes")
    finally:
        client.close()


def test_screen_image_comes_back_as_a_png(demo_daemon):
    """纯客户端没有终端模型，屏幕只能由守护进程渲染好送过来。"""
    client = WireClient(demo_daemon.address)
    try:
        client.request("create_terminal", sid="shot", command=[sys.executable, "-c", "print(1)"])
        reply = client.request("read_terminal", sid="shot", mode="image", scale=1.0)
        assert reply.kind == "image"
        assert client.wait_binary().data.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        client.close()


def test_disconnecting_leaves_the_session_alive(demo_daemon):
    """**会话与连接无关**——这正是两条链分开的意义。"""
    first = WireClient(demo_daemon.address)
    first.request("create_shell_terminal", sid="survivor")
    first.close()

    second = WireClient(demo_daemon.address)
    try:
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline:
            sessions = data_of(second.request("list_sessions"))["sessions"]
            if any(s["sid"] == "survivor" for s in sessions):
                break
        else:
            raise AssertionError("断开客户端之后会话不见了")
        second.request("input_into_terminal", sid="survivor", text="echo still-here\n")
        assert "still-here" in second.screen_until("survivor", "still-here")
    finally:
        second.close()


def test_resize_and_remove(demo_daemon):
    client = WireClient(demo_daemon.address)
    try:
        client.request("create_shell_terminal", sid="r")
        assert data_of(client.request("resize_terminal", sid="r", cols=120, rows=40)) == {
            "sid": "r",
            "cols": 120,
            "rows": 40,
        }
        assert data_of(client.request("remove_session", sids=["r"]))["removed"] == ["r"]
        assert data_of(client.request("list_sessions"))["sessions"] == []
        assert data_of(client.request("get_daemon_status"))["sessions"] == 0
    finally:
        client.close()


def test_ended_waits_for_the_tail_output_too(demo_daemon):
    """`ended` 要等**退出且排空**——进程退出与尾部输出到达之间有竞态。"""
    client = WireClient(demo_daemon.address)
    try:
        client.request(
            "create_terminal",
            sid="slow",
            command=[sys.executable, "-c", "import time; time.sleep(0.4); print('late')"],
        )
        started = time.monotonic()
        reply = client.request_with(
            "read_terminal",
            condition={"ended": True, "timeout": float(DEADLINE)},
            sid="slow",
            mode="bytes",
        )
        assert data_of(reply)["wait"]["reason"] == "ended"
        assert time.monotonic() - started >= 0.3
        assert b"late" in client.wait_binary().data  # 最后一段输出没丢
    finally:
        client.close()


def _finished_client(daemon, sid: str, code: str) -> WireClient:
    """起一个输出确定、随即结束的会话，等它结束。"""
    client = WireClient(daemon.address)
    client.request("create_terminal", sid=sid, command=[sys.executable, "-c", code])
    client.request_with(
        "read_terminal",
        condition={"ended": True, "timeout": float(DEADLINE)},
        sid=sid,
        mode="bytes",
    )
    return client


def test_bytes_read_can_take_only_the_tail(demo_daemon):
    """字节流全量可能到日志预算（1MB），客户端要能只取尾巴——否则每次刷新整段搬过来会拖死界面。"""
    client = _finished_client(demo_daemon, "tail", "print('x' * 4000)")
    try:
        full = client.wait_binary().data  # 那次 ended 读回来的整段
        reply = client.request("read_terminal", sid="tail", mode="bytes", tail=16)
        assert data_of(reply)["bytes"] == 16
        assert client.wait_binary().data == full[-16:]  # 就是这段流的最后 16 字节
    finally:
        client.close()


def test_text_read_can_take_the_last_lines(demo_daemon):
    client = _finished_client(
        demo_daemon, "lines", "print(chr(10).join('row%d' % i for i in range(60)))"
    )
    try:
        reply = client.request("read_terminal", sid="lines", mode="text", lines=3)
        text = data_of(reply)["text"]
        assert "row59" in text
        assert len(text.splitlines()) <= 4  # 只留了最后三行
    finally:
        client.close()


def test_tail_must_be_a_positive_int(demo_daemon):
    client = _finished_client(demo_daemon, "bad", "print(1)")
    try:
        for bad in (0, -5, True):
            reply = client.request("read_terminal", sid="bad", mode="bytes", tail=bad)
            assert error_of(reply).code == "BadRequest", bad
    finally:
        client.close()

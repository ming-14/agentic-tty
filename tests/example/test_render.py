"""会话 → 文本的渲染（`core_test/render.py`）：纯函数，不碰 Tk。"""

from __future__ import annotations

from agentic_tty.core.ports import Stream
from agentic_tty.core.process.session import ProcessSession
from agentic_tty.core.runtime.runner import Exited, Ingested, StreamEof
from agentic_tty.example.core_test import render
from agentic_tty.example.core_test.programs import PROGRAMS
from agentic_tty.example.core_test.runtime_fakehost import FakeHost
from agentic_tty.example.core_test.sessions import ExampleMode, session_spec


def test_view_text_of_a_process_session_is_both_streams(fake_registry):
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    assert render.view_text(session, full=False).startswith("── stdout ──")


def test_view_text_reads_the_requested_range(fake_registry, monkeypatch):
    """「可见屏幕」与「全量输出」是 core 的两项返回数据，语义不同。"""
    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))
    monkeypatch.setattr(session.host, "screen_text", lambda: "可见屏幕")
    monkeypatch.setattr(session.host, "full_text", lambda: "含历史")
    assert render.view_text(session, full=False) == "可见屏幕"
    assert render.view_text(session, full=True) == "含历史"


def test_view_text_turns_a_dead_host_into_a_note(fake_registry, monkeypatch):
    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))

    def boom() -> str:
        raise RuntimeError("宿主已关")

    monkeypatch.setattr(session.host, "screen_text", boom)
    assert render.view_text(session, full=False) == "<不可用: 宿主已关>"


def test_cells_and_rebuild_text(fake_registry):
    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))
    session.ingest_stream(Stream.STDOUT, b"ab\ncd")
    assert render.cells_text(session) == "ab\ncd"
    assert "重建字节" in render.raw_text(session)


def test_raw_text_takes_only_the_tail(fake_registry):
    """`read_all` 会把整个保留区复制一遍，所以每路只取尾部。"""
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    session.ingest_stream(Stream.STDOUT, b"x" * (render.RAW_TAIL + 10))
    assert render.raw_text(session).count("x") == render.RAW_TAIL


def test_process_members_are_none_when_not_observable():
    """未启动的会话观测不到进程树：返回 None 而不是抛给界面。"""
    session = ProcessSession(
        "uid-bare",
        session_spec(ExampleMode.FAKE, ("repl",)),
        lambda spec: FakeHost(spec, PROGRAMS["repl"]),
        journal_budget_bytes=1 << 16,
    )
    assert render.process_members(session) is None
    assert "观测不到" in render.processes_text(session)


def test_processes_text_and_row_value_show_the_members(fake_registry):
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    session.host.descendants_pids = (101, 202)
    text = render.processes_text(session)
    assert "进程树成员 2 个" in text
    assert "pid 101" in text and "pid 202" in text
    assert render.row_values(session)[4] == 2


def test_status_text_carries_uid_drain_and_exit(fake_registry):
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    status = render.status_text(session)
    assert f"uid={session.uid[:8]}" in status
    assert "exit=" in status
    assert "进行中" in status  # 脚本化宿主没排空
    assert render.metadata(session) == ""  # 非终端会话没有标题 / cwd


def test_input_text_points_out_the_soft_watermark():
    assert render.input_text(0, drained=True) == " · 输入 0B"
    assert render.input_text(70, drained=False) == " · 输入 70B（越软水位）"


def test_event_lines_render_each_kind_of_pump_event():
    """`pump_all()` 交出的三类事件：哪一路进了哪一段、哪一路排空、何时拿到退出码。"""
    events = [
        Ingested(Stream.STDOUT, 0, 3),
        Ingested(Stream.STDERR, 0, 1),
        StreamEof(Stream.STDERR),
        Exited(2),
    ]
    assert render.event_lines(events) == [
        "进 stdout [0, 3) +3B\n",
        "进 stderr [0, 1) +1B\n",
        "排空 stderr\n",
        "退出 code=2\n",
    ]

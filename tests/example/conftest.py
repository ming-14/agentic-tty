"""示例层测试的公共装配：Tk 根窗口、假宿主注册表。"""

from __future__ import annotations

import gc

import pytest

from agentic_tty.core.session.registry import SessionRegistry
from agentic_tty.example.core_test.runtime_fakehost import FakeHost, FakeProgram


@pytest.fixture
def fake_registry() -> SessionRegistry:
    """用假宿主装配的注册表：测界面与渲染不必起真进程。"""
    return SessionRegistry(lambda spec: FakeHost(spec, FakeProgram()))


@pytest.fixture
def root():
    """不弹窗口的 Tk 根（`withdraw`）；起不来 Tk（无显示环境）就跳过。"""
    tk = pytest.importorskip("tkinter")
    try:
        widget = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"无法创建 Tk 窗口: {exc}")
    widget.withdraw()
    yield widget
    # 界面与其回调互持，是环状垃圾：必须在主线程收掉，否则后台线程触发的循环 GC 会在
    # 非主线程里跑 `Variable.__del__`（`RuntimeError: main thread is not in main loop`）
    gc.collect()
    try:
        widget.destroy()
    except tk.TclError:
        pass

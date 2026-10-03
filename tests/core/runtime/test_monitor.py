"""GUI 窗口探测：只有 Windows 有实现，其他平台恒返回空。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from agentic_tty.core.runtime.monitor import WindowInfo, windows_of

_TITLE = "agentic-tty-probe"
_TK_CODE = (
    "import tkinter as tk\n"
    "r = tk.Tk()\n"
    f"r.title({_TITLE!r})\n"
    "r.geometry('160x100')\n"
    "r.update()\n"
    "print('ready', flush=True)\n"
    "r.after(10000, r.destroy)\n"
    "r.mainloop()\n"
)


def _wait_for_windows(pid: int, timeout: float = 10.0) -> tuple[WindowInfo, ...]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = windows_of({pid})
        if found:
            return found
        time.sleep(0.05)
    return windows_of({pid})


def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def test_windows_of_empty_input_and_console_process():
    assert windows_of([]) == ()
    # 控制台进程的窗口属于 conhost 而非程序本身，不能被误判成"弹了 GUI"
    assert windows_of({os.getpid()}) == ()


@pytest.mark.skipif(sys.platform != "win32", reason="窗口探测仅 Windows 有实现")
@pytest.mark.skipif(not _tk_available(), reason="没有 tkinter")
def test_windows_of_finds_gui_window_of_child_process():
    proc = subprocess.Popen([sys.executable, "-c", _TK_CODE], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        found = _wait_for_windows(proc.pid)
        assert [w.title for w in found] == [_TITLE]
        assert found[0].pid == proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5)

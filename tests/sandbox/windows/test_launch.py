"""沙箱两条 stdio 路径的端到端契约：真把子进程关进受限令牌里跑。

需要 Windows、`vendor/winsandbox/`（`_native/*.pyd`）与 `vendor/condrv/OpenConsole.exe`
——缺任一条即整体跳过（双管道那条只用 winsandbox，但跟着整模块一起跳）。

`make_launcher()`（`sandbox_pty`，伪终端）与 `make_process_launcher()`（`sandbox_subprocess`，
双管道）共用同一份限制，所以写权限与进程树的用例只在伪终端那条路上写一遍；管道那条另测
"两路各自独立、stdin 能进、关写端发 EOF"这些伪终端给不了的东西。

**跑过这些用例之后本进程不再能被外部强杀**：winsandbox 每次 spawn 会给宿主进程的
DACL 加两条 Deny（logon SID + Everyone，含 `PROCESS_TERMINATE`）。收尾仍走进程内
（`pytest-timeout` 的线程法在进程内 `os._exit`），不受影响。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("沙箱目前只有 Windows 实现", allow_module_level=True)

from agentic_tty.core.ports import SessionSpec, Stream
from agentic_tty.core.runtime.errors import MonitorUnavailable
from agentic_tty.core.runtime.local_pty.host import LocalPtyHost
from agentic_tty.core.runtime.runtime import Runtime
from agentic_tty.core.runtime.subprocess.pipes import ProcessConsole
from agentic_tty.core.session.registry import DEFAULT_KINDS, SessionRegistry
from agentic_tty.sandbox import SANDBOX_PTY, SANDBOX_SUBPROCESS, sandbox_kinds
from agentic_tty.sandbox.windows.launch import make_launcher, make_process_launcher


def _available() -> bool:
    try:
        from agentic_tty.core.runtime.local_pty.console import require_primitive
        from agentic_tty.sandbox.windows.wsandbox import require_winsandbox

        require_winsandbox()
        require_primitive().find_conhost()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="沙箱依赖不可用（vendor/ 未补齐）")

_WAIT_LIMIT = 30.0
"""等条件的时限。判定看条件，秒表只用来兜真正的回归：机器忙时进程起得慢，按固定秒表
卡会把“慢”误报成“回归”。"""

_WRITE_TRIED = (
    "try:\n"
    "    open({target}, 'w').close()\n"
    "    print('WRITE-OK')\n"
    "except Exception as exc:\n"
    "    print('WRITE-DENIED', type(exc).__name__)\n"
)


def _host(workspace: Path, *, workspace_write: bool = True, argv: list[str]) -> LocalPtyHost:
    spec = SessionSpec(mode=SANDBOX_PTY, argv=argv, cwd=str(workspace))
    return LocalPtyHost(spec, launch=make_launcher(workspace_write=workspace_write))


def _script(workspace: Path, name: str, body: str, *args: str) -> list[str]:
    """把子进程脚本放进工作区再执行——省掉 `-c` 的多行引号，顺带证明工作区可读。"""
    path = workspace / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path), *args]


def _pump(host, deadline_s: float, *, until: bytes | None = None) -> bytes:
    """把输出读进模型；给了 `until` 就读到它出现为止，否则读到排空或超时。"""
    deadline = time.monotonic() + deadline_s
    raw = b""
    while time.monotonic() < deadline:
        if until is not None and until in raw:
            break
        chunk = host.read(timeout=0.2)
        if chunk:
            raw += chunk
            host.ingest(chunk)
            continue
        if host.poll_eof():
            break
    return raw


def _settle(host, deadline_s: float = _WAIT_LIMIT) -> int | None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        code = host.try_wait()
        if code is not None:
            return code
        time.sleep(0.05)
    return None


def test_read_feed_and_screen_text(tmp_path):
    host = _host(tmp_path, argv=["cmd.exe", "/c", "echo sandbox-hello"])
    try:
        assert b"sandbox-hello" in _pump(host, _WAIT_LIMIT)
        assert "sandbox-hello" in host.screen_text()
        assert host.try_wait() == 0
    finally:
        host.kill()
        host.close()


def test_workspace_is_writable(tmp_path):
    body = "import os\n" + _WRITE_TRIED.format(target="os.path.join(os.getcwd(), 'w.txt')")
    host = _host(tmp_path, argv=_script(tmp_path, "tw.py", body))
    try:
        assert b"WRITE-OK" in _pump(host, _WAIT_LIMIT)
        assert (tmp_path / "w.txt").is_file()
    finally:
        host.kill()
        host.close()


def test_outside_the_workspace_is_read_only(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "outside.txt"
    body = "import sys\n" + _WRITE_TRIED.format(target="sys.argv[1]")
    argv = _script(tmp_path, "outside.py", body, str(target))
    host = _host(tmp_path, argv=argv)
    try:
        raw = _pump(host, _WAIT_LIMIT)
        assert b"WRITE-DENIED PermissionError" in raw, raw
        assert not target.exists()
    finally:
        host.kill()
        host.close()
        shutil.rmtree(outside, ignore_errors=True)


def test_read_only_also_blocks_the_workspace(tmp_path):
    body = "import os\n" + _WRITE_TRIED.format(target="os.path.join(os.getcwd(), 'w.txt')")
    host = _host(tmp_path, workspace_write=False, argv=_script(tmp_path, "ro.py", body))
    try:
        raw = _pump(host, _WAIT_LIMIT)
        assert b"WRITE-DENIED PermissionError" in raw, raw
        assert not (tmp_path / "w.txt").exists()
    finally:
        host.kill()
        host.close()


def test_read_only_still_gets_a_writable_private_temp(tmp_path):
    """只读档关掉的只有工作区：私有 temp 照旧可写。一个可写目录都没有的话，
    解释器的 DLL 初始化与 `tempfile` 都过不去——那一档就只是个空壳。"""
    body = (
        "import os, tempfile\n"
        + _WRITE_TRIED.format(target="os.path.join(os.getcwd(), 'w.txt')")
        + "spill = tempfile.mkdtemp()\n"
        "in_temp = os.path.dirname(spill) == os.environ['TEMP'] != os.getcwd()\n"
        "print('TEMP-DIR', spill) if in_temp else print('TEMP-WRONG', os.environ['TEMP'], spill)\n"
    )
    host = _host(tmp_path, workspace_write=False, argv=_script(tmp_path, "ro_temp.py", body))
    try:
        raw = _pump(host, _WAIT_LIMIT, until=b"TEMP-DIR")
        assert b"WRITE-DENIED PermissionError" in raw, raw
        assert b"TEMP-DIR" in raw, raw
    finally:
        host.kill()
        host.close()


def test_descendants_sees_the_job_and_excludes_the_root(tmp_path):
    """作业里的成员枚举：孙进程在，根进程不在。"""
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('GRANDCHILD', child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    host = _host(tmp_path, argv=_script(tmp_path, "tree.py", body))
    try:
        raw = _pump(host, _WAIT_LIMIT, until=b"GRANDCHILD")
        grandchild = int(raw.split(b"GRANDCHILD")[1].split()[0])
        members: tuple[int, ...] = ()
        deadline = time.monotonic() + _WAIT_LIMIT
        while members != (grandchild,) and time.monotonic() < deadline:
            members = host.descendants()
            time.sleep(0.05)
        assert members == (grandchild,), f"作业成员 {members}，根 pid {host.pid}"
    finally:
        host.kill()
        host.close()


def test_kill_ends_the_tree(tmp_path):
    argv = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"]
    host = _host(tmp_path, argv=argv)
    try:
        assert b"up" in _pump(host, _WAIT_LIMIT, until=b"up")
        assert host.try_wait() is None
        host.kill()
        assert _settle(host) is not None
    finally:
        host.kill()
        host.close()


def test_closed_tree_stops_observing(tmp_path):
    """实例关掉之后不能再装作观测得到——静默空列表会把“看不到”当成“没有”。"""
    spec = SessionSpec(mode=SANDBOX_PTY, argv=["cmd.exe", "/c", "echo x"], cwd=str(tmp_path))
    console = make_launcher()(spec)
    try:
        console.tree.descendants()  # 活着时答得出：空元组 = 确实没有子孙
        console.tree.kill()
        console.tree.close()
        with pytest.raises(MonitorUnavailable):
            console.tree.descendants()
    finally:
        console.tree.close()
        console.pty.close()


def test_mode_is_registered_and_journals_output(tmp_path):
    """按装配处的口径注册模式：与 `DEFAULT_KINDS` 合并后建会话，字节进日志与屏幕。"""
    registry = SessionRegistry(kinds={**DEFAULT_KINDS, **sandbox_kinds()})
    runtime = Runtime(registry)
    spec = SessionSpec(
        mode=SANDBOX_PTY, argv=["cmd.exe", "/c", "echo sandbox-hello"], cwd=str(tmp_path)
    )
    try:
        session = runtime.create(spec)
        deadline = time.monotonic() + _WAIT_LIMIT
        while time.monotonic() < deadline and b"sandbox-hello" not in session.read_all():
            runtime.pump_all()
            time.sleep(0.01)
        assert b"sandbox-hello" in session.read_all()
        assert "sandbox-hello" in session.screen_text()
    finally:
        runtime.close_all()


# ── sandbox_subprocess：双管道那一半 ───────────────────────────────────


def _pipes_host(tmp_path: Path, argv: list[str], *, workspace_write: bool = True) -> ProcessConsole:
    spec = SessionSpec(mode=SANDBOX_SUBPROCESS, argv=argv, cwd=str(tmp_path))
    return make_process_launcher(workspace_write=workspace_write)(spec)


def _drain(pipes, deadline_s: float = _WAIT_LIMIT) -> tuple[bytes, bytes]:
    """把两路都读到进程退出：管道读空**且**进程已退才算排空。"""
    out = b""
    err = b""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        chunk = pipes.read(timeout=0.1)
        if chunk:
            out += chunk
            continue
        chunk = pipes.read_stderr(timeout=0.1)
        if chunk:
            err += chunk
            continue
        if pipes.try_wait() is not None:
            break
    return out, err


def test_pipes_keep_the_two_streams_apart(tmp_path):
    console = _pipes_host(tmp_path, ["cmd.exe", "/c", "echo out-line && echo err-line 1>&2"])
    try:
        out, err = _drain(console.pipes)
        assert b"out-line" in out and b"err-line" not in out, out
        assert b"err-line" in err and b"out-line" not in err, err
        assert console.pipes.try_wait() == 0
    finally:
        console.tree.kill()
        console.pipes.close()


def test_stdin_reaches_the_child_and_closing_it_sends_eof(tmp_path):
    console = _pipes_host(tmp_path, ["cmd.exe", "/c", "findstr ."])
    try:
        console.pipes.write(b"line-a\r\nline-b\r\n")
        console.pipes.close_stdin()  # findstr 读到 EOF 才收工
        out, _ = _drain(console.pipes)
        assert b"line-a" in out and b"line-b" in out, out
        assert console.pipes.try_wait() == 0
    finally:
        console.tree.kill()
        console.pipes.close()


def test_pipes_share_the_workspace_rule(tmp_path):
    """受限约束与伪终端那条同源：工作区外照样写不进去。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "outside.txt"
    body = "import sys\n" + _WRITE_TRIED.format(target="sys.argv[1]")
    argv = _script(tmp_path, "outside_pipe.py", body, str(target))
    console = _pipes_host(tmp_path, argv)
    try:
        out, _ = _drain(console.pipes)
        assert b"WRITE-DENIED PermissionError" in out, out
        assert not target.exists()
    finally:
        console.tree.kill()
        console.pipes.close()
        shutil.rmtree(outside, ignore_errors=True)


def test_process_mode_is_registered_and_journals_both_streams(tmp_path):
    """按装配处的口径注册：双管道会话的字节分别进两路日志。"""
    registry = SessionRegistry(kinds={**DEFAULT_KINDS, **sandbox_kinds()})
    runtime = Runtime(registry)
    spec = SessionSpec(
        mode=SANDBOX_SUBPROCESS,
        argv=["cmd.exe", "/c", "echo pipe-out && echo pipe-err 1>&2"],
        cwd=str(tmp_path),
    )
    try:
        session = runtime.create(spec)
        deadline = time.monotonic() + _WAIT_LIMIT
        while time.monotonic() < deadline and not session.drained:
            runtime.pump_all()
            time.sleep(0.01)
        assert b"pipe-out" in session.read_all()
        assert b"pipe-err" in session.read_all(Stream.STDERR)
    finally:
        runtime.close_all()

"""后台化：起一个新进程、把入口参数带过去、把标准流接掉。

**不在这里真起守护进程**——那要等它起来、还要收掉，是端到端的事。这里只把"该起什么"
这件事锁住：命令、标志、stdio。

`detach()` 收到的是**已摘掉 `BACKGROUND_FLAG` 的参数**（摘由入口做），所以这里传进去的
就不该再带它。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agentic_tty.daemon.__main__ import _parse
from agentic_tty.daemon.platform.detach import BACKGROUND_FLAG, DetachError, detach

_IS_WINDOWS = sys.platform == "win32"


class _Child:
    pid = 4242


class _Recorder:
    """替下 `subprocess.Popen`，只记参数、不真起进程。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> _Child:
        self.calls.append((list(argv), dict(kwargs)))
        return _Child()


@pytest.fixture
def fake_popen(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    return recorder


def test_flag_name_is_defined_once():
    """入口的 argparse 与后台化层必须认同一个名字。

    写岔了不会报错——只会"说要后台、实际还在前台"，所以拿断言钉住。
    """
    parsed = _parse([BACKGROUND_FLAG])
    assert parsed.background is True
    assert parsed.cwd is None


def test_replays_the_entry_verbatim(fake_popen):
    """新进程跑同一个入口，参数原样带过去——**带的是摘过标志的那份**。"""
    parsed = _parse([BACKGROUND_FLAG, "--cwd", "/tmp/x"])
    raw = [BACKGROUND_FLAG, "--cwd", "/tmp/x"]
    detach([arg for arg in raw if arg != BACKGROUND_FLAG])
    argv, kwargs = fake_popen.calls[0]
    assert argv == [sys.executable, "-m", "agentic_tty.daemon", "--cwd", "/tmp/x"]
    assert "--background" not in argv, "标志要摘掉，否则新进程又去后台化"
    assert parsed.background is True  # 摘的是重放的那份，解析结果不受影响
    assert all(kwargs[key] is subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert kwargs["close_fds"] is True


def test_starts_a_new_session_on_posix(fake_popen):
    if _IS_WINDOWS:
        pytest.skip("POSIX 专属：start_new_session 是 setsid 的等价物")
    detach([])
    _argv, kwargs = fake_popen.calls[0]
    assert kwargs["start_new_session"] is True


def test_asks_for_a_detached_process_on_windows(fake_popen):
    if not _IS_WINDOWS:
        pytest.skip("Windows 专属")
    detach([])
    _argv, kwargs = fake_popen.calls[0]
    flags = kwargs["creationflags"]
    assert flags & subprocess.DETACHED_PROCESS
    # 刻意**不**要"新进程组"：那是更容易被 Ctrl-Break 打到，与脱开控制台相反
    assert not flags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_windows_falls_back_when_breakaway_is_denied(monkeypatch):
    """父进程困在禁止 breakaway 的作业里时退回普通脱离——实测过 WinError 5 就是这条。"""
    if not _IS_WINDOWS:
        pytest.skip("Windows 专属")
    recorded: list[int] = []

    def selective(_argv, **kwargs):
        flags = kwargs.get("creationflags", 0)
        recorded.append(flags)
        if flags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise OSError(5, "拒绝访问。")
        return _Child()

    monkeypatch.setattr(subprocess, "Popen", selective)
    detach([])
    assert len(recorded) == 2, "先试带 BREAKAWAY 的，失败后才退回"
    assert recorded[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert recorded[1] & subprocess.DETACHED_PROCESS
    assert not recorded[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB


def test_spawn_failure_surfaces_as_detach_error(monkeypatch):
    """起不来时必须明确失败——不能让一个"打算后台跑"的进程留在前台。"""

    def boom(*_argv, **_kwargs):
        raise OSError(5, "拒绝访问。")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(DetachError):
        detach([])

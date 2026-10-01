"""假程序库：示例层用到的脚本化程序。

真实场景下宿主由运行时层提供（真 PTY / 真子进程）；这里用假宿主把核心层跑起来。
"""

from __future__ import annotations

from ..core.ports import SessionSpec
from .fake_host import FakeHost, FakeProgram


def _respond(line: bytes) -> bytes:
    return b"echo: " + line.strip() + b"\n> "


PROGRAMS: dict[str, FakeProgram] = {
    "build": FakeProgram(
        chunks=(
            (0.05, b"compiling a.c\n"),
            (0.10, b"compiling b.c\n"),
            (0.15, b"linking\n"),
            (0.20, b"build OK\n"),
        ),
        exit_code=0,
        exit_after=0.6,
        title="build",
    ),
    "noisy": FakeProgram(
        chunks=((0.05, b"stdout: 1\n"), (0.08, b"stdout: 2\n")),
        stderr_chunks=((0.06, b"stderr: warning\n"),),
        exit_after=0.5,
        title="noisy",
    ),
    "repl": FakeProgram(
        chunks=((0.05, b"repl ready\n> "),),
        echo_input=True,
        respond=_respond,
        exit_after=None,  # 交互式：不自动退出
        title="repl",
    ),
    "tail": FakeProgram(
        chunks=((0.10, b"line 1\n"), (0.20, b"line 2\n"), (0.30, b"line 3\n")),
        exit_after=None,
        title="tail",
    ),
    "crash": FakeProgram(
        chunks=((0.05, b"about to fail\n"),),
        exit_code=2,
        exit_after=0.3,
        title="crash",
    ),
}


def make_host(spec: SessionSpec) -> FakeHost:
    name = spec.argv[0] if spec.argv else ""
    program = PROGRAMS.get(name)
    if program is None:
        raise KeyError(f"示例未定义程序 {name!r}（可选：{', '.join(sorted(PROGRAMS))}）")
    return FakeHost(spec, program)

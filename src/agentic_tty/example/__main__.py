"""示例：用假宿主或真宿主驱动核心层。

    python -m agentic_tty.example                       # 三个假程序场景
    python -m agentic_tty.example --gui                 # Tk 管理台
    python -m agentic_tty.example --run "cmd /c dir"    # 跑一条真命令（子进程）
    python -m agentic_tty.example --run "cmd" --mode pty --save-screen out/

有屏幕的形态（`pty` / `fake`）把可见屏幕导出成 SVG + PNG——命令行没法"显示"
图片，只能落成文件；`subprocess` 是纯字节流，直接打印双流。

场景里的 `_wait_for` 只是**演示用轮询**——真正的"返回条件引擎"属于命令层，
核心层不做匹配。
"""

from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
import time
from pathlib import Path

from ..core.ports import Stream
from ..core.session.base import Session
from ..core.views import last_lines
from ..foundation.logs import configure
from ..runtime.runner import SessionRunner
from .sessions import ExampleMode, create_session


def _open(mode: ExampleMode, argv) -> tuple[Session, SessionRunner]:
    session = create_session(mode, argv)
    session.start()
    runner = SessionRunner(session)
    runner.start()
    return session, runner


def _close(session: Session, runner: SessionRunner) -> None:
    session.close()  # 先强杀进程树再关宿主（核心层内部顺序）
    runner.stop()


def _wait_for(runner: SessionRunner, session: Session, needle: bytes, timeout: float) -> bool:
    """演示用轮询：等到输出里出现某段字节。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in session.read_all(Stream.STDOUT):
            return True
        if runner.pump():
            break
        time.sleep(0.01)
    return needle in session.read_all(Stream.STDOUT)


# ════════════════════════════════════════════════════════════════
# 假程序场景
# ════════════════════════════════════════════════════════════════


def _demo_build() -> None:
    print("== 假会话：跑完读输出 ==")
    session, runner = _open(ExampleMode.FAKE, ("build",))
    finished = runner.run_until_drained(time.monotonic() + 5.0)
    print(f"  已结束 = {finished}   退出码 = {session.exit_code}   状态 = {session.state}")
    print("  输出尾部：")
    for line in last_lines(session.read_all(Stream.STDOUT), 4).splitlines():
        print(f"    {line}")
    _close(session, runner)
    print()


def _demo_streams() -> None:
    print("== 假会话：stdout / stderr 分离 ==")
    session, runner = _open(ExampleMode.FAKE, ("noisy",))
    runner.run_until_drained(time.monotonic() + 5.0)
    print(f"  stdout = {session.read_all(Stream.STDOUT).decode().strip()!r}")
    print(f"  stderr = {session.read_all(Stream.STDERR).decode().strip()!r}")
    _close(session, runner)
    print()


def _demo_repl() -> None:
    print("== 假会话：等提示符 → 发输入 → 读回显 ==")
    session, runner = _open(ExampleMode.FAKE, ("repl",))
    print(f"  等提示符 = {_wait_for(runner, session, b'> ', 5.0)}")
    runner.submit_input(b"hello\n")
    print(f"  等回显 = {_wait_for(runner, session, b'echo: hello', 5.0)}")
    print("  屏幕快照（假宿主为纯文本尾部）：")
    for line in session.snapshot().decode().splitlines():
        print(f"    {line}")
    _close(session, runner)
    print()


def _run_demos() -> int:
    _demo_build()
    _demo_streams()
    _demo_repl()
    return 0


# ════════════════════════════════════════════════════════════════
# 真命令
# ════════════════════════════════════════════════════════════════


def _export_screen(session: Session, out_dir: Path) -> None:
    """把可见屏幕落成 SVG + 位图（命令行没法直接显示图片）。"""
    print("屏幕已导出：")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / "screen.svg"
    svg_path.write_text(session.render_svg(), encoding="utf-8")
    print(f"  SVG → {svg_path}")
    png_path = out_dir / "screen.png"
    try:
        png_path.write_bytes(session.render_image(scale=2.0, fmt="png"))
    except Exception as exc:  # 假宿主不渲染位图
        print(f"  PNG → 不可用: {exc}")
        return
    print(f"  PNG → {png_path}")


def _run_command(mode: ExampleMode, command: str, timeout: float, out_dir: Path) -> int:
    argv = shlex.split(command)
    if not argv:
        print("--run 需要一条命令", file=sys.stderr)
        return 2
    session, runner = _open(mode, argv)
    try:
        runner.run_until_drained(time.monotonic() + timeout)
        if mode is ExampleMode.SUBPROCESS:
            for stream in session.streams():
                data = session.read_all(stream)
                if data:
                    print(f"── {stream} ──")
                    print(data.decode("utf-8", errors="replace"), end="")
        else:
            _export_screen(session, out_dir)
        print(f"\n[状态] {session.state}  退出码 {session.exit_code}")
    finally:
        _close(session, runner)
    return 0


# ════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-tty-example")
    parser.add_argument("--gui", action="store_true", help="打开 Tk 管理台")
    parser.add_argument(
        "--run",
        metavar="COMMAND",
        help="跑一条命令：有屏幕的形态导出屏幕，subprocess 打印双流",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in ExampleMode],
        default=ExampleMode.SUBPROCESS.value,
        help="fake 跑示例假程序；pty / subprocess 跑真命令",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--save-screen",
        metavar="DIR",
        help="pty / fake 模式把屏幕导出到该目录（默认系统临时目录）",
    )
    args = parser.parse_args(argv)
    configure()
    if args.gui:
        from .gui import main as gui_main

        return gui_main()
    if args.run:
        out_dir = (
            Path(args.save_screen)
            if args.save_screen
            else Path(tempfile.gettempdir()) / "agentic-tty-screen"
        )
        return _run_command(ExampleMode(args.mode), args.run, args.timeout, out_dir)
    return _run_demos()


if __name__ == "__main__":
    sys.exit(main())

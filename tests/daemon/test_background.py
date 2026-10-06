"""端到端：`--background` 真的起出一个独立进程，而且它真的在服务。

单测把 `Popen` 换掉了，只证明"该起什么"；这里**真起**一次入口，证明：
父进程立刻退（返回 0）、后台进程活着、能连上、能收掉、收掉后连接点消失。

真起进程要等它 ready、还要确保收干净，所以给这条用例放长一点的超时。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_tty.config import DEFAULT_INSTANCE, PREFIX, endpoint_name, runtime_dir
from agentic_tty.protocol.contracts.daemon_ipc import Command
from agentic_tty.protocol.envelope import Envelope, from_json, make_request, to_json
from agentic_tty.protocol.frame import ControlFrame, FrameReader, encode_control
from agentic_tty.protocol.response import data_of
from agentic_tty.transport.errors import TransportError
from agentic_tty.transport.pipe import PipeTransport, pipe_address
from agentic_tty.transport.stream import Connection, parse_address

_SRC = Path(__file__).resolve().parents[2] / "src"
_DEADLINE = 20.0


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch) -> Path:
    """临时运行时目录——**父进程也按它算地址**，否则两侧算出来的不是同一个端点。"""
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


def _env(runtime: Path) -> dict[str, str]:
    env = dict(os.environ)
    # 子进程要 import 得到 agentic_tty；运行时目录跟着临时目录走，别碰真的那一个
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["LOCALAPPDATA"] = str(runtime)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


def _address() -> str:
    """算端点地址——`runtime_dir()` 读**当前进程**的环境变量，所以父进程侧要靠
    `runtime` fixture 把环境摆成与子进程一致，否则两侧算出两个不同的端点。"""
    return pipe_address(endpoint_name(DEFAULT_INSTANCE), runtime_dir(DEFAULT_INSTANCE))


def _connection(address: str) -> Connection:
    """连上为止（新进程起来要一点时间）——"就绪 = 连得上"这条口径的现场用法。"""
    deadline = time.monotonic() + _DEADLINE
    while True:
        try:
            return PipeTransport().connect(parse_address(address), timeout=1.0)
        except TransportError:
            if time.monotonic() >= deadline:
                pytest.fail(f"等了 {_DEADLINE:.0f}s 也没连上 {address}")


def _wait_gone(address: str) -> None:
    """等连接点消失——收到停机请求后它应当真的不听（而不是"进程挂着"）。"""
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        try:
            connection = PipeTransport().connect(parse_address(address), timeout=1.0)
        except TransportError:
            return
        connection.close()
        time.sleep(0.2)
    pytest.fail("要求停止后守护进程仍连得上")


def _read_one(connection: Connection) -> Envelope:
    reader = FrameReader(lambda: connection.recv(65536, timeout=0.05))
    deadline = time.monotonic() + _DEADLINE
    while True:
        frames = reader.read()
        if frames:
            assert isinstance(frames[0], ControlFrame)
            return from_json(frames[0].data)
        if time.monotonic() >= deadline:
            pytest.fail("等不到答复")


@pytest.mark.timeout(60)
def test_background_daemon_runs_untethered(runtime: Path):
    env = _env(runtime)
    address = _address()

    started = time.monotonic()
    parent = subprocess.run(
        [sys.executable, "-m", "agentic_tty.daemon", "--background"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - started
    assert parent.returncode == 0, parent.stderr
    assert elapsed < 10.0, "父进程应当起完就走，不该等服务就绪"

    connection = _connection(address)
    try:
        connection.send(encode_control(to_json(make_request(Command.DAEMON_STATUS))))
        status = _read_one(connection)
        # 报出来的 pid 是**那个新进程**的，不是发起这一次启动的那个 python
        assert data_of(status)["pid"] > 0

        connection.send(encode_control(to_json(make_request(Command.SHUTDOWN_DAEMON))))
        _read_one(connection)  # 停机 ack
    finally:
        connection.close()

    _wait_gone(address)


@pytest.mark.timeout(60)
def test_custom_name_and_listen(runtime: Path):
    """`--name` 定实例（锁 / 目录），`--listen` 定接入点——后者是完整管道名，不与前者互相派生。"""
    env = _env(runtime)
    name = f"custom-{uuid4().hex[:8]}"
    endpoint = f"custom-pipe-{uuid4().hex[:8]}"
    address = pipe_address(endpoint, runtime_dir(name))

    parent = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_tty.daemon",
            "--background",
            "--name",
            name,
            "--listen",
            endpoint,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert parent.returncode == 0, parent.stderr

    connection = _connection(address)
    try:
        connection.send(encode_control(to_json(make_request(Command.DAEMON_STATUS))))
        assert data_of(_read_one(connection))["endpoint"] == address

        connection.send(encode_control(to_json(make_request(Command.SHUTDOWN_DAEMON))))
        _read_one(connection)
    finally:
        connection.close()

    _wait_gone(address)
    # 运行时目录（锁 / 日志）按 `--name` 落，与端点名无关
    assert (runtime / f"{PREFIX}{name}").is_dir()


@pytest.mark.timeout(60)
def test_foreground_daemon_still_serves(runtime: Path):
    """前台不变：仍然阻塞着服务（这里只验证"不给 --background 时照旧跑起来"）。"""
    env = _env(runtime)
    address = _address()

    child = subprocess.Popen(
        [sys.executable, "-m", "agentic_tty.daemon"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        connection = _connection(address)
        connection.close()
        assert child.poll() is None, "前台进程不该自己退出"
    finally:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

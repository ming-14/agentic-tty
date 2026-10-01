"""TCP 传输：同机 loopback 与跨机**是同一份代码**。

差别只在监听地址（`127.0.0.1` 还是可配置接口）与要不要在外面套 TLS，实现本身
没有任何分支。loopback 不触发防火墙、外部不可达。

**不设 `SO_REUSEADDR`**：它在 Windows 上允许别的进程绑同一地址（劫持语义），
而不设的代价——重启时撞上 TIME_WAIT——用"端口写 0 让内核挑"就能绕开，
不必为此在上层写平台分支。
"""

from __future__ import annotations

import socket

from ..foundation.logs import get_logger
from .errors import ConnectionClosed, TransportError
from .stream import Address, Connection, Listener

_logger = get_logger("transport.tcp")

_LISTEN_BACKLOG = 64


class TcpConnection:
    """一条 TCP 连接。"""

    def __init__(self, sock: socket.socket, peer: str) -> None:
        self._sock = sock
        self._peer = peer
        self._closed = False

    @property
    def peer(self) -> str:
        return self._peer

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        try:
            self._sock.settimeout(timeout)
            data = self._sock.recv(max_bytes)
        except (TimeoutError, BlockingIOError):
            # 本轮无数据。`timeout=0` 会让套接字变非阻塞，那条路抛的是
            # BlockingIOError（也是 OSError 的子类），必须先于下面接住，
            # 否则会被当成"对端已关闭"。
            return b""
        except OSError as exc:
            raise ConnectionClosed(f"读取失败 peer={self._peer}: {exc}") from exc
        if not data:
            raise ConnectionClosed(f"对端已关闭 peer={self._peer}")
        return data

    def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionClosed(f"连接已关闭 peer={self._peer}")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ConnectionClosed(f"写入失败 peer={self._peer}: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # 对端已断，关掉就行
            pass
        self._sock.close()


class TcpListener:
    """监听点。"""

    def __init__(self, sock: socket.socket, address: Address) -> None:
        self._sock = sock
        self._address = address
        self._closed = False

    @property
    def address(self) -> Address:
        return self._address

    def accept(self, timeout: float | None = None) -> Connection | None:
        if self._closed:
            raise ConnectionClosed("监听点已关闭")
        try:
            self._sock.settimeout(timeout)
            conn, peer = self._sock.accept()
        except (TimeoutError, BlockingIOError):
            # 本轮没有待接受的连接。`timeout=0`（非阻塞轮询）走的是 BlockingIOError
            # 那条路，必须先于下面接住，否则会被当成"监听点坏了"。
            return None
        except OSError as exc:
            raise ConnectionClosed(f"接受连接失败: {exc}") from exc
        return TcpConnection(conn, f"{peer[0]}:{peer[1]}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sock.close()


class TcpTransport:
    """TCP 传输实现。"""

    scheme = "tcp"

    def listen(self, address: Address) -> Listener:
        host, port = address.host_port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.listen(_LISTEN_BACKLOG)
            bound = sock.getsockname()
        except OSError as exc:
            sock.close()
            raise TransportError(f"绑定 {address} 失败: {exc}") from exc
        listener_address = address.with_netloc(f"{bound[0]}:{bound[1]}")
        _logger.info("开始监听 %s", listener_address)
        return TcpListener(sock, listener_address)

    def connect(self, address: Address, *, timeout: float = 5.0) -> Connection:
        host, port = address.host_port
        if not port:
            raise TransportError(f"连接地址必须给出端口: {address}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((host, port))
        except OSError as exc:
            sock.close()
            raise TransportError(f"连接 {address} 失败: {exc}") from exc
        return TcpConnection(sock, f"{host}:{port}")

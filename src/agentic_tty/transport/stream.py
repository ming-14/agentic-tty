"""传输抽象：只搬字节。

这里**刻意不认识帧**——帧是 `protocol` 的事。于是"共享内存 / 命名管道 / stdio"
要接进来时，只需要再实现一遍 `Connection` 与 `Listener`，上层一行不改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .errors import AddressError


@dataclass(frozen=True, slots=True)
class Address:
    """统一地址：`tcp://127.0.0.1:8765`。

    `netloc` **原样保留**（不做大小写归一）——非 host:port 的 scheme（共享内存名、
    管道名）是大小写敏感的，`urlsplit().hostname` 会把它们改掉。
    """

    scheme: str
    netloc: str = ""
    path: str = ""
    raw: str = ""

    @property
    def host_port(self) -> tuple[str, int]:
        """`host:port` 形式的地址拆成两段；端口允许为 0（由内核挑一个）。"""
        host, _, port_text = self.netloc.rpartition(":")
        if not host:
            host, port_text = self.netloc, ""
        try:
            port = int(port_text) if port_text else 0
        except ValueError:
            raise AddressError(f"端口不是数字: {self.raw!r}") from None
        if not 0 <= port <= 65535:
            raise AddressError(f"端口越界: {self.raw!r}")
        return host, port

    def with_netloc(self, netloc: str) -> Address:
        """换掉 `host:port`（监听端口填 0 时用来回报真实端口）。"""
        raw = f"{self.scheme}://{netloc}{self.path}"
        return Address(scheme=self.scheme, netloc=netloc, path=self.path, raw=raw)

    def __str__(self) -> str:
        return self.raw


def parse_address(text: str) -> Address:
    """解析地址；没有 scheme 直接报错，不猜。"""
    parts = urlsplit(text)
    if not parts.scheme:
        raise AddressError(f"地址缺少 scheme（形如 tcp://127.0.0.1:8765）: {text!r}")
    return Address(scheme=parts.scheme.lower(), netloc=parts.netloc, path=parts.path, raw=text)


@runtime_checkable
class Connection(Protocol):
    """一条双向字节流。实现要保证 `recv` 与 `send` 可以被两个线程同时用。"""

    @property
    def peer(self) -> str:
        """对端标识，只用于日志。"""
        ...

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """读一段。**空返回值表示本轮无数据（超时）**；对端关闭抛 `ConnectionClosed`。"""
        ...

    def send(self, data: bytes) -> None:
        """写完这段再返回；对端已断抛 `ConnectionClosed`。"""
        ...

    def close(self) -> None:
        """关闭（幂等）。"""
        ...


@runtime_checkable
class Listener(Protocol):
    """监听点。"""

    @property
    def address(self) -> Address:
        """实际绑定的地址（端口填 0 时这里给出内核挑的那个）。"""
        ...

    def accept(self, timeout: float | None = None) -> Connection | None:
        """接受一个连接；超时返回 None。"""
        ...

    def close(self) -> None:
        """关闭（幂等）。"""
        ...


@runtime_checkable
class Transport(Protocol):
    """一族传输实现，能力只有两件：监听与连接。"""

    scheme: str

    def listen(self, address: Address) -> Listener:
        """占用地址开始监听。"""
        ...

    def connect(self, address: Address, *, timeout: float = 5.0) -> Connection:
        """连上去。"""
        ...

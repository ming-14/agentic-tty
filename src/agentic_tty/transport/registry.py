"""按地址 scheme 分派传输实现。

**这是"留缺口"的落点**：以后接共享内存、命名管道、stdio，只需在这里注册一个新的
实现，`daemon` 与客户端一行都不用改——它们只会拿到另一个 `Connection`。

    register(ShmTransport())            # shm://<名字>
    connection = connect("shm://agentic-tty")

注册表是显式构建的，不靠 import 副作用。
"""

from __future__ import annotations

from .errors import UnsupportedScheme
from .stream import Connection, Listener, Transport, parse_address
from .tcp import TcpTransport

_TRANSPORTS: dict[str, Transport] = {TcpTransport.scheme: TcpTransport()}


def register(transport: Transport) -> None:
    """注册（或替换）一个 scheme 的实现。"""
    _TRANSPORTS[transport.scheme] = transport


def unregister(scheme: str) -> None:
    """注销一个 scheme；没注册过也不报错。"""
    _TRANSPORTS.pop(scheme.lower(), None)


def schemes() -> list[str]:
    """已注册的 scheme 列表。"""
    return sorted(_TRANSPORTS)


def transport_for(scheme: str) -> Transport:
    transport = _TRANSPORTS.get(scheme.lower())
    if transport is None:
        raise UnsupportedScheme(f"没有 {scheme!r} 的传输实现（可用：{', '.join(schemes())}）")
    return transport


def listen(uri: str) -> Listener:
    """按地址开始监听。"""
    address = parse_address(uri)
    return transport_for(address.scheme).listen(address)


def connect(uri: str, *, timeout: float = 5.0) -> Connection:
    """按地址连上去。"""
    address = parse_address(uri)
    return transport_for(address.scheme).connect(address, timeout=timeout)

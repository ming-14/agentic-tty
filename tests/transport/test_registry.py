from __future__ import annotations

import pytest

from agentic_tty.transport import registry
from agentic_tty.transport.errors import AddressError, UnsupportedScheme
from agentic_tty.transport.stream import Address, Connection, Listener


class _FakeConnection:
    def __init__(self, peer: str = "fake") -> None:
        self._peer = peer
        self.sent: list[bytes] = []

    @property
    def peer(self) -> str:
        return self._peer

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return b""

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass


class _FakeListener:
    def __init__(self, address: Address) -> None:
        self._address = address

    @property
    def address(self) -> Address:
        return self._address

    def accept(self, timeout: float | None = None) -> Connection | None:
        return None

    def close(self) -> None:
        pass


class _FakeTransport:
    """假传输：证明"加新传输方式只需注册一条"，别的代码一行不改。"""

    scheme = "fake"

    def __init__(self) -> None:
        self.listened: list[Address] = []

    def listen(self, address: Address) -> Listener:
        self.listened.append(address)
        return _FakeListener(address)

    def connect(self, address: Address, *, timeout: float = 5.0) -> Connection:
        return _FakeConnection(str(address))


def test_tcp_is_registered_by_default():
    assert "tcp" in registry.schemes()


def test_unknown_scheme_is_rejected():
    with pytest.raises(UnsupportedScheme, match="没有"):
        registry.transport_for("shm")


def test_registering_a_new_scheme_is_enough():
    transport = _FakeTransport()
    registry.register(transport)
    try:
        assert "fake" in registry.schemes()
        connection = registry.connect("fake://host:1")
        assert connection.peer == "fake://host:1"
        listener = registry.listen("fake://host:2")
        assert transport.listened[0].scheme == "fake"
        assert listener.accept() is None
    finally:
        registry.unregister("fake")
    assert "fake" not in registry.schemes()


def test_unregistering_an_unknown_scheme_is_harmless():
    registry.unregister("never-registered")


def test_registry_dispatches_by_scheme_case_insensitively():
    assert registry.transport_for("TCP").scheme == "tcp"


def test_connect_rejects_missing_scheme():
    with pytest.raises(AddressError):
        registry.connect("127.0.0.1:1")

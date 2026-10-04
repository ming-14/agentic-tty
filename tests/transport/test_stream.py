import pytest

from agentic_tty.transport import registry
from agentic_tty.transport.errors import AddressError
from agentic_tty.transport.stream import Address, Connection, Listener, Transport, parse_address
from agentic_tty.transport.tcp import TcpTransport


def test_parse_tcp_address():
    address = parse_address("tcp://127.0.0.1:8765")
    assert address.scheme == "tcp"
    assert address.host_port == ("127.0.0.1", 8765)
    assert str(address) == "tcp://127.0.0.1:8765"


def test_scheme_is_lowercased():
    assert parse_address("TCP://127.0.0.1:1").scheme == "tcp"


def test_port_may_be_zero_to_let_the_kernel_pick():
    assert parse_address("tcp://127.0.0.1:0").host_port == ("127.0.0.1", 0)


def test_host_only_address_has_port_zero():
    assert parse_address("shm://pool").host_port == ("pool", 0)


def test_netloc_keeps_original_case():
    """非 host:port 的 scheme（共享内存名、管道名）是大小写敏感的。"""
    assert parse_address("shm://AgenticTTY").netloc == "AgenticTTY"


def test_path_is_kept():
    assert parse_address("pipe:///tmp/sock").path == "/tmp/sock"


def test_address_without_scheme_is_rejected():
    with pytest.raises(AddressError, match="scheme"):
        parse_address("127.0.0.1:8765")


@pytest.mark.parametrize("text", ["tcp://127.0.0.1:abc", "tcp://127.0.0.1:99999"])
def test_bad_port_is_rejected(text):
    with pytest.raises(AddressError, match="端口"):
        _ = parse_address(text).host_port


def test_with_netloc_rebuilds_raw():
    address = parse_address("tcp://127.0.0.1:0")
    assert str(address.with_netloc("127.0.0.1:54321")) == "tcp://127.0.0.1:54321"


def test_with_netloc_keeps_path():
    address = Address(scheme="pipe", netloc="old", path="/tmp/x", raw="pipe://old/tmp/x")
    assert str(address.with_netloc("new")) == "pipe://new/tmp/x"


def test_transport_implementation_satisfies_the_protocol():
    assert isinstance(TcpTransport(), Transport)


def test_real_objects_satisfy_the_connection_and_listener_protocols():
    """`Connection` / `Listener` 是结构性契约,实现缺方法要在这里就暴露,
    而不是等上层运行到那一步才发现。"""
    listener = registry.listen("tcp://127.0.0.1:0")
    try:
        assert isinstance(listener, Listener)
        connection = registry.connect(listener.address.raw, timeout=2.0)
        try:
            assert isinstance(connection, Connection)
        finally:
            connection.close()
    finally:
        listener.close()

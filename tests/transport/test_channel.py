from __future__ import annotations

import time

import pytest

from agentic_tty.protocol.envelope import make_request
from agentic_tty.protocol.frame import BytesFrame, ControlFrame
from agentic_tty.transport import registry
from agentic_tty.transport.channel import Channel, decode_control
from agentic_tty.transport.stream import Listener

_DEADLINE = 5.0


@pytest.fixture
def pair() -> tuple[Channel, Channel]:
    listener: Listener = registry.listen("tcp://127.0.0.1:0")
    client = Channel(registry.connect(listener.address.raw, timeout=_DEADLINE))
    server = Channel(listener.accept(timeout=_DEADLINE))
    try:
        yield client, server
    finally:
        client.close()
        server.close()
        listener.close()


def _recv_until(channel: Channel, count: int) -> list[object]:
    frames: list[object] = []
    deadline = time.monotonic() + _DEADLINE
    while len(frames) < count:
        assert time.monotonic() < deadline
        frames.extend(channel.recv(timeout=0.1))
    return frames


def test_recv_returns_empty_on_timeout(pair):
    client, _ = pair
    assert client.recv(timeout=0.05) == []


def test_envelope_roundtrip(pair):
    client, server = pair
    request = make_request("read_terminal", op={"sid": "s1"})
    client.send(request)
    frames = _recv_until(server, 1)
    assert isinstance(frames[0], ControlFrame)
    assert decode_control(frames[0]) == request


def test_bytes_frame_roundtrip(pair):
    client, server = pair
    client.send_bytes("stderr", "sid-1", b"\x00\x01raw")
    frames = _recv_until(server, 1)
    assert frames[0] == BytesFrame(stream="stderr", key="sid-1", data=b"\x00\x01raw")


def test_mixed_frames_keep_their_order(pair):
    client, server = pair
    client.send(make_request("a"))
    client.send_bytes("stdout", "s", b"payload")
    client.send(make_request("b"))
    frames = _recv_until(server, 3)
    assert isinstance(frames[0], ControlFrame)
    assert isinstance(frames[1], BytesFrame)
    assert isinstance(frames[2], ControlFrame)


def test_peer_is_reported_for_logging(pair):
    client, server = pair
    assert client.peer.startswith("127.0.0.1:")
    assert server.peer.startswith("127.0.0.1:")

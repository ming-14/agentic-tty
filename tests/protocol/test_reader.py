"""FrameReader：字节源由装配方注入，凑齐一帧交一帧。

字节源用脚本喂（而不是真连接），因为这一层要验的是"任意切分都能解回来"——
脚本能把一次发送切成任意小的段，比等真实 TCP 切包更确定。
"""

from __future__ import annotations

import pytest

from agentic_tty.protocol.envelope import from_json, make_request, to_json
from agentic_tty.protocol.frame import (
    BytesFrame,
    ControlFrame,
    FrameError,
    FrameReader,
    encode_bytes,
    encode_control,
)


class _Scripted:
    """按脚本一段一段吐字节；吐完返回空，模拟"本轮无数据"。"""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    def __call__(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    @property
    def drained(self) -> bool:
        return not self._chunks


def _sliced(data: bytes, size: int) -> list[bytes]:
    return [data[offset : offset + size] for offset in range(0, len(data), size)]


def _read_until(reader: FrameReader, source: _Scripted, count: int) -> list[object]:
    """喂到脚本耗尽或凑够条数——不按轮数猜，避免与切片大小耦合。"""
    frames: list[object] = []
    while not source.drained and len(frames) < count:
        frames.extend(reader.read())
    return frames


def test_read_returns_empty_when_the_source_has_nothing():
    assert FrameReader(_Scripted()).read() == []


def test_frames_survive_being_split_and_coalesced():
    """一次发送被切成 3 字节一段、几帧粘在一起——解码必须是增量的。"""
    stream = (
        encode_control(to_json(make_request("a")))
        + encode_bytes("stdout", "s", b"payload")
        + encode_control(to_json(make_request("b")))
    )
    source = _Scripted(*_sliced(stream, 3))
    frames = _read_until(FrameReader(source), source, 3)
    assert isinstance(frames[0], ControlFrame)
    assert isinstance(frames[1], BytesFrame)
    assert isinstance(frames[2], ControlFrame)


def test_control_frame_carries_the_envelope_back():
    request = make_request("read_terminal", op={"sid": "s1"})
    source = _Scripted(encode_control(to_json(request)))
    frames = _read_until(FrameReader(source), source, 1)
    assert isinstance(frames[0], ControlFrame)
    assert from_json(frames[0].data) == request


def test_bytes_frame_keeps_raw_bytes():
    source = _Scripted(encode_bytes("stderr", "sid-1", b"\x00\x01raw"))
    assert _read_until(FrameReader(source), source, 1) == [
        BytesFrame(stream="stderr", key="sid-1", data=b"\x00\x01raw")
    ]


def test_reader_is_dead_once_the_stream_is_misaligned():
    """错位之后不可能再读出东西——调用方没有"忘记断连"的余地。"""
    reader = FrameReader(_Scripted(b"\x09" + (1).to_bytes(4, "big") + b"x"))
    with pytest.raises(FrameError):
        reader.read()
    assert reader.broken
    with pytest.raises(FrameError):
        reader.read()

from __future__ import annotations

import pytest

from agentic_tty.protocol import frame as frame_module
from agentic_tty.protocol.errors import FrameError
from agentic_tty.protocol.frame import (
    KIND_BYTES,
    KIND_CONTROL,
    BytesFrame,
    ControlFrame,
    FrameDecoder,
    encode_bytes,
    encode_control,
)

# 帧层不认识"流"——标签是 1 字节的不透明值，测试用两个任意值。
_TAG_A = 0x01
_TAG_B = 0x02


def _decode_all(*chunks: bytes) -> list[ControlFrame | BytesFrame]:
    decoder = FrameDecoder()
    frames: list[ControlFrame | BytesFrame] = []
    for chunk in chunks:
        frames.extend(decoder.feed(chunk))
    return frames


def test_control_roundtrip():
    wire = encode_control(b'{"a":1}')
    assert _decode_all(wire) == [ControlFrame(b'{"a":1}')]


def test_control_accepts_empty_payload():
    assert _decode_all(encode_control(b"")) == [ControlFrame(b"")]


def test_bytes_roundtrip():
    wire = encode_bytes(_TAG_B, "sid-1", b"\x1b[31mred\x1b[0m")
    assert _decode_all(wire) == [
        BytesFrame(tag=_TAG_B, key="sid-1", data=b"\x1b[31mred\x1b[0m")
    ]


def test_bytes_accepts_empty_data():
    wire = encode_bytes(_TAG_A, "m1", b"")
    assert _decode_all(wire) == [BytesFrame(tag=_TAG_A, key="m1", data=b"")]


def test_bytes_keeps_binary_data_intact():
    blob = bytes(range(256)) * 4
    frames = _decode_all(encode_bytes(_TAG_A, "m1", blob))
    assert isinstance(frames[0], BytesFrame)
    assert frames[0].data == blob


def test_frames_can_be_glued_together():
    wire = encode_control(b"one") + encode_bytes(_TAG_A, "k", b"two") + encode_control(b"three")
    assert _decode_all(wire) == [
        ControlFrame(b"one"),
        BytesFrame(tag=_TAG_A, key="k", data=b"two"),
        ControlFrame(b"three"),
    ]


def test_split_at_every_boundary_yields_same_frames():
    """TCP 会把一次发送切成任意片段，切在哪都不能影响结果。"""
    wire = encode_control(b'{"x":"\xe4\xb8\xad"}') + encode_bytes(_TAG_B, "sid", b"\x00\x01")
    expected = _decode_all(wire)
    for cut in range(len(wire) + 1):
        assert _decode_all(wire[:cut], wire[cut:]) == expected, f"切在 {cut} 字节处"


def test_byte_by_byte_feed():
    wire = encode_bytes(_TAG_A, "key", b"payload")
    assert _decode_all(*[bytes((b,)) for b in wire]) == [
        BytesFrame(tag=_TAG_A, key="key", data=b"payload")
    ]


def test_incomplete_frame_is_buffered_not_emitted():
    decoder = FrameDecoder()
    wire = encode_control(b"abcdef")
    assert decoder.feed(wire[:-1]) == []
    assert decoder.pending == len(wire) - 1
    assert decoder.feed(wire[-1:]) == [ControlFrame(b"abcdef")]
    assert decoder.pending == 0


def test_oversized_payload_is_rejected():
    header = bytes((KIND_CONTROL,)) + (17 << 20).to_bytes(4, "big")
    with pytest.raises(FrameError, match="超限"):
        _decode_all(header)


def test_encode_rejects_oversized_control_payload(monkeypatch):
    """编码器与解码器同一道上限——超限的帧对端必然拒收，发送端就该先报错。"""
    monkeypatch.setattr(frame_module, "_MAX_PAYLOAD", 8)
    with pytest.raises(FrameError, match="超限"):
        encode_control(b"x" * 9)


def test_encode_rejects_oversized_bytes_payload(monkeypatch):
    monkeypatch.setattr(frame_module, "_MAX_PAYLOAD", 8)
    with pytest.raises(FrameError, match="超限"):
        encode_bytes(_TAG_A, "k", b"x" * 16)


def test_unknown_frame_kind_is_rejected():
    with pytest.raises(FrameError, match="未知帧类型"):
        _decode_all(bytes((0x7F, 0, 0, 0, 0)))


def test_truncated_bytes_frame_is_rejected():
    payload = bytes((0x01, 8)) + b"short"
    wire = bytes((KIND_BYTES,)) + len(payload).to_bytes(4, "big") + payload
    with pytest.raises(FrameError, match="键长度越界"):
        _decode_all(wire)


def test_bytes_frame_shorter_than_tag_and_length_is_rejected():
    wire = bytes((KIND_BYTES, 0, 0, 0, 1)) + b"\x01"
    with pytest.raises(FrameError, match="过短"):
        _decode_all(wire)


def test_encode_rejects_tag_out_of_range():
    """标签只能占 1 字节。"""
    with pytest.raises(FrameError, match="越界"):
        encode_bytes(0x100, "k", b"x")


def test_encode_rejects_overlong_key():
    with pytest.raises(FrameError, match="帧键过长"):
        encode_bytes(_TAG_A, "k" * 256, b"x")


def test_multi_byte_key_survives_roundtrip():
    frames = _decode_all(encode_bytes(_TAG_A, "会话-1", b"x"))
    assert isinstance(frames[0], BytesFrame)
    assert frames[0].key == "会话-1"


def test_key_that_is_not_utf8_is_reported_as_a_frame_error():
    """对端送来的键不是合法 UTF-8 时不能裸穿 UnicodeDecodeError。"""
    payload = bytes((0x01, 2)) + b"\xff\xfe" + b"data"
    wire = bytes((KIND_BYTES,)) + len(payload).to_bytes(4, "big") + payload
    with pytest.raises(FrameError, match="UTF-8"):
        _decode_all(wire)

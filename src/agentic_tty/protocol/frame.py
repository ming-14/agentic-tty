"""帧：长度前缀，控制与字节两条通道分开。

TCP 没有消息边界，还会把一次发送切开、把几次发送粘起来，所以每帧自带长度、解码
必须增量（攒够一个完整帧才交出去）。

    ┌──────────┬──────────────────┬──────────────────┐
    │ 帧类型 1B │ 负载长度 4B(大端) │ 负载 length 字节  │
    └──────────┴──────────────────┴──────────────────┘

控制帧是 JSON 信封；字节帧是"流标签 + 键 + 原始字节"——字节不走 JSON，否则要给
每个字节做 base64（膨胀三分之一，也抹平"字节流才是真源"）。单帧上限见 `_MAX_PAYLOAD`。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import FrameError

# 帧类型
KIND_CONTROL = 0x01
"""控制帧：负载是 UTF-8 的 JSON 信封。"""
KIND_BYTES = 0x02
"""字节帧：负载是"流标签 + 键 + 原始字节"。"""

# 流标签（取值与 core 的 Stream 一致，但协议层不 import core）
STREAM_STDOUT = 0x01
STREAM_STDERR = 0x02

_STREAM_NAMES = {STREAM_STDOUT: "stdout", STREAM_STDERR: "stderr"}
_STREAM_TAGS = {name: tag for tag, name in _STREAM_NAMES.items()}

_HEADER = struct.Struct(">BI")  # 帧类型 + 负载长度
_MAX_PAYLOAD = 16 << 20
"""单帧上限。长度字段是对端给的，不设上限就等于允许它让我们分配任意大的内存。"""
_KEY_MAX = 0xFF  # 键长度用 1 字节


@dataclass(frozen=True, slots=True)
class ControlFrame:
    """控制帧：一段待解码的 JSON 信封字节。"""

    data: bytes


@dataclass(frozen=True, slots=True)
class BytesFrame:
    """字节帧：一个流标签、一个键、一段原始字节。

    键的含义按方向定（同一连接上先发控制帧表明意图、再发字节帧，顺序由传输保证）：

    - 客户端 → 守护进程：键是会话 `sid`，这批字节写给谁；
    - 守护进程 → 客户端：键是消息 `mid`，这批字节属于哪个请求的返回。
    """

    stream: str
    key: str
    data: bytes


def encode_control(data: bytes) -> bytes:
    """把信封字节封成控制帧。"""
    return _HEADER.pack(KIND_CONTROL, len(data)) + data


def encode_bytes(stream: str, key: str, data: bytes) -> bytes:
    """把一段原始字节封成字节帧。"""
    tag = _STREAM_TAGS.get(stream)
    if tag is None:
        raise FrameError(f"未知流标签: {stream!r}")
    key_bytes = key.encode("utf-8")
    if len(key_bytes) > _KEY_MAX:
        raise FrameError(f"帧键过长（{len(key_bytes)} 字节）: {key!r}")
    payload = bytes((tag, len(key_bytes))) + key_bytes + data
    return _HEADER.pack(KIND_BYTES, len(payload)) + payload


class FrameDecoder:
    """增量解码器：喂进任意切分的字节，吐出其中已经完整的帧。

    解析出错一律抛 `FrameError` 并**不再继续可用**——字节流已经错位，接着解只会
    解出垃圾。调用方收到异常就断连接。
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """还没凑成一个完整帧的字节数（诊断用）。"""
        return len(self._buf)

    def feed(self, data: bytes) -> list[ControlFrame | BytesFrame]:
        """喂入新到的字节，返回这次凑齐的全部帧（可能为空）。"""
        self._buf.extend(data)
        frames: list[ControlFrame | BytesFrame] = []
        while len(self._buf) >= _HEADER.size:
            kind, length = _HEADER.unpack_from(self._buf)
            if length > _MAX_PAYLOAD:
                raise FrameError(f"帧负载超限: {length} > {_MAX_PAYLOAD}")
            total = _HEADER.size + length
            if len(self._buf) < total:
                break
            payload = bytes(self._buf[_HEADER.size : total])
            del self._buf[:total]
            frames.append(_decode_payload(kind, payload))
        return frames


def _decode_payload(kind: int, payload: bytes) -> ControlFrame | BytesFrame:
    if kind == KIND_CONTROL:
        return ControlFrame(payload)
    if kind == KIND_BYTES:
        if len(payload) < 2:
            raise FrameError("字节帧负载过短")
        tag, key_len = payload[0], payload[1]
        stream = _STREAM_NAMES.get(tag)
        if stream is None:
            raise FrameError(f"未知流标签: {tag:#x}")
        if len(payload) < 2 + key_len:
            raise FrameError("字节帧键长度越界")
        try:
            key = payload[2 : 2 + key_len].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError("字节帧的键不是合法 UTF-8") from exc
        return BytesFrame(stream=stream, key=key, data=payload[2 + key_len :])
    raise FrameError(f"未知帧类型: {kind:#x}")

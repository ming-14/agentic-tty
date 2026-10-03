"""信封：两端共享的统一格式。

字段：`proto / dir / type / mid / ts / kind / auth / payload`。载荷分四组——操作参数 `op`、
返回条件 `condition`、返回数据 `output`、IO `io`——让各归其位；分组只是组织方式，
不改变线格式。

`direction` 在线格式里的键是 `dir`（避免与内建 `dir` 同名）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..foundation.ids import new_message_id, now_timestamp
from .errors import EnvelopeError

PROTO_VERSION = 1
"""协议版本。破坏性变更时递增，由 `proto` 字段协商。"""

DIR_REQUEST = "request"
DIR_RESPONSE = "response"

_GROUP_KEYS = ("op", "condition", "output", "io")


@dataclass(frozen=True, slots=True)
class Payload:
    """业务载荷的四个分组。"""

    op: Mapping[str, Any] = field(default_factory=dict)
    condition: Mapping[str, Any] = field(default_factory=dict)
    output: Mapping[str, Any] = field(default_factory=dict)
    io: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": dict(self.op),
            "condition": dict(self.condition),
            "output": dict(self.output),
            "io": dict(self.io),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Payload:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise EnvelopeError("payload 必须是对象")
        groups: dict[str, Any] = {}
        for key in _GROUP_KEYS:
            value = raw.get(key)
            if value is None:
                groups[key] = {}
                continue
            if not isinstance(value, dict):
                raise EnvelopeError(f"payload.{key} 必须是对象")
            groups[key] = value
        return cls(**groups)


@dataclass(frozen=True, slots=True)
class Envelope:
    """一条消息。"""

    type: str
    direction: str
    mid: str
    payload: Payload = field(default_factory=Payload)
    proto: int = PROTO_VERSION
    ts: str = ""
    kind: str = "text"
    auth: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proto": self.proto,
            "dir": self.direction,
            "type": self.type,
            "mid": self.mid,
            "ts": self.ts,
            "kind": self.kind,
            "auth": self.auth,
            "payload": self.payload.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Envelope:
        if not isinstance(raw, dict):
            raise EnvelopeError("信封必须是对象")
        proto = raw.get("proto")
        if not isinstance(proto, int):
            raise EnvelopeError("信封缺少 proto")
        if proto != PROTO_VERSION:
            raise EnvelopeError(f"协议版本不匹配: 对端 {proto}，本端 {PROTO_VERSION}")
        direction = raw.get("dir")
        if direction not in (DIR_REQUEST, DIR_RESPONSE):
            raise EnvelopeError(f"未知消息方向: {direction!r}")
        type_ = raw.get("type")
        if not isinstance(type_, str) or not type_:
            raise EnvelopeError("信封缺少 type")
        mid = raw.get("mid")
        if not isinstance(mid, str) or not mid:
            raise EnvelopeError("信封缺少 mid")
        auth = raw.get("auth")
        if auth is not None and not isinstance(auth, str):
            raise EnvelopeError("auth 必须是字符串或 null")
        ts = raw.get("ts")
        if ts is not None and not isinstance(ts, str):
            raise EnvelopeError("ts 必须是字符串或 null")
        kind = raw.get("kind")
        if kind is not None and not isinstance(kind, str):
            raise EnvelopeError("kind 必须是字符串或 null")
        return cls(
            type=type_,
            direction=direction,
            mid=mid,
            payload=Payload.from_dict(raw.get("payload")),
            proto=proto,
            ts=ts or "",
            kind=kind or "text",
            auth=auth,
        )


def make_request(
    type_: str,
    *,
    op: Mapping[str, Any] | None = None,
    condition: Mapping[str, Any] | None = None,
    io: Mapping[str, Any] | None = None,
    kind: str = "text",
    auth: str | None = None,
) -> Envelope:
    """构造一条请求。"""
    return Envelope(
        type=type_,
        direction=DIR_REQUEST,
        mid=new_message_id(),
        payload=Payload(op=op or {}, condition=condition or {}, io=io or {}),
        ts=now_timestamp(),
        kind=kind,
        auth=auth,
    )


def make_response(
    type_: str,
    mid: str,
    *,
    output: Mapping[str, Any] | None = None,
    kind: str = "text",
) -> Envelope:
    """构造一条响应（与请求的 `mid` 关联）。"""
    return Envelope(
        type=type_,
        direction=DIR_RESPONSE,
        mid=mid,
        payload=Payload(output=output or {}),
        ts=now_timestamp(),
        kind=kind,
    )


def to_json(envelope: Envelope) -> bytes:
    """信封 → UTF-8 JSON 字节。

    字段顺序与分隔符都固定，所以同样的信封永远编出同样的字节，两端可直接比对。
    """
    text = json.dumps(envelope.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


def from_json(data: bytes) -> Envelope:
    """UTF-8 JSON 字节 → 信封。"""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"信封不是合法的 UTF-8 JSON: {exc}") from exc
    return Envelope.from_dict(raw)

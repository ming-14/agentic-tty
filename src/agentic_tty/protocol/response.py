"""响应约定：成功 / 失败的载荷形状。

两条边界的响应都长这样：

    output = {"ok": true,  "data": {...}}
    output = {"ok": false, "error": {"code": ..., "message": ..., ...}}

`code` 用**错误类名**，客户端据此分流，不靠文案匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .envelope import Envelope, make_response


@dataclass(frozen=True, slots=True)
class Failure:
    """失败的响应载荷。"""

    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def ok_response(
    type_: str, mid: str, data: dict[str, Any] | None = None, *, kind: str = "text"
) -> Envelope:
    """成功响应。"""
    return make_response(type_, mid, output={"ok": True, "data": data or {}}, kind=kind)


def failed_response(
    type_: str, mid: str, code: str, message: str, *, extra: dict[str, Any] | None = None
) -> Envelope:
    """失败响应。"""
    failure = Failure(code=code, message=message, extra=extra or {})
    return make_response(type_, mid, output={"ok": False, "error": failure.to_dict()})


def is_ok(envelope: Envelope) -> bool:
    return bool(envelope.payload.output.get("ok"))


def data_of(envelope: Envelope) -> dict[str, Any]:
    """取成功响应的数据体；不是成功响应就抛。"""
    output = envelope.payload.output
    if not output.get("ok"):
        raise ValueError(f"不是成功响应: {output.get('error')}")
    data = output.get("data")
    return data if isinstance(data, dict) else {}


def error_of(envelope: Envelope) -> Failure | None:
    """取失败响应的错误；不是失败响应返回 None。"""
    output = envelope.payload.output
    if output.get("ok"):
        return None
    raw = output.get("error")
    if not isinstance(raw, dict):
        return Failure(code="MalformedError", message="响应缺少 error 字段")
    code = str(raw.get("code") or "UnknownError")
    message = str(raw.get("message") or "")
    extra = {k: v for k, v in raw.items() if k not in ("code", "message")}
    return Failure(code=code, message=message, extra=extra)

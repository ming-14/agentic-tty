"""线协议错误。

对端可能送来任何东西（旧版本、被打断的流、干脆是别的程序连上来），因此这些
错误都是**可预期**的结果，不是内部缺陷——调用方拿到就应断开这条连接。
"""

from __future__ import annotations

from ..foundation.errors import AgenticTtyError


class ProtocolError(AgenticTtyError):
    """协议层错误基类。"""


class FrameError(ProtocolError):
    """帧格式不合法：未知帧类型、超长负载、越界的键长度。"""


class EnvelopeError(ProtocolError):
    """信封字段缺失或类型不对。"""


class MessageError(ProtocolError):
    """消息体内字段缺失或类型不对。

    与 `EnvelopeError` 分开只是为了定位方便——两者调用方都按 `ProtocolError` 兜住即可。
    """

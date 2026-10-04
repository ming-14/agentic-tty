"""传输层错误。"""

from ..foundation.errors import AgenticTtyError


class TransportError(AgenticTtyError):
    """传输层错误基类。"""


class AddressError(TransportError):
    """地址不合法：缺 scheme、端口不是数字。"""


class UnsupportedScheme(TransportError):
    """地址的 scheme 没有对应实现（比如还没接进来的共享内存）。"""


class ConnectionClosed(TransportError):
    """连接已断或对端关闭。

    刻意用异常而不是"返回空"来表示关闭：空返回值已经被"本轮无数据（超时）"占用，
    两者混同会让读写循环把"没了"当成"暂时没有"而空转。
    """

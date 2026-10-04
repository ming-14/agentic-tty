"""请求处理层的接缝：守护进程对它的全部要求。

守护进程只做"把消费者投进来的请求交给它、把它交出的答复回调给消费者"，**不认识请求和
答复里装的是什么**——所以这里的类型一律不透明：`object` 进、`object` 出；换请求处理层
不用动 `daemon` 的机制一行。

**所有方法都只在所有者线程上被调用**，实现里不需要任何锁。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Reply:
    """一条待交回消费者的答复。

    `request` 是它归属的**原始请求对象**（`handle` 收到的那个）。延迟答复必须带上它：
    消费者靠请求的身份把答复送回对的那条连接——`mid` 只在一条连接内才有意义，不能当键。

    `answer` 的内容守护进程一概不解释，原样交回。
    """

    request: object
    answer: object = None


class Delivery(StrEnum):
    """一条答复的投递结果——**回告给处理层**，它据此决定还要不要为这条请求产出下一段。

    订阅推送的节奏就建立在这上面：没被收下就别再 `pull`（游标停住），游标落后到裁剪点
    自然走重同步。**不需要客户端的 `ack`**——管道本身已经是有限缓冲，这一道闸就够了。
    """

    SENT = "sent"
    """已进连接的写缓冲；写线程保证最终写出（它宁可阻塞也不丢字节）。"""
    CONGESTED = "congested"
    """写缓冲满了，**没有收下**——先别再为这条请求产出，下一轮再试。"""
    GONE = "gone"
    """连接没了——这条请求的订阅作废。"""
    NOT_MINE = "not_mine"
    """不是本接入点发出的请求（进程内消费者投的）。守护进程自己另找去处，不转给处理层。"""


@runtime_checkable
class RequestHandler(Protocol):
    """请求处理层。"""

    def handle(self, request: object) -> Reply | None:
        """处理一条请求。

        返回 `None` 表示**已登记等待**，答复稍后由 `poll` 交出——等待不能在这里阻塞，
        否则一个慢等待会把所有会话冻住。`poll` 交出的延迟答复必须带上本方法收到的那个
        请求对象（`Reply.request`）。

        一条请求也可以**先回一条答复、之后再由 `poll` 交出后续**（订阅：先 ack，推送随后
        跟上）——它们都带同一个请求对象。
        """
        ...

    def poll(self) -> list[Reply]:
        """交出此刻已经可以回答的答复（可能为空）。"""
        ...

    def pending(self) -> int:
        """还有多少条请求压着等（draining 时靠它判断要不要等）。"""
        ...

    def failure(self, request: object, error: BaseException) -> Reply:
        """`handle` 抛了异常：把它变成一条明确的失败答复。

        守护进程不会替本层组答复（它不认识报文），但也不能让消费者干等，所以异常
        一律走这里。失败长什么样由本层决定。
        """
        ...

    def on_input(self, key: str, data: bytes) -> None:
        """接下一段上行字节；`key` 的语义由消费者定（如会话 uid）。"""
        ...

    def pump(self) -> None:
        """按轮推进（例如排空各会话的读桥、推进等待判定）。"""
        ...

    def wait(self, timeout: float) -> None:
        """空闲钩子：阻塞到"可能有活"，或超时。

        所有者循环没有别的事可等时调它——实现方在这里等自己的异步工作（如会话输出的
        唤醒通道），**有活就立刻返回**，别把整轮睡死。没有异步工作的实现直接睡过去即可。
        """
        ...

    def on_reply(self, request: object, delivery: Delivery) -> None:
        """一条答复的投递结果。**投订阅推送的节奏全靠它。**

        `CONGESTED` 时不要再为这条请求 `pull`（数据还在，下一轮重发）；`GONE` 时把这条
        请求的订阅丢掉。
        """
        ...

    def on_disconnected(self, connection: object) -> None:
        """一条连接没了：把挂在它上面的订阅全部注销。

        `connection` 就是 `handle` 收到的请求对象上带着的那个连接凭据——处理层只拿它当
        键，不认识它是什么。
        """
        ...

    def shutdown(self) -> None:
        """收尾：关掉手上的会话。

        可能长时间阻塞（宿主关闭在部分平台上要等数百秒），调用方会把它放在线程里
        并带超时。
        """
        ...

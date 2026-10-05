"""请求处理层的接缝：守护进程对它的全部要求。

守护进程只做"把消费者投进来的请求交给它、把它交出的答复回调给消费者"，**不认识请求和
答复里装的是什么**——所以这里的类型一律不透明：`object` 进、`object` 出；换请求处理层
不用动 `daemon` 的机制一行。

**除 `bind` 外，所有方法都只在所有者线程上被调用**，实现里不需要任何锁。
`bind` 是装配期的入住钩子（进循环之前调一次），不是运行期调用。
"""

from __future__ import annotations

from collections.abc import Callable
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

    **`CONGESTED` 的重发由守护进程负责**（它按请求攒着，连接一有空位就重投），处理层
    不必为同一段内容再产出一遍；处理层要做的只是**别在没被收下时把游标往前推**——
    那是订阅自己的事（游标停住，数据还在日志里）。
    """

    SENT = "sent"
    """已进连接的写缓冲；写线程保证最终写出（它宁可阻塞也不丢字节）。"""
    CONGESTED = "congested"
    """写缓冲满了，**没有收下**——守护进程会原样重投，处理层别再为这条请求产出下一段。"""
    GONE = "gone"
    """连接没了——这条请求的订阅作废。"""
    NOT_MINE = "not_mine"
    """不是本接入点发出的请求（进程内消费者投的）。守护进程自己另找去处，不转给处理层。"""


class StopSignal(Protocol):
    """要求守护进程停止的通道。

    处理层想停守护进程时只能调它——**它不能自己持有 `Daemon`**，否则机制与处理层反向
    依赖，接缝就废了。由守护进程在装配时注入（见 `bind`）。
    """

    def request_stop(self) -> None:
        """请求守护进程停止；只是置标志，不阻塞、不关连接。"""
        ...


@runtime_checkable
class RequestHandler(Protocol):
    """请求处理层。"""

    def bind(self, stop: StopSignal) -> None:
        """入住：接过守护进程给的停机通道。

        由守护进程在构造本层之后、进循环之前调用一次。刻意不做成构造参数——那样
        `handler_factory` 的签名（地址进、处理器出）就得跟着改。
        """
        ...

    def handle(self, request: object) -> Reply | None:
        """处理一条请求。

        返回 `None` 表示**已登记等待**，答复稍后由 `poll` 交出——等待不能在这里阻塞，
        否则一个慢等待会把所有会话冻住。`poll` 交出的延迟答复必须带上本方法收到的那个
        请求对象（`Reply.request`）。

        一条请求也可以**先回一条答复、之后再由 `poll` 交出后续**（订阅：先 ack，推送随后
        跟上）——它们都带同一个请求对象。
        """
        ...

    def poll(self, room_of: Callable[[object], int | None]) -> list[Reply]:
        """交出此刻已经可以回答的答复（可能为空）。

        `room_of(request)` 回答"这条请求所属的连接**此刻**还能收多少字节"——守护进程
        给的，处理层拿它当**这一轮的交付额度**：一条订阅可以在一轮里连交几帧，直到
        额度用光为止，而不是一轮一帧。返回 `None` 表示"问不出来"（进程内消费者、
        没有接入点），此时按"不限额"处理。

        额度是**上限不是保证**：真交出去时连接仍可能被写线程占满（额度只是快照），
        所以 `on_reply` 的 `CONGESTED` 依然是权威——处理层每交一帧后都要有办法收手。
        实现方的接口是"一轮尽量交完，交不动就停这一条订阅、其余照旧"。
        """
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
        请求的订阅丢掉。**响应型答复（非订阅）的重发不在这里**——守护进程会按请求攒着
        重投，处理层只处理自己那条推送流。
        """
        ...

    def owns_retransmission(self, request: object) -> bool:
        """这条请求的答复被拒时，**是不是由处理层自己留着重发**。

        订阅推送是：字节从游标里取出来就摆在 `out` 里，没收下就原样再交，守护进程
        不必替它攒。响应型答复不是：处理层手里没有它的第二份，所以拒了只能由守护进程
        攒着重投（否则客户端永远等不到那条 `mid`）。

        守护进程靠它分拣：**处理层负责的，拒了就不进守护进程的待重发表**——两边都攒
        就是重复投递（同一条订阅的几帧共用一个请求对象，在待重发表里还会互相顶掉）。
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

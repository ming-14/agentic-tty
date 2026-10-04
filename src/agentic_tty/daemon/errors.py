"""守护进程错误。"""

from ..foundation.errors import AgenticTtyError


class DaemonError(AgenticTtyError):
    """守护进程错误基类。"""


class AlreadyRunning(DaemonError):
    """单实例锁已被占用——已经有一个守护进程在跑。"""


class NotStarted(DaemonError):
    """在未启动的守护进程上调用了只对运行期有意义的方法。"""

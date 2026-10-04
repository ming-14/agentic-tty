"""openpty 伪终端（Linux）。

`os.forkpty()` 一次把 fork、`setsid`、开 slave、`login_tty`（取控制终端）与 dup2 都
做完——这几步全在 glibc 的 C 代码里，子进程只剩「设尺寸 → chdir → exec」三步 Python。

不用 `os.posix_spawn`：它没有 `cwd` 参数（file action 也只有 open / close / dup2，
没有 chdir），而「从哪个目录起」是会话描述的一部分。不用
`Popen(start_new_session=True, stdin=slave, ...)`：dup2 不会取得控制终端，交互式
shell 的 job control 会失效、Ctrl-C 传不到前台进程组。

进程树不归本模块管：Linux 侧由调用方按 `/proc` 枚举，本模块不认调用方的任何类型。
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import selectors
import struct
import termios
import time
from collections.abc import Mapping, Sequence

#: 挂在调用方的日志命名空间下——`foundation.logs.configure()` 只给 `agentic_tty`
#: 装 handler 且关掉了它的 propagate，挂到别处写出来的 INFO 会直接丢掉。
_logger = logging.getLogger("agentic_tty.openpty")

_CHILD_FAILED = 127
"""子进程准备失败或 exec 失败时的退出码，与 shell 的“command not found”同口径。"""

_REAP_TIMEOUT = 1.0
"""关闭时补收子进程的等待上限。"""


def _wait_readable(fd: int, timeout: float | None) -> bool:
    """等 fd 可读或超时；`timeout=None` 表示一直等。"""
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _read_failure(status_fd: int) -> str | None:
    """读 exec 上报管道：有内容即失败（内容为原因），EOF 即成功。"""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(status_fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace") or None


def _decode_status(status: int) -> int:
    """wait 状态 → 退出码；被信号打断的取负信号号（与 `Popen.returncode` 同口径）。"""
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _exec_child(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    cols: int,
    rows: int,
    status_fd: int,
) -> None:
    """子进程侧：只做 syscall 级的动作，失败经 `status_fd` 报给父进程。

    fork 之后只活下来本线程，别的线程持有的锁（日志、import）可能永远不释放，
    所以这里不写日志、不碰任何共享状态。
    """
    try:
        # 尺寸在 exec **之前**设：`os.forkpty()` 给 forkpty 传的 winsize 是 NULL，
        # 由子进程自己按 slave 设好，程序启动时读到的就是对的，没有竞态。
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if cwd:
            os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    except BaseException as exc:
        try:
            os.write(status_fd, f"{exc}".encode())
        except BaseException:
            pass
        os._exit(_CHILD_FAILED)


class OpenPty:
    """一个伪终端会话：slave 上的子进程 + master 这一侧。

    Args:
        cols: 初始列数。
        rows: 初始行数。
    """

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols = cols
        self._rows = rows
        self._pid: int | None = None
        self._master: int | None = None
        self._closed = False
        self._eof = False
        self._reaped = False
        self._exit_code: int | None = None

    # ── 生命周期 ────────────────────────────────────────────────

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> int:
        """起子进程，返回它的 pid。

        exec 成败经一条 CLOEXEC 管道同步回来：exec 成功时写端自动关闭（父进程读到
        EOF），失败时子进程往里写一句再退出。没有它，命令不存在只会变成一个「先进
        注册表、随即死掉」的会话——那正是核心层要避免的残骸。
        """
        if not argv:
            raise ValueError("argv 不能为空")
        if self._master is not None:
            raise RuntimeError("本实例已经启动过")

        argv_list = list(argv)
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        cols, rows = self._cols, self._rows

        status_r, status_w = os.pipe()  # 两个 fd 都带 CLOEXEC（PEP 446）
        try:
            pid, master = os.forkpty()
        except BaseException:
            os.close(status_r)
            os.close(status_w)
            raise
        if pid == 0:
            os.close(status_r)
            _exec_child(
                argv_list, cwd=cwd, env=child_env, cols=cols, rows=rows, status_fd=status_w
            )
            os._exit(_CHILD_FAILED)  # `_exec_child` 不返回；这行只是兜底
        os.close(status_w)

        failure = _read_failure(status_r)
        os.close(status_r)
        if failure:
            os.close(master)
            # 子进程刚报完错、正在退出；不等它一下就会留一个僵尸——这条路径上
            # `self._pid` 还没落，`close()` 的补收也够不着它
            os.waitpid(pid, 0)
            raise OSError(f"启动子进程失败 {argv_list[0]!r}: {failure}")

        self._pid = pid
        self._master = master
        _logger.info("openpty 会话已启动 pid=%s size=%dx%d", pid, cols, rows)
        return pid

    def try_wait(self) -> int | None:
        if self._closed or self._pid is None:
            return None
        if self._reaped:
            return self._exit_code
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:  # 已经被收过了
            self._reaped = True
            return None
        if pid == 0:
            return None
        self._reaped = True
        self._exit_code = _decode_status(status)
        return self._exit_code

    def poll_eof(self) -> bool:
        """本路是否已排空。

        与 Windows 侧不同，POSIX 的 pty 有**真 EOF**：slave 端全关之后 master 上的
        读会以 `EIO` 报出来，不需要按「退出后静默多久」猜。根进程退出但还有孙进程
        握着 slave 时不算排空——那时确实可能再来输出。
        """
        return self._closed or self._eof

    def close(self) -> None:
        """补收子进程并释放 master。子进程由调用方先终止（进程树）。"""
        if self._closed:
            return
        self._reap()
        self._closed = True
        master, self._master = self._master, None
        if master is not None:
            try:
                os.close(master)
            except OSError as exc:
                _logger.debug("关闭 master 失败: %s", exc)

    # ── I/O ────────────────────────────────────────────────────

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """读一段输出；超时返 `b""`（不代表 EOF，排空问 `poll_eof`）。"""
        master = self._master
        if self._closed or master is None:
            return b""
        if not _wait_readable(master, timeout):
            return b""
        try:
            data = os.read(master, max_bytes)
        except OSError as exc:
            if exc.errno == errno.EIO:  # slave 端全关
                self._eof = True
                return b""
            raise
        if not data:
            self._eof = True
        return data

    def write(self, data: bytes) -> None:
        """写输入。阻塞写：调用方把它放在专用写线程上。"""
        master = self._master
        if self._closed or master is None:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(master, view)
            except OSError as exc:
                if exc.errno == errno.EIO:  # slave 已关：不是错误
                    return
                raise
            view = view[written:]

    def resize(self, cols: int, rows: int) -> None:
        """改尺寸。`winsize` 的字段序是「先行后列」。"""
        master = self._master
        if self._closed or master is None:
            return
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._cols, self._rows = cols, rows

    # ── 内部 ───────────────────────────────────────────────────

    def _reap(self) -> None:
        """补收一次子进程。

        调用方是「先强杀、再非阻塞查退出码、最后关闭」，查的那一下与这里之间有个
        窗口：子进程可能刚好在那时退出。不补收它就一直是僵尸——本进程是它的父进程，
        没人替我们收。
        """
        if self._pid is None:
            return
        deadline = time.monotonic() + _REAP_TIMEOUT
        while not self._reaped:
            self.try_wait()
            if self._reaped or time.monotonic() >= deadline:
                return
            time.sleep(0.01)

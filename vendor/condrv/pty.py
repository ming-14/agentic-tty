"""ConDrv 直连的伪终端（Windows）。

不走 `CreatePseudoConsole` API：直接打开 `\\Device\\ConDrv\\Server`，自己拼 conhost
命令行、自己拼 HPCON，与 Windows Terminal 的 `winconpty.cpp` 同一条路径。区别在于
conhost 可以是自带的那份，而那个 API 只会起系统 `conhost.exe`。

进程树不归本模块管：作业句柄由调用方建好、经 `spawn(job_handle=...)` 传入，本模块
不认调用方的任何类型，也不反向依赖它。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import subprocess
import time
from collections.abc import Mapping, Sequence
from ctypes import wintypes as W
from pathlib import Path

from .win32 import (
    CREATE_UNICODE_ENVIRONMENT,
    ERROR_BROKEN_PIPE,
    EXTENDED_STARTUPINFO_PRESENT,
    FILE_SYNCHRONOUS_IO_NONALERT,
    GENERIC_ALL,
    GENERIC_READ,
    GENERIC_WRITE,
    HANDLE_FLAG_INHERIT,
    HPCON,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    PROC_THREAD_ATTRIBUTE_JOB_LIST,
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
    PROCESS_INFORMATION,
    PROCESS_QUERY_LIMITED_INFORMATION,
    PSEUDO_CONSOLE,
    PTY_SIGNAL_RESIZE_WINDOW,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    STILL_ACTIVE,
    SYNCHRONIZE,
    AttrList,
    CancelIoEx,
    CreatePipe,
    CreateProcessW,
    GetExitCodeProcess,
    OpenProcess,
    PeekNamedPipe,
    ReadFile,
    SetHandleInformation,
    WriteFile,
    close_handles,
    load_console_driver,
    open_nt_object,
)

#: 挂在调用方的日志命名空间下——`foundation.logs.configure()` 只给 `agentic_tty`
#: 装 handler 且关掉了它的 propagate，挂到别处写出来的 INFO 会直接丢掉。
_logger = logging.getLogger("agentic_tty.condrv")

#: 自带的 conhost 宿主。用系统 conhost.exe 的话 `CreatePseudoConsole` 就够了，
#: 走 ConDrv 直连的意义正在于换掉它。
_CONHOST_NAME = "OpenConsole.exe"

#: ConPTY 拿不到真 EOF：排空之后阻塞读永不返回，带超时的读只会等到超时返 b""，
#: 所以"读空"与"排空"是两回事。退出信号（进程句柄）又比最后一段输出早约 15ms
#: ——字节要经 conhost 中继，负载下能迟到 200ms 以上。于是排空只能按"退出之后
#: 静默多久"判定。
_DRAIN_QUIET = 0.3

#: 同步匿名管道的读句柄对 `WaitForSingleObject` 恒为有信号（没有挂起 I/O 即为有信号），
#: 拿它做超时是空转；而直接 `ReadFile` 又会一直阻塞到有数据——ConPTY 的输出管道写端
#: 由 conhost 握着，子进程退出也不会出现 EOF，于是永久卡住。只能轮询就绪字节数。
_POLL_INTERVAL = 0.002


def find_conhost() -> str:
    """定位自带的 conhost 宿主；缺失即报错，不退回系统 conhost。"""
    path = Path(__file__).resolve().parent / _CONHOST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"缺少 conhost 宿主 {path}（按 vendor/README.md 补齐）")
    return str(path)


class _Quiet:
    """按"最后一次读到字节"计静默。"""

    def __init__(self, quiet: float) -> None:
        self._quiet = quiet
        self._since: float | None = None

    def note_data(self) -> None:
        self._since = None

    def settled(self, exited: bool) -> bool:
        now = time.monotonic()
        if not exited:
            self._since = None
            return False
        if self._since is None:
            self._since = now
            return False
        return now - self._since >= self._quiet


def _open_server() -> int:
    """打开 ConDrv 的 server 设备；首次失败时请求加载驱动再试一次。"""
    device = r"\Device\ConDrv\Server"
    try:
        return open_nt_object(device, GENERIC_ALL, inheritable=True)
    except OSError as exc:
        _logger.debug("打开 %s 失败（%s），尝试加载控制台驱动", device, exc)
        load_console_driver()
        return open_nt_object(device, GENERIC_ALL, inheritable=True)


def _make_pipe() -> tuple[int, int]:
    read_end, write_end = W.HANDLE(), W.HANDLE()
    if not CreatePipe(ctypes.byref(read_end), ctypes.byref(write_end), None, 0):
        raise OSError(ctypes.get_last_error(), "CreatePipe 失败")
    return read_end.value, write_end.value


def _make_inheritable(handle: int) -> None:
    if not SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
        raise OSError(ctypes.get_last_error(), "SetHandleInformation 失败")


def _environment_block(env: Mapping[str, str] | None) -> ctypes.Array:
    """拼 Windows 环境块。键统一大写——Windows 环境变量名大小写不敏感，
    混用会在块里留下同名两项，取值变成先到先得。"""
    merged = {key.upper(): value for key, value in os.environ.items()}
    if env:
        merged.update({key.upper(): value for key, value in env.items()})
    text = "".join(f"{key}={value}\0" for key, value in sorted(merged.items())) + "\0"
    return ctypes.create_unicode_buffer(text)


class ConDrvPty:
    """一个 ConPTY 会话：conhost 宿主 + 与之绑定的子进程。

    Args:
        cols: 初始列数。
        rows: 初始行数。
    """

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols = cols
        self._rows = rows
        self._in_w: int | None = None
        self._out_r: int | None = None
        self._signal: int | None = None
        self._reference: int | None = None
        self._conhost: int | None = None
        self._child: int | None = None
        self._pseudo_console: PSEUDO_CONSOLE | None = None
        self._closed = False
        self._eof = False
        self._quiet = _Quiet(_DRAIN_QUIET)

    # ── 生命周期 ────────────────────────────────────────────────

    def open(self) -> None:
        """建 conhost 与伪终端——**不认子进程**。

        子进程由谁来起都行：自带路径见 `spawn()`；沙箱那边拿 `console` 给的 HPCON 起
        受限子进程，再 `adopt(pid)` 认领。
        """
        if self._in_w is not None:
            raise RuntimeError("本实例已经打开")

        conhost = find_conhost()
        in_r = in_w = out_r = out_w = sig_r = sig_w = None
        server = reference = None
        conhost_attrs: AttrList | None = None
        conhost_pi = PROCESS_INFORMATION()
        try:
            conhost_attrs = AttrList(1)

            in_r, in_w = _make_pipe()
            out_r, out_w = _make_pipe()
            _make_inheritable(in_r)
            _make_inheritable(out_w)

            server = _open_server()
            reference = open_nt_object(
                r"\Reference",
                GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE,
                parent=server,
                open_options=FILE_SYNCHRONOUS_IO_NONALERT,
            )

            sig_r, sig_w = _make_pipe()
            _make_inheritable(sig_r)

            conhost_pi = self._start_conhost(conhost, server, in_r, out_w, sig_r, conhost_attrs)
        except BaseException:
            close_handles(
                in_r, in_w, out_r, out_w, sig_r, sig_w, server, reference,
                conhost_pi.hProcess, conhost_pi.hThread,
            )
            raise
        finally:
            if conhost_attrs is not None:
                conhost_attrs.close()

        close_handles(server, in_r, out_w, sig_r, conhost_pi.hThread)
        pseudo = PSEUDO_CONSOLE()
        pseudo.hSignal = sig_w
        pseudo.hPtyReference = reference
        pseudo.hConPtyProcess = conhost_pi.hProcess

        self._in_w = in_w
        self._out_r = out_r
        self._signal = sig_w
        self._reference = reference
        self._conhost = conhost_pi.hProcess
        # 持有 HPCON 指向的那块内存：传给子进程的是它的地址，结构体被回收掉就悬空了
        self._pseudo_console = pseudo
        _logger.info(
            "ConDrv 伪终端已就绪 conhost=%s size=%dx%d",
            conhost_pi.dwProcessId,
            self._cols,
            self._rows,
        )

    @property
    def console(self) -> int:
        """HPCON 值——`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 要的是它本身，不是它的地址。"""
        if self._pseudo_console is None:
            raise RuntimeError("伪终端还没打开")
        return ctypes.cast(ctypes.pointer(self._pseudo_console), HPCON).value

    def adopt(self, pid: int) -> None:
        """认领一个**外部起的**子进程，`try_wait()` 才答得出退出码。

        只取只读类权限：沙箱子进程的默认 DACL 是**追加**授权（不替换），本进程仍查得到它。
        """
        if self._closed:
            raise RuntimeError("本实例已经关闭")
        if self._child is not None:
            raise RuntimeError("本实例已经认领过子进程")
        handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess 失败 pid={pid}")
        self._child = handle

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        job_handle: int | None = None,
    ) -> int:
        """起子进程（伪终端由 `open()` 先建好），返回它的 pid。

        `job_handle` 在**创建子进程时**经 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 生效，
        所以子进程一条指令都没执行就已经在作业里——创建后再
        `AssignProcessToJobObject` 有时间窗，期间 fork 出的孙进程会逃出去。
        """
        if not argv:
            raise ValueError("argv 不能为空")
        self.open()

        child_attrs: AttrList | None = None
        child_pi = PROCESS_INFORMATION()
        try:
            child_attrs = AttrList(2 if job_handle else 1)
            child_pi = self._start_child(
                argv,
                cwd=cwd,
                env=env,
                pseudo=self._pseudo_console,
                job_handle=job_handle,
                attrs=child_attrs,
            )
        except BaseException:
            close_handles(child_pi.hProcess, child_pi.hThread)
            self.close()  # 伪终端已建好：起子进程失败就一并收掉，不留半截
            raise
        finally:
            if child_attrs is not None:
                child_attrs.close()

        close_handles(child_pi.hThread)
        self._child = child_pi.hProcess
        _logger.info(
            "ConDrv 会话已启动 pid=%s size=%dx%d",
            child_pi.dwProcessId,
            self._cols,
            self._rows,
        )
        return child_pi.dwProcessId

    def try_wait(self) -> int | None:
        if self._closed or not self._child:
            return None
        code = W.DWORD(0)
        if not GetExitCodeProcess(self._child, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)

    def poll_eof(self) -> bool:
        """本路是否已排空：ConPTY 没有真 EOF，退出之后还要再静默一段才算。"""
        if self._closed or self._eof:
            return True
        return self._quiet.settled(self.try_wait() is not None)

    def close(self) -> None:
        """释放 conhost 与全部管道。子进程由调用方先终止（作业对象）。

        conhost 不必显式杀：它的存活靠引用句柄吊着，句柄一关它自己就退出。
        """
        if self._closed:
            return
        self._closed = True
        # 取消可能挂起的读（`timeout=None` 那条阻塞路径），别让读线程卡在 ReadFile 里
        if self._out_r:
            CancelIoEx(self._out_r, None)
        close_handles(self._conhost)
        self._conhost = None
        close_handles(self._out_r, self._in_w)
        self._out_r = self._in_w = None
        close_handles(self._signal, self._reference, self._child)
        self._signal = self._reference = self._child = None
        self._pseudo_console = None

    # ── I/O ────────────────────────────────────────────────────

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """读一段输出；超时返 `b""`（不代表 EOF，排空问 `poll_eof`）。"""
        out_r = self._out_r
        if self._closed or not out_r:
            return b""
        if timeout is None:
            return self._read_once(out_r, max_bytes)
        deadline = time.monotonic() + timeout
        while True:
            ready = self._ready_bytes(out_r)
            if ready is None:
                return b""
            if ready:
                return self._read_once(out_r, min(ready, max_bytes))
            if time.monotonic() >= deadline:
                return b""
            time.sleep(_POLL_INTERVAL)

    def _ready_bytes(self, out_r: int) -> int | None:
        """管道里就绪的字节数；写端已关返回 None。"""
        avail = W.DWORD(0)
        if not PeekNamedPipe(out_r, None, 0, None, ctypes.byref(avail), None):
            err = ctypes.get_last_error()
            if err == ERROR_BROKEN_PIPE:
                self._eof = True
                return None
            raise OSError(err, "PeekNamedPipe 失败")
        return avail.value

    def _read_once(self, out_r: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        got = W.DWORD(0)
        if not ReadFile(out_r, buf, size, ctypes.byref(got), None):
            err = ctypes.get_last_error()
            if err == ERROR_BROKEN_PIPE:
                self._eof = True
                return b""
            raise OSError(err, "ReadFile 失败")
        data = buf.raw[: got.value]
        if data:
            self._quiet.note_data()
        return data

    def write(self, data: bytes) -> None:
        if self._closed or not self._in_w:
            return
        written = W.DWORD(0)
        if not WriteFile(self._in_w, data, len(data), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "WriteFile 失败")

    def resize(self, cols: int, rows: int) -> None:
        """经信号管道改尺寸。

        包体是 `{PTY_SIGNAL_RESIZE_WINDOW, cols, rows}` 三个 `unsigned short`，共
        **6 字节**。用 `<Ihh` 之类会输出 8 字节，conhost 按前 6 字节解析会把 cols
        读成 0，子进程随即以 `0xC000013A` 崩掉。
        """
        if self._closed or not self._signal:
            return
        packet = struct.pack("<HHH", PTY_SIGNAL_RESIZE_WINDOW, cols, rows)
        written = W.DWORD(0)
        if not WriteFile(self._signal, packet, len(packet), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "写 resize 信号失败")
        self._cols, self._rows = cols, rows

    # ── 内部 ───────────────────────────────────────────────────

    def _start_conhost(
        self,
        conhost: str,
        server: int,
        in_r: int,
        out_w: int,
        sig_r: int,
        attrs: AttrList,
    ) -> PROCESS_INFORMATION:
        """起 conhost：把四个句柄经 `HANDLE_LIST` 精确地交出去。

        不走 `HANDLE_LIST` 而是靠 `bInheritHandles=TRUE` 全量继承的话，本进程其余
        可继承句柄（别的会话的管道）也会漏进 conhost。
        """
        cmdline = (
            f'"{conhost}" --headless --width {self._cols} --height {self._rows}'
            f" --signal 0x{sig_r:X} --server 0x{server:X}"
        )
        inherited = (W.HANDLE * 4)(server, in_r, out_w, sig_r)
        attrs.set(
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.byref(inherited), ctypes.sizeof(inherited)
        )

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = in_r
        si.StartupInfo.hStdOutput = out_w
        si.StartupInfo.hStdError = out_w
        si.lpAttributeList = attrs.ptr

        pi = PROCESS_INFORMATION()
        if not CreateProcessW(
            conhost,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            True,
            EXTENDED_STARTUPINFO_PRESENT,
            None,
            None,
            ctypes.byref(si.StartupInfo),
            ctypes.byref(pi),
        ):
            raise OSError(ctypes.get_last_error(), f"启动 conhost 失败: {cmdline}")
        return pi

    def _start_child(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        pseudo: PSEUDO_CONSOLE,
        job_handle: int | None,
        attrs: AttrList,
    ) -> PROCESS_INFORMATION:
        """起子进程并把它绑到 HPCON 上。

        `bInheritHandles=False`：控制台句柄由 ConPTY 驱动按属性分配，继承父进程的
        句柄反而会让 `isatty` 失真。
        """
        hpc = ctypes.cast(ctypes.pointer(pseudo), HPCON)
        # PSEUDOCONSOLE 的 lpValue 是 HPCON 值本身，不是它的地址
        attrs.set(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc, ctypes.sizeof(HPCON))
        if job_handle:
            jobs = (W.HANDLE * 1)(job_handle)
            attrs.set(PROC_THREAD_ATTRIBUTE_JOB_LIST, ctypes.byref(jobs), ctypes.sizeof(jobs))

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        # hStdInput / hStdOutput / hStdError 保持 0：带 STARTF_USESTDHANDLES 但三个句柄
        # 都不给，子进程才不会继承到本进程被重定向过的标准句柄（守护进程的 stderr 是日志
        # 文件），控制台句柄由 ConPTY 驱动分配。
        si.lpAttributeList = attrs.ptr

        cmdline = subprocess.list2cmdline(list(argv))
        pi = PROCESS_INFORMATION()
        if not CreateProcessW(
            None,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            False,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            _environment_block(env),
            cwd,
            ctypes.byref(si.StartupInfo),
            ctypes.byref(pi),
        ):
            raise OSError(ctypes.get_last_error(), f"启动子进程失败: {cmdline}")
        return pi

r"""本机管道传输：一个名字、多条连接、双向字节流。

**它不是网络**：Windows 用命名管道（`\\.\pipe\...`，内核对象，外部不可达），POSIX 用
`AF_UNIX`。两者都是本机 IPC，所以守护进程可以挂一个接入点让同机的消费者连进来，
而它并没有因此"实现网络功能"。

与 `tcp.py` 同形——差别只在"地址怎么变成一条通道"，上层拿到的永远只是一个 `Connection`：

    pipe://<名字>                    → 平台默认运行时目录
    pipe://<名字>/<运行时目录>        → 用给定目录

      Windows →  \\\\.\\pipe\\<名字>-<目录哈希>
      POSIX   →  <运行时目录>/<名字>.sock

**名字是完整名字**（如 `agentic-tty-daemon-test`）——前缀由 `config.constants.endpoint_name()`
加，transport 不认识命名习惯，只认完整名字。

目录由**地址**给出（`pipe_address`），所以锁 / 端点落在同一个目录里；同机多份
配置、多用户各自一份，互不相撞。

**一个名字上可以同时有多条连接**（Windows 命名管道叫"实例"，POSIX 是 accept 出的新
socket），每条是独立的一条双向字节流，互不干扰。这是"多个消费者连同一个守护进程"的
底座。
"""

from __future__ import annotations

import ctypes
import hashlib
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Protocol

from ..foundation.logs import get_logger
from .errors import ConnectionClosed, TransportError
from .stream import Address, Connection, Listener

_logger = get_logger("transport.pipe")

_POLL_INTERVAL = 0.005
"""recv 空转时的重试步长；也是"本轮无数据"的响应粒度。"""
_BUFFER = 1 << 16


def pipe_address(name: str, runtime_dir: Path | None = None) -> str:
    """拼一个管道地址。给了运行时目录就带上——**两端都用它算端点位置**。

    地址是端点位置的唯一来源：`pipe://<名字>` 用平台默认目录，`pipe://<名字>/<目录>`
    用给定目录。这样锁 / 端点才会落在同一个目录里。
    """
    if runtime_dir is None:
        return f"pipe://{name}"
    posix = Path(runtime_dir).as_posix()
    return f"pipe://{name}{posix}" if posix.startswith("/") else f"pipe://{name}/{posix}"


def _runtime_dir(address: Address) -> Path:
    """端点目录：**必须由地址给出**（`config.constants.runtime_dir()` 算出来，
    `pipe_address()` 拼进去）。

    不给就报错——猜平台默认目录，猜出来的未必是守护进程待的那个。
    """
    if not address.path:
        raise TransportError(f"管道地址必须给运行时目录: {address}")
    # URL 的路径一定带前导斜杠；Windows 上那会毁掉盘符路径（`/C:\x` 不是绝对路径）。
    text = address.path[1:] if sys.platform == "win32" else address.path
    return Path(text)


def pipe_path(address: Address) -> str:
    """把管道地址解析成本平台的实际端点——`listen` 与 `connect` 都走这里。"""
    name = address.netloc
    runtime_dir = _runtime_dir(address)
    if sys.platform == "win32":
        # 命名管道名是全局的、没有目录——把目录并进名字，好让同机多份配置互不相撞。
        digest = hashlib.sha256(str(runtime_dir).encode("utf-8")).hexdigest()[:16]
        return rf"\\.\pipe\{name}-{digest}"
    return str(runtime_dir / f"{name}.sock")


class _PipeIO(Protocol):
    """一条连接上的实际收发；平台差异全在这两个实现里。"""

    def read(self, max_bytes: int) -> bytes:
        """有数据就读一段；本轮无数据返回空；对端关闭抛 `ConnectionClosed`。"""
        ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...

    @property
    def peer(self) -> str: ...


class PipeConnection:
    """一条管道连接（对外形状与 `TcpConnection` 一致）。"""

    def __init__(self, io: _PipeIO) -> None:
        self._io = io
        self._closed = False

    @property
    def peer(self) -> str:
        return self._io.peer

    def recv(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed:
            raise ConnectionClosed(f"连接已关闭 peer={self.peer}")
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            data = self._io.read(max_bytes)
            if data:
                return data
            if deadline is not None and time.monotonic() >= deadline:
                return b""
            time.sleep(_POLL_INTERVAL)

    def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionClosed(f"连接已关闭 peer={self.peer}")
        self._io.write(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._io.close()


# ════════════════════════════════════════════════════════════════════
# Windows：命名管道
# ════════════════════════════════════════════════════════════════════

_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_BUSY = 231
_ERROR_IO_PENDING = 997
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_INFINITE = 0xFFFFFFFF
_INVALID_HANDLE = -1

if sys.platform == "win32":  # `ctypes.wintypes` 只在 Windows 上存在
    from ctypes import wintypes

    class _OVERLAPPED(ctypes.Structure):
        """重叠 I/O 的载体。**事件必须在调用之前挂上**，否则永远等不到。"""

        _fields_ = (
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )


_winapi: ctypes.WinDLL | None = None
"""kernel32 句柄。**只建一次**——原型声明长在实例上，建两个等于声明白费。"""


def _k32() -> ctypes.WinDLL:
    global _winapi
    if _winapi is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare_winapi(k32)
        _winapi = k32
    return _winapi


def _declare_winapi(k32: ctypes.WinDLL) -> None:
    """声明 Win32 原型：不声明的话指针参数会被截成 32 位。"""
    handle, dword, bool_ = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    ptr = ctypes.POINTER
    k32.CreateNamedPipeW.argtypes = (
        wintypes.LPCWSTR,
        dword,
        dword,
        dword,
        dword,
        dword,
        dword,
        ctypes.c_void_p,
    )
    k32.CreateNamedPipeW.restype = handle
    k32.ConnectNamedPipe.argtypes = (handle, ptr(_OVERLAPPED))
    k32.ConnectNamedPipe.restype = bool_
    k32.PeekNamedPipe.argtypes = (
        handle,
        ctypes.c_void_p,
        dword,
        ctypes.c_void_p,
        ptr(dword),
        ctypes.c_void_p,
    )
    k32.PeekNamedPipe.restype = bool_
    k32.ReadFile.argtypes = (handle, ctypes.c_void_p, dword, ptr(dword), ptr(_OVERLAPPED))
    k32.ReadFile.restype = bool_
    k32.WriteFile.argtypes = k32.ReadFile.argtypes
    k32.WriteFile.restype = bool_
    k32.GetOverlappedResult.argtypes = (handle, ptr(_OVERLAPPED), ptr(dword), bool_)
    k32.GetOverlappedResult.restype = bool_
    k32.WaitForSingleObject.argtypes = (handle, dword)
    k32.WaitForSingleObject.restype = dword
    k32.CreateEventW.argtypes = (ctypes.c_void_p, bool_, bool_, wintypes.LPCWSTR)
    k32.CreateEventW.restype = handle
    k32.CancelIoEx.argtypes = (handle, ptr(_OVERLAPPED))
    k32.CloseHandle.argtypes = (handle,)
    k32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        dword,
        dword,
        ctypes.c_void_p,
        dword,
        dword,
        handle,
    )
    k32.CreateFileW.restype = handle
    k32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, dword)
    k32.GetNamedPipeClientProcessId.argtypes = (handle, ptr(wintypes.ULONG))


def _close(handle: int) -> None:
    if handle and handle != _INVALID_HANDLE:
        _k32().CloseHandle(handle)


class _OverlappedOp:
    """一次重叠操作：句柄 + 事件。**事件必须在调用前就挂上**，否则等不到。"""

    def __init__(self) -> None:
        self.k32 = _k32()
        self.event = self.k32.CreateEventW(None, True, False, None)
        if not self.event:
            raise TransportError(f"建事件失败: {ctypes.get_last_error()}")
        self.overlapped = _OVERLAPPED()
        self.overlapped.hEvent = self.event

    def wait(self, timeout: float | None) -> bool:
        ms = _WAIT_INFINITE if timeout is None else max(0, int(timeout * 1000))
        return self.k32.WaitForSingleObject(self.event, ms) == _WAIT_OBJECT_0

    def close(self) -> None:
        self.k32.CloseHandle(self.event)


class _WinPipeIO:
    """Windows 命名管道句柄上的收发。

    句柄一律带 `FILE_FLAG_OVERLAPPED`——只有重叠 I/O 才做得出 accept 的超时。
    但 **recv 不走 CancelIoEx**：取消一次挂起的读再重发容易踩坑，改用
    `PeekNamedPipe` 先探可读字节，有才读；这样既不阻塞、也天然认得出对端关闭。
    """

    def __init__(self, handle: int, peer: str) -> None:
        self._handle = handle
        self._peer = peer
        self._closed = False

    @property
    def peer(self) -> str:
        return self._peer

    def read(self, max_bytes: int) -> bytes:
        k32 = _k32()
        available = wintypes.DWORD(0)
        if not k32.PeekNamedPipe(self._handle, None, 0, None, ctypes.byref(available), None):
            raise ConnectionClosed(f"对端已关闭 peer={self._peer}")
        if available.value == 0:
            return b""
        want = min(max_bytes, available.value)
        buf = ctypes.create_string_buffer(want)
        read = wintypes.DWORD(0)
        # 重叠句柄的 ReadFile **不能给 NULL 的 OVERLAPPED**——那样它会错误地报告完成
        op = _OverlappedOp()
        try:
            ok = k32.ReadFile(
                self._handle, buf, want, ctypes.byref(read), ctypes.byref(op.overlapped)
            )
            if not ok:
                if ctypes.get_last_error() != _ERROR_IO_PENDING or not op.wait(None):
                    raise ConnectionClosed(f"读取失败 peer={self._peer}")
                if not k32.GetOverlappedResult(
                    self._handle, ctypes.byref(op.overlapped), ctypes.byref(read), False
                ):
                    raise ConnectionClosed(f"读取失败 peer={self._peer}")
        finally:
            op.close()
        if read.value == 0:
            raise ConnectionClosed(f"对端已关闭 peer={self._peer}")
        return buf.raw[: read.value]

    def write(self, data: bytes) -> None:
        k32 = _k32()
        sent = 0
        while sent < len(data):
            chunk = data[sent:]
            buf = ctypes.create_string_buffer(chunk, len(chunk))
            written = wintypes.DWORD(0)
            op = _OverlappedOp()
            try:
                ok = k32.WriteFile(
                    self._handle,
                    buf,
                    len(chunk),
                    ctypes.byref(written),
                    ctypes.byref(op.overlapped),
                )
                if not ok:
                    if ctypes.get_last_error() != _ERROR_IO_PENDING or not op.wait(None):
                        raise ConnectionClosed(f"写入失败 peer={self._peer}")
                    if not k32.GetOverlappedResult(
                        self._handle, ctypes.byref(op.overlapped), ctypes.byref(written), False
                    ):
                        raise ConnectionClosed(f"写入失败 peer={self._peer}")
                if written.value == 0:
                    raise ConnectionClosed(f"写入失败 peer={self._peer}")
                sent += written.value
            finally:
                op.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close(self._handle)
        self._handle = 0


class _WinPipeListener:
    """Windows 命名管道监听点：一个名字，多条连接。"""

    def __init__(self, address: Address) -> None:
        self._path = pipe_path(address)
        self._address = address
        self._pending: int | None = None
        self._closed = False
        self._create_instance()

    @property
    def address(self) -> Address:
        return self._address

    def _create_instance(self) -> None:
        """建一个新实例并在其上等客户端。

        **交出一条就立刻建下一条**：名字上没有可用实例时，下一个客户端会拿到
        `ERROR_PIPE_BUSY` 而连不上。
        """
        handle = _k32().CreateNamedPipeW(
            self._path,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            _BUFFER,
            _BUFFER,
            0,
            None,
        )
        if handle == _INVALID_HANDLE:
            raise TransportError(f"建命名管道实例失败 {self._path}: {ctypes.get_last_error()}")
        self._pending = handle

    def accept(self, timeout: float | None = None) -> PipeConnection | None:
        if self._closed:
            raise ConnectionClosed("监听点已关闭")
        handle = self._pending
        assert handle is not None  # `_hand_off` / `_drop` 之后一定立刻建好下一个
        op = _OverlappedOp()
        try:
            if not _k32().ConnectNamedPipe(handle, ctypes.byref(op.overlapped)):
                error = ctypes.get_last_error()
                if error == _ERROR_PIPE_CONNECTED:  # 客户端在调用之前就等在那儿了
                    return self._hand_off(handle)
                if error != _ERROR_IO_PENDING:  # 实例坏了：丢掉重建，这轮算没有
                    self._drop(handle)
                    return None
                if not op.wait(timeout):
                    _k32().CancelIoEx(handle, ctypes.byref(op.overlapped))
                    self._drop(handle)
                    return None
        finally:
            op.close()
        return self._hand_off(handle)

    def _hand_off(self, handle: int) -> PipeConnection:
        """交出这条连接，并立刻把下一个实例备好。"""
        self._pending = None
        self._create_instance()
        return PipeConnection(_WinPipeIO(handle, _peer_of(handle)))

    def _drop(self, handle: int) -> None:
        self._pending = None
        _close(handle)
        self._create_instance()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pending is not None:
            _close(self._pending)
            self._pending = None


def _peer_of(handle: int) -> str:
    """对端标识（只用于日志）：客户端进程 pid。"""
    pid = wintypes.ULONG(0)
    if _k32().GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        return f"pid {pid.value}"
    return "pipe"


# ════════════════════════════════════════════════════════════════════
# POSIX：AF_UNIX
# ════════════════════════════════════════════════════════════════════


class _PosixPipeIO:
    """`AF_UNIX` socket 上的收发；非阻塞读，空转交给上层。"""

    def __init__(self, sock: socket.socket, peer: str) -> None:
        self._sock = sock
        self._peer = peer
        self._closed = False

    @property
    def peer(self) -> str:
        return self._peer

    def read(self, max_bytes: int) -> bytes:
        try:
            data = self._sock.recv(max_bytes)
        except (TimeoutError, BlockingIOError):
            return b""
        except OSError as exc:
            raise ConnectionClosed(f"读取失败 peer={self._peer}: {exc}") from exc
        if not data:
            raise ConnectionClosed(f"对端已关闭 peer={self._peer}")
        return data

    def write(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ConnectionClosed(f"写入失败 peer={self._peer}: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class _PosixPipeListener:
    """`AF_UNIX` 监听点：一个路径，多条连接。"""

    def __init__(self, path: str, address: Address) -> None:
        self._path = path
        self._address = address
        self._closed = False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).unlink(missing_ok=True)  # 上次异常退出会留下路径，绑不上
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(path)
            self._sock.listen(64)
        except OSError as exc:
            self._sock.close()
            raise TransportError(f"绑定 {path} 失败: {exc}") from exc
        _logger.info("开始监听 %s（%s）", address, path)

    @property
    def address(self) -> Address:
        return self._address

    def accept(self, timeout: float | None = None) -> PipeConnection | None:
        if self._closed:
            raise ConnectionClosed("监听点已关闭")
        try:
            self._sock.settimeout(timeout)
            sock, _ = self._sock.accept()
        except (TimeoutError, BlockingIOError):
            return None
        except OSError as exc:
            raise ConnectionClosed(f"接受连接失败: {exc}") from exc
        sock.setblocking(False)
        return PipeConnection(_PosixPipeIO(sock, _peer_of_posix(sock)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
        Path(self._path).unlink(missing_ok=True)


def _peer_of_posix(sock: socket.socket) -> str:
    """对端标识（只用于日志）：Linux 上取客户端 pid，取不到就退回 `unix`。"""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is not None:
        try:
            pid, _uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, option, 12))
            return f"pid {pid}"
        except OSError:
            pass
    return "unix"


# ════════════════════════════════════════════════════════════════════
# 传输实现
# ════════════════════════════════════════════════════════════════════


class PipeTransport:
    """本机管道传输。`listen` 给守护进程用，`connect` 给消费者用。"""

    scheme = "pipe"

    def listen(self, address: Address) -> Listener:
        name = address.netloc
        if not name:
            raise TransportError(f"管道地址必须给名字: {address}")
        if sys.platform == "win32":
            return _WinPipeListener(address)  # type: ignore[return-value]
        return _PosixPipeListener(pipe_path(address), address)  # type: ignore[return-value]

    def connect(self, address: Address, *, timeout: float = 5.0) -> Connection:
        if not address.netloc:
            raise TransportError(f"管道地址必须给名字: {address}")
        if sys.platform == "win32":
            return _connect_windows(address, timeout)
        return _connect_posix(address, timeout)


def _connect_windows(address: Address, timeout: float) -> Connection:
    """先等名字上有可用实例，再打开它。

    等到了也可能被别人抢走（`ERROR_PIPE_BUSY`），所以打开要重试到超时为止。
    """
    k32 = _k32()
    path = pipe_path(address)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        left = max(0.0, deadline - time.monotonic())
        if not k32.WaitNamedPipeW(path, max(0, int(left * 1000))):
            raise TransportError(f"连接 {address} 失败: {ctypes.get_last_error()}")
        handle = k32.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle != _INVALID_HANDLE:
            return PipeConnection(_WinPipeIO(handle, "pipe"))
        if ctypes.get_last_error() != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
            raise TransportError(f"连接 {address} 失败: {ctypes.get_last_error()}")
        time.sleep(_POLL_INTERVAL)


def _connect_posix(address: Address, timeout: float) -> Connection:
    path = pipe_path(address)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise TransportError(f"连接 {address} 失败: {exc}") from exc
    sock.setblocking(False)
    return PipeConnection(_PosixPipeIO(sock, _peer_of_posix(sock)))

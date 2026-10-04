"""ConDrv 直连所需的 Win32 / NT 声明。

只放声明与薄封装，不放流程——创建序列在 `pty.py`。

这些声明自成一份、不共用 agentic_tty 里的同名声明：本模块是被依赖方，不得反向
依赖调用方。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as W

_K = ctypes.WinDLL("kernel32", use_last_error=True)
_N = ctypes.WinDLL("ntdll")

# ── NT 结构 ──────────────────────────────────────────────────────


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", W.USHORT),
        ("MaximumLength", W.USHORT),
        ("Buffer", W.LPWSTR),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", W.ULONG),
        ("RootDirectory", W.HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", W.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_void_p),
    ]


# ── Win32 结构 ───────────────────────────────────────────────────


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", W.DWORD),
        ("lpReserved", W.LPWSTR),
        ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR),
        ("dwX", W.DWORD),
        ("dwY", W.DWORD),
        ("dwXSize", W.DWORD),
        ("dwYSize", W.DWORD),
        ("dwXCountChars", W.DWORD),
        ("dwYCountChars", W.DWORD),
        ("dwFillAttribute", W.DWORD),
        ("dwFlags", W.DWORD),
        ("wShowWindow", W.WORD),
        ("cbReserved2", W.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", W.HANDLE),
        ("hStdOutput", W.HANDLE),
        ("hStdError", W.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", W.HANDLE),
        ("hThread", W.HANDLE),
        ("dwProcessId", W.DWORD),
        ("dwThreadId", W.DWORD),
    ]


class PSEUDO_CONSOLE(ctypes.Structure):
    """ConPTY 的 HPCON 指向的实际结构（conhost 内部定义）。

    `CreatePseudoConsole` 不参与时得自己拼一个，子进程才能靠
    `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 附着上来。
    """

    _fields_ = [
        ("hSignal", W.HANDLE),
        ("hPtyReference", W.HANDLE),
        ("hConPtyProcess", W.HANDLE),
    ]


HPCON = ctypes.c_void_p

# ── 常量 ─────────────────────────────────────────────────────────

OBJ_CASE_INSENSITIVE = 0x00000040
OBJ_INHERIT = 0x00000002
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
GENERIC_ALL = 0x10000000
SYNCHRONIZE = 0x00100000

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

HANDLE_FLAG_INHERIT = 0x00000001

STARTF_USESTDHANDLES = 0x00000100
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

STILL_ACTIVE = 259
ERROR_BROKEN_PIPE = 109

# `ProcThreadAttributeValue(Number, Thread, Input, Additive)` 展开 =
# `Number | (Thread ? 0x10000) | (Input ? 0x20000) | (Additive ? 0x40000)`。
# 三个属性都是 Input 类，故高位恒为 0x20000。
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

# `PTY_SIGNAL_RESIZE_WINDOW`：写进信号管道的第一字段，见 `pty.ConDrvPty.resize`。
PTY_SIGNAL_RESIZE_WINDOW = 8

SYSTEM_CONSOLE_DRIVER_LOAD_INFORMATION = 132

# ── 函数绑定 ─────────────────────────────────────────────────────


def _kapi(name, restype, argtypes):
    fn = _K[name]
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


CreatePipe = _kapi(
    "CreatePipe",
    W.BOOL,
    [ctypes.POINTER(W.HANDLE), ctypes.POINTER(W.HANDLE), ctypes.c_void_p, W.DWORD],
)
SetHandleInformation = _kapi("SetHandleInformation", W.BOOL, [W.HANDLE, W.DWORD, W.DWORD])
CloseHandle = _kapi("CloseHandle", W.BOOL, [W.HANDLE])
ReadFile = _kapi(
    "ReadFile",
    W.BOOL,
    [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p],
)
WriteFile = _kapi(
    "WriteFile",
    W.BOOL,
    [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p],
)
PeekNamedPipe = _kapi(
    "PeekNamedPipe",
    W.BOOL,
    [
        W.HANDLE,
        ctypes.c_void_p,
        W.DWORD,
        ctypes.POINTER(W.DWORD),
        ctypes.POINTER(W.DWORD),
        ctypes.POINTER(W.DWORD),
    ],
)
WaitForSingleObject = _kapi("WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD])
CancelIoEx = _kapi("CancelIoEx", W.BOOL, [W.HANDLE, ctypes.c_void_p])
GetExitCodeProcess = _kapi("GetExitCodeProcess", W.BOOL, [W.HANDLE, ctypes.POINTER(W.DWORD)])

InitializeProcThreadAttributeList = _kapi(
    "InitializeProcThreadAttributeList",
    W.BOOL,
    [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)],
)
UpdateProcThreadAttribute = _kapi(
    "UpdateProcThreadAttribute",
    W.BOOL,
    [ctypes.c_void_p, W.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
     ctypes.c_void_p, ctypes.c_void_p],
)
DeleteProcThreadAttributeList = _kapi("DeleteProcThreadAttributeList", None, [ctypes.c_void_p])

CreateProcessW = _kapi(
    "CreateProcessW",
    W.BOOL,
    [
        W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, W.BOOL, W.DWORD,
        ctypes.c_void_p, W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ],
)

NtOpenFile = _N["NtOpenFile"]
NtOpenFile.restype = ctypes.c_long
NtOpenFile.argtypes = [
    ctypes.POINTER(W.HANDLE),
    W.ULONG,
    ctypes.POINTER(OBJECT_ATTRIBUTES),
    ctypes.POINTER(IO_STATUS_BLOCK),
    W.ULONG,
    W.ULONG,
]

NtSetSystemInformation = _N["NtSetSystemInformation"]
NtSetSystemInformation.restype = ctypes.c_long
NtSetSystemInformation.argtypes = [W.INT, ctypes.c_void_p, W.ULONG]


# ── 薄封装 ───────────────────────────────────────────────────────


def open_nt_object(
    device: str,
    desired_access: int,
    *,
    parent: int | None = None,
    inheritable: bool = False,
    open_options: int = 0,
) -> int:
    """`NtOpenFile` 打开设备对象，返回句柄；失败抛 `OSError`（NTSTATUS 在 errno 里）。"""
    flags = OBJ_CASE_INSENSITIVE | (OBJ_INHERIT if inheritable else 0)
    name_buf = ctypes.create_unicode_buffer(device)
    us = UNICODE_STRING(
        len(device) * 2,
        (len(device) + 1) * 2,
        ctypes.cast(name_buf, W.LPWSTR),
    )
    oa = OBJECT_ATTRIBUTES(
        ctypes.sizeof(OBJECT_ATTRIBUTES),
        parent,
        ctypes.pointer(us),
        flags,
        None,
        None,
    )
    iosb = IO_STATUS_BLOCK()
    handle = W.HANDLE()
    status = NtOpenFile(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(oa),
        ctypes.byref(iosb),
        FILE_SHARE_ALL,
        open_options,
    )
    if status != 0:
        raise OSError(status, f"NtOpenFile({device!r}) 失败: 0x{status & 0xFFFFFFFF:08X}")
    return handle.value


def load_console_driver() -> None:
    """请求内核加载控制台驱动（ConDrv 未加载时 `NtOpenFile` 会失败）。

    失败不抛：调用方重试 `NtOpenFile` 自会拿到最终结论，这里只是多给一次机会。
    """
    info = W.ULONG(1)
    NtSetSystemInformation(
        SYSTEM_CONSOLE_DRIVER_LOAD_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(W.ULONG),
    )


class AttrList:
    """`InitializeProcThreadAttributeList` 的缓冲区。

    `lpAttributeList` 必须与 `EXTENDED_STARTUPINFO_PRESENT` 同时给，否则
    `CreateProcessW` 会**忽略整张属性表**（句柄列表与作业列表一起失效）。
    """

    def __init__(self, count: int) -> None:
        size = ctypes.c_size_t(0)
        # 第一次只为取所需长度，按约定必然失败（ERROR_INSUFFICIENT_BUFFER）
        InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList 取长度失败")
        self._buf = ctypes.create_string_buffer(size.value)
        if not InitializeProcThreadAttributeList(self._buf, count, 0, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList 失败")

    @property
    def ptr(self) -> ctypes.c_void_p:
        return ctypes.cast(self._buf, ctypes.c_void_p)

    def set(self, attribute: int, value, size: int) -> None:
        """填入一个属性。

        `lpValue` 的传法按属性而异：`PSEUDOCONSOLE` 要传 **HPCON 值本身**，
        句柄列表与作业列表要传**指向句柄数组的指针**。
        """
        if not UpdateProcThreadAttribute(self._buf, 0, attribute, value, size, None, None):
            raise OSError(
                ctypes.get_last_error(),
                f"UpdateProcThreadAttribute 失败 attr=0x{attribute:08X}",
            )

    def close(self) -> None:
        if self._buf is not None:
            DeleteProcThreadAttributeList(self._buf)
            self._buf = None


def close_handles(*handles) -> None:
    """逐个关句柄，忽略空值与已失效的句柄。"""
    for handle in handles:
        if handle:
            CloseHandle(handle)

"""字节视图：从字节日志派生出的查询，按需生成、不额外维护状态。"""

from __future__ import annotations

import re

DEFAULT_ENCODING = "utf-8"


def decode(data: bytes, encoding: str = DEFAULT_ENCODING) -> str:
    """按视图需要解码；字节流本身仍是真源。"""
    return data.decode(encoding, errors="replace")


def read_all(data: bytes) -> bytes:
    return data


def read_range(data: bytes, start: int, end: int | None = None) -> bytes:
    return data[start:end]


def last_bytes(data: bytes, n: int) -> bytes:
    if n <= 0:
        return b""
    return data[-n:]


def last_lines(data: bytes, n: int, *, encoding: str = DEFAULT_ENCODING) -> str:
    """最后 N 行（含行尾）。"""
    if n <= 0:
        return ""
    lines = decode(data, encoding).splitlines(keepends=True)
    return "".join(lines[-n:])


def line_range(
    data: bytes,
    start: int,
    end: int | None = None,
    *,
    encoding: str = DEFAULT_ENCODING,
) -> str:
    """指定行范围（半开 `[start, end)`，含行尾）。"""
    lines = decode(data, encoding).splitlines(keepends=True)
    return "".join(lines[start:end])


def grep(
    data: bytes,
    pattern: str,
    *,
    encoding: str = DEFAULT_ENCODING,
    limit: int | None = None,
) -> list[str]:
    """按正则筛行。"""
    regex = re.compile(pattern)
    out: list[str] = []
    for line in decode(data, encoding).splitlines():
        if regex.search(line):
            out.append(line)
            if limit is not None and len(out) >= limit:
                break
    return out

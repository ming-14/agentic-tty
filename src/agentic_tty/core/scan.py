"""转义序列与多字节字符的边界扫描。

日志裁剪点与快照对齐点都必须落在"解析状态干净"的位置：从转义序列或
多字节字符中间切开再重放，会产生可见乱码（`3m` 这样的尾巴被当普通文本画出来）。

"干净边界"的定义：不在任何转义序列内部，也不落在多字节 UTF-8 字符中间。
"""

from __future__ import annotations

from collections.abc import Iterator

Buffer = bytes | bytearray

ESC = 0x1B
_BEL = 0x07
_ST = 0x5C  # '\\'；ST = ESC \
_CSI = 0x5B  # '['
_STRING_INTRO = (0x5D, 0x50, 0x58, 0x5E, 0x5F)  # ] P X ^ _（OSC/DCS/SOS/PM/APC）
_DESIGNATOR = (0x28, 0x29, 0x2A, 0x2B, 0x23)  # ( ) * + #（三字节序列）


def _csi_end(data: Buffer, i: int) -> int | None:
    """CSI：ESC [ 参数 中间字节 终止字节(0x40-0x7E)。"""
    j = i + 2
    n = len(data)
    while j < n:
        if 0x40 <= data[j] <= 0x7E:
            return j + 1
        j += 1
    return None


def _string_end(data: Buffer, i: int) -> int | None:
    """字符串型序列：ESC ] / P / X / ^ / _，到 BEL 或 ST 结束。"""
    j = i + 2
    n = len(data)
    while j < n:
        b = data[j]
        if b == _BEL:
            return j + 1
        if b == ESC:
            if j + 1 >= n:
                return None
            if data[j + 1] == _ST:
                return j + 2
            j += 2  # 字符串内部出现非 ST 的 ESC：按普通字节继续找
            continue
        j += 1
    return None


def sequence_end(data: Buffer, i: int) -> int | None:
    """`data[i]` 是 ESC；返回该转义序列结束后的下标；不完整返回 None。"""
    n = len(data)
    if i + 1 >= n:
        return None
    b = data[i + 1]
    if b == _CSI:
        return _csi_end(data, i)
    if b in _STRING_INTRO:
        return _string_end(data, i)
    if b in _DESIGNATOR:
        return i + 3 if i + 3 <= n else None
    return i + 2


def _utf8_len(b: int) -> int:
    if b < 0x80:
        return 1
    if b >> 5 == 0b110:
        return 2
    if b >> 4 == 0b1110:
        return 3
    if b >> 3 == 0b11110:
        return 4
    return 1  # 非法首字节：按单字节推进，不阻塞扫描


def iter_boundaries(data: Buffer) -> Iterator[int]:
    """产出所有干净边界（含 0 与末尾的干净位置）；尾部残缺则停止。"""
    n = len(data)
    yield 0
    i = 0
    while i < n:
        b = data[i]
        if b == ESC:
            end = sequence_end(data, i)
            if end is None:
                return
            i = end
        elif b < 0x80:
            i += 1
        else:
            length = _utf8_len(b)
            if i + length > n:
                return
            i += length
        yield i


def clean_offset_at_or_after(data: Buffer, offset: int) -> int:
    """返回 >= offset 的最小干净边界；找不到（尾部残缺）返回 len(data)。"""
    for boundary in iter_boundaries(data):
        if boundary >= offset:
            return boundary
    return len(data)


def replay_offset(data: Buffer) -> int:
    """尾部残缺序列/字符之前的最后一个干净边界（重建对齐点）。"""
    last = 0
    for boundary in iter_boundaries(data):
        last = boundary
    return last

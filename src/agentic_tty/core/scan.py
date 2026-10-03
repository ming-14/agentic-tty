"""转义序列与多字节字符的边界扫描。

"干净边界"= 不在任何转义序列内部，也不落在多字节 UTF-8 字符中间。从序列中间切开
再重放会产生可见乱码。

`clean_offset_at_or_after` / `replay_offset` 精确（与逐字节解析一致），把"整段普通
字节"用正则一次吃掉、序列结尾交给 C 层 `search`；`trim_cut_offset` 只给裁剪用，
**不保证最小**，因此能用一次 C 速扫描定界，门控不成立时退回精确实现。
"""

from __future__ import annotations

import re

Buffer = bytes | bytearray

ESC = 0x1B
_BEL = 0x07
_ST = 0x5C  # '\\'；ST = ESC \
_CSI = 0x5B  # '['
_STRING_INTRO = (0x5D, 0x50, 0x58, 0x5E, 0x5F)  # ] P X ^ _（OSC/DCS/SOS/PM/APC）
_DESIGNATOR = (0x28, 0x29, 0x2A, 0x2B, 0x23)  # ( ) * + #（三字节序列）

# ── C 速扫描用的模式 ────────────────────────────────────────────────────

# CSI 的终止字节、转义序列的结束点、以及普通文本字节，都落在 0x40-0x7E
_FINAL = re.compile(rb"[\x40-\x7e]")
# 字符串型序列的结束标记：BEL 或 ESC（ESC 后面是否跟 ST 由调用方判断）
_STR_END = re.compile(rb"[\x07\x1b]")
# 普通字节段：逐项复刻 `_utf8_len` 的跳法。ESC 不在单字节分支里，所以段边界上的
# ESC 会让匹配停下（那正是序列的起点）；被多字节字符吞掉的 ESC 仍由 [\s\S] 吃掉。
_TEXT = re.compile(
    rb"(?:[\x00-\x1a\x1c-\x7f]|[\x80-\xbf\xf8-\xff]"
    rb"|[\xc0-\xdf][\s\S]|[\xe0-\xef][\s\S]{2}|[\xf0-\xf7][\s\S]{3})+"
)
# 字符串型序列的开头（裁剪快路径的门控用）
_STR_INTRO = re.compile(rb"\x1b[\]PX^_]")

# 快路径最多向后看多少字节找结束点（找不到就退回精确实现）
_TRIM_WINDOW = 64
# 快路径最多跳过几个"紧跟 ESC 的候选"（畸形嵌套才会连续出现）
_TRIM_SKIPS = 4


def _csi_end(data: Buffer, i: int) -> int | None:
    """CSI：ESC [ 参数 中间字节 终止字节(0x40-0x7E)。"""
    m = _FINAL.search(data, i + 2)
    return None if m is None else m.start() + 1


def _string_end(data: Buffer, i: int) -> int | None:
    """字符串型序列：ESC ] / P / X / ^ / _，到 BEL 或 ST 结束。"""
    n = len(data)
    j = i + 2
    while j < n:
        m = _STR_END.search(data, j)
        if m is None:
            return None
        k = m.start()
        if data[k] == _BEL:
            return k + 1
        if k + 1 >= n:
            return None
        if data[k + 1] == _ST:
            return k + 2
        j = k + 2  # 字符串内部出现非 ST 的 ESC：按普通字节继续找
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


def _char_boundary(data: Buffer, p: int) -> int | None:
    """多字节字符跨过 p 时返回它的结束位置，否则 None（最长 4 字节，回看 3 字节够）。"""
    for s in range(max(0, p - 3), p):
        if data[s] >= 0x80:
            end = s + _utf8_len(data[s])
            if end > p:
                return end
    return None


def _string_intro_before(data: Buffer, hi: int) -> bool:
    """[0, hi) 里有没有字符串型序列的开头（OSC/DCS/SOS/PM/APC）。"""
    return _STR_INTRO.search(data, 0, hi) is not None


def clean_offset_at_or_after(data: Buffer, offset: int) -> int:
    """返回 >= offset 的最小干净边界；找不到（尾部残缺）返回 len(data)。

    只推进到 offset 为止：普通字节段用 `endpos=offset` 一次问出"最后一个不超过 offset
    的字符边界"，所以成本正比于 offset，而不是整个缓冲。
    """
    n = len(data)
    if offset <= 0:
        return 0
    if offset >= n:
        return n
    i = 0
    while i < n:
        if data[i] == ESC:
            end = sequence_end(data, i)
            if end is None:
                return n
            if end >= offset:
                return end
            i = end
            continue
        m = _TEXT.match(data, i, offset)
        b = i if m is None else m.end()
        if b >= offset:
            return offset  # 段干净地延伸到 offset，且 offset 正落在字符边界上
        if data[b] == ESC:
            i = b
            continue
        nxt = b + _utf8_len(data[b])  # offset 落在 b 处那个多字节字符内部
        return nxt if nxt <= n else n
    return n


def _trim_candidate(data: Buffer, offset: int, hi: int) -> int | None:
    """>= offset 的第一个可用候选字节（0x40-0x7E）。

    紧跟 ESC 的候选直接跳过：它是序列的第二字节或 CSI 引导符，落在哪儿要看更外层，
    局部判不了。跳过次数用尽就返回 None（交给精确实现）。
    """
    for _ in range(_TRIM_SKIPS):
        m = _FINAL.search(data, offset, hi)
        if m is None:
            return None
        if data[m.start() - 1] != ESC:
            return m.start()
        offset = m.start() + 1
    return None


def trim_cut_offset(data: Buffer, offset: int) -> int:
    """给"按长度裁剪"找一个干净边界：返回 >= offset 的**某个**干净边界。

    与 `clean_offset_at_or_after` 的唯一差别是**不保证最小**——最多晚
    `_TRIM_WINDOW` 字节（实际通常 0~2 字节）。换来的是定界只要一次 C 速扫描：
    逐序列解析在转义序列密集的输出（满屏重绘的 TUI）里是 ~155ns/字节。

    候选（`_trim_candidate`）在有门控的前提下只可能是文本字节、CSI 的终止字节、
    二/三字节序列的末字节，于是看 `p-1` 就能判定 `p` 自己是不是边界。门控（多字节字符
    跨过 p、区间里有字符串序列开头）不成立、或候选都用不了时，退回精确实现。
    """
    n = len(data)
    if offset <= 0:
        return 0
    if offset >= n:
        return n
    hi = min(n, offset + _TRIM_WINDOW)
    p = _trim_candidate(data, offset, hi)
    if p is None:
        # 没有可用候选：区间连 ESC 都没有的话，就只剩多字节对齐问题
        if data.find(ESC, 0, hi) < 0:
            end = _char_boundary(data, offset)
            return offset if end is None else min(end, n)
        return clean_offset_at_or_after(data, offset)
    if _char_boundary(data, p) is not None or _string_intro_before(data, p + 1):
        return clean_offset_at_or_after(data, offset)
    prev = data[p - 1]
    # p-1 是 0x40-0x7E 的 ASCII 时它必是文本字节或某个序列（含 CSI）的末字节 → p 就是边界；
    # 例外是 `ESC [` 的第二个字节，那是 CSI 引导符，p 是该 CSI 的终止字节。
    if 0x40 <= prev <= 0x7E and not (prev == _CSI and p >= 2 and data[p - 2] == ESC):
        return p
    return p + 1  # p-1 是控制字节 / CSI 参数 / 多字节字节 → 保守多裁一个字节


def replay_offset(data: Buffer) -> int:
    """尾部残缺序列/字符之前的最后一个干净边界（重建对齐点）。"""
    n = len(data)
    i = 0
    while i < n:
        if data[i] == ESC:
            end = sequence_end(data, i)
            if end is None:
                return i  # 残缺序列：最后一个边界是它的起点
            i = end
            continue
        m = _TEXT.match(data, i, n)
        if m is None:
            return i  # 段首字符就被截断
        end = m.end()
        if end == n or data[end] != ESC:
            return end  # 到尾 or 尾部是截断的多字节字符：到此为止
        i = end
    return i

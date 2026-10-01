from __future__ import annotations

import random

from agentic_tty.core import scan


def test_plain_text_boundaries():
    assert list(scan.iter_boundaries(b"abc")) == [0, 1, 2, 3]


def test_incomplete_csi_has_no_boundary_past_it():
    data = b"abc\x1b[3"
    assert list(scan.iter_boundaries(data)) == [0, 1, 2, 3]
    assert scan.replay_offset(data) == 3


def test_complete_csi_is_a_boundary():
    data = b"abc\x1b[31m"  # 共 8 字节，CSI 结束于 8
    assert list(scan.iter_boundaries(data)) == [0, 1, 2, 3, 8]
    assert scan.clean_offset_at_or_after(data, 4) == 8
    assert scan.replay_offset(data) == 8


def test_osc_terminated_by_bel():
    data = b"\x1b]0;title\x07x"
    assert scan.replay_offset(data) == len(data)


def test_osc_terminated_by_st():
    data = b"\x1b]0;title\x1b\\x"
    assert scan.replay_offset(data) == len(data)


def test_incomplete_multibyte_character():
    data = "ab中".encode()[:4]
    assert scan.replay_offset(data) == 2


def test_charset_designator_is_three_bytes():
    data = b"\x1b(Bx"
    assert scan.replay_offset(data) == len(data)


def test_clean_offset_at_or_after_beyond_end():
    assert scan.clean_offset_at_or_after(b"abc", 99) == 3


# ── 热路径与参考实现（iter_boundaries）的差分 / 性质测试 ────────────────


def _reference_at_or_after(data: bytes, offset: int) -> int:
    for boundary in scan.iter_boundaries(data):
        if boundary >= offset:
            return boundary
    return len(data)


def _reference_replay(data: bytes) -> int:
    last = 0
    for boundary in scan.iter_boundaries(data):
        last = boundary
    return last


_SEQUENCES = (
    b"\x1b[38;5;196m",
    b"\x1b[0m",
    b"\x1b[2J",
    b"\x1b[H",
    b"\x1b[?25l",
    b"\x1b[K",
    b"\x1b]0;title\x07",
    b"\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\",
    b"\x1bP1;2;3q dcs\x1b\\",
    b"\x1b(0",
    b"\x1b#8",
    b"\x1b",
    b"\x1b\x1b",
)
_TEXTS = ("中文输出", "plain text ", "\r\n", "aaa\n", "0123456789", "")


def _samples(seed: int, rounds: int) -> list[bytearray]:
    """合法终端流的随机拼装：合法 UTF-8 + 完整序列，可能被从任意位置截断。"""
    rng = random.Random(seed)
    out = []
    for _ in range(rounds):
        data = bytearray()
        for _ in range(rng.randrange(1, 8)):
            data += rng.choice(_SEQUENCES) if rng.random() < 0.5 else rng.choice(_TEXTS).encode()
        if rng.random() < 0.5 and data:
            del data[rng.randrange(len(data) + 1) :]
        out.append(data)
    return out


def test_hot_path_matches_reference_on_random_bytes():
    rng = random.Random(20261001)
    for _ in range(400):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 48)))
        for offset in range(-1, len(data) + 2):
            assert scan.clean_offset_at_or_after(data, offset) == _reference_at_or_after(
                data, offset
            )
        assert scan.replay_offset(data) == _reference_replay(data)


def test_hot_path_matches_reference_on_terminal_streams():
    for data in _samples(7, 800):
        for offset in range(-1, len(data) + 2):
            assert scan.clean_offset_at_or_after(data, offset) == _reference_at_or_after(
                data, offset
            )
        assert scan.replay_offset(data) == _reference_replay(data)


def test_trim_cut_offset_always_lands_on_a_clean_boundary():
    """裁剪只要"对齐序列边界"，不要求最小——但返回的位置必须真的干净。"""
    for data in _samples(11, 800):
        boundaries = set(scan.iter_boundaries(data))
        for offset in range(0, len(data) + 2):
            pos = scan.trim_cut_offset(data, offset)
            assert pos >= min(offset, len(data))
            assert pos <= len(data)
            # 要么落在干净边界上，要么是"找不到边界"的兜底（整段裁掉）
            assert pos in boundaries or pos == len(data), (bytes(data), offset, pos)


def _assert_clean_landing(data: bytes) -> None:
    boundaries = set(scan.iter_boundaries(data))
    for offset in range(len(data) + 1):
        pos = scan.trim_cut_offset(data, offset)
        assert pos in boundaries or pos == len(data), (data, offset, pos)


def test_trim_cut_offset_skips_a_csi_intro_byte():
    """'['(0x5B) 本身落在 0x40-0x7E 里，不能把 CSI 的引导符当成序列结束点。"""
    assert scan.trim_cut_offset(b"\x1b[31mabc", 1) == 5
    _assert_clean_landing(b"\x1b[2J\x1b[H\x1b[?25lplain ")


def test_trim_cut_offset_skips_candidates_right_after_esc():
    """紧跟 ESC 的候选（序列第二字节 / CSI 引导符）判不了归属，要往下跳再判。"""
    _assert_clean_landing(b"\x1b[\x1b[\x1b[?2")  # 嵌套未收尾的 CSI
    _assert_clean_landing(b"\x1b\x1b[\x1bP\x1b]0;title\x07")
    _assert_clean_landing(b"aaa\n\x1b\x1b0123456789\x1b[\r\nplain text ")
    _assert_clean_landing(b"\x1b[\x1b#8\r\naaa\naaa\n ")


def test_trim_cut_offset_gate_covers_osc_intro_second_byte():
    """门控必须覆盖 `\\x1b]` 的第二个字节，否则裁点会落进 OSC 载荷。"""
    data = b"\x1b]0;title\x07\x1b[2J"
    assert scan.trim_cut_offset(data, 1) == 10
    assert 10 in set(scan.iter_boundaries(data))

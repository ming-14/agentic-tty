from __future__ import annotations

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

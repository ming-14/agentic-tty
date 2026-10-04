import pytest

from agentic_tty.core.errors import OffsetAhead
from agentic_tty.core.journal import OutputJournal, Rebuild, Resume, plan_attach


def test_offsets_are_monotonic_from_zero():
    journal = OutputJournal(1024)
    assert (journal.start_offset, journal.end_offset) == (0, 0)
    journal.append(b"abc")
    assert (journal.start_offset, journal.end_offset) == (0, 3)
    journal.append(b"de")
    assert (journal.start_offset, journal.end_offset) == (0, 5)


def test_read_range_and_out_of_bounds():
    journal = OutputJournal(1024)
    journal.append(b"abcdef")
    assert journal.read(0) == b"abcdef"
    assert journal.read(2, 3) == b"cde"
    assert journal.read(10) == b""


def test_trim_keeps_budget_when_boundary_is_clean():
    journal = OutputJournal(8)
    journal.append(b"abcdef\x1b[31m")  # 11 字节；边界 0-6、11
    cut = journal.trim_to_budget()
    assert cut == 3
    assert journal.start_offset == 3
    assert journal.read(0) == b"def\x1b[31m"


def test_trim_does_not_cut_inside_escape_sequence():
    journal = OutputJournal(7)
    journal.append(b"aaaa\x1b[31mbbbb")  # 13 字节；边界 0-4、9-13
    cut = journal.trim_to_budget()
    assert cut == 9  # 抬到序列之后，绝不切开 ESC [ 31 m
    assert journal.start_offset == 9
    assert journal.read(0) == b"bbbb"


def test_plan_attach_fresh_subscriber():
    journal = OutputJournal(1024)
    journal.append(b"0123456789")
    assert plan_attach(journal, None) == Resume(0)


def test_plan_attach_in_range_resumes():
    journal = OutputJournal(1024)
    journal.append(b"0123456789")
    assert plan_attach(journal, 4) == Resume(4)


def test_plan_attach_ahead_raises():
    journal = OutputJournal(1024)
    journal.append(b"0123456789")
    with pytest.raises(OffsetAhead):
        plan_attach(journal, 11)


def test_plan_attach_rebuild_when_cursor_trimmed_away():
    journal = OutputJournal(4)
    journal.append(b"abcdefgh")
    journal.trim_to_budget()
    assert journal.start_offset > 0
    assert plan_attach(journal, 0) == Rebuild()


def test_plan_attach_fresh_subscriber_rebuilds_when_trimmed():
    journal = OutputJournal(4)
    journal.append(b"abcdefgh")
    journal.trim_to_budget()
    # 全新订阅者视作游标 0；0 已被裁剪 → 与落后游标一样走重建，不能静默丢头部
    assert plan_attach(journal, None) == Rebuild()

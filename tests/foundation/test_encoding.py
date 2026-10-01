from __future__ import annotations

from agentic_tty.foundation.encoding import StreamDecoder


def test_multibyte_character_split_across_chunks():
    decoder = StreamDecoder("utf-8")
    raw = "中文".encode()
    assert decoder.decode(raw[:1]) == ""
    assert decoder.decode(raw[1:4]) == "中"
    assert decoder.decode(raw[4:]) == "文"


def test_invalid_bytes_become_replacement_not_error():
    decoder = StreamDecoder("utf-8")
    assert decoder.decode(b"\xff") == "\ufffd"


def test_final_flushes_incomplete_prefix():
    decoder = StreamDecoder("utf-8")
    raw = "中".encode()
    assert decoder.decode(raw[:1]) == ""
    assert decoder.decode(b"", final=True) == "\ufffd"


def test_reset_discards_pending_state():
    decoder = StreamDecoder("utf-8")
    raw = "中".encode()
    assert decoder.decode(raw[:1]) == ""
    decoder.reset()
    assert "中" not in decoder.decode(raw[1:])

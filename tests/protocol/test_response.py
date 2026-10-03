"""响应约定：通用底座，两条边界共用。"""

from __future__ import annotations

import pytest

from agentic_tty.protocol.envelope import make_request
from agentic_tty.protocol.response import (
    data_of,
    error_of,
    failed_response,
    is_ok,
    ok_response,
)


def test_ok_response_roundtrips_through_data_of():
    response = ok_response("list_sessions", "m1", {"sessions": []})
    assert is_ok(response)
    assert error_of(response) is None
    assert data_of(response) == {"sessions": []}


def test_ok_response_without_data():
    response = ok_response("remove_session", "m1")
    assert data_of(response) == {}


def test_ok_response_carries_presentation_kind():
    response = ok_response("read_terminal", "m1", {"text": "hi"}, kind="screen")
    assert response.kind == "screen"


def test_failed_response_roundtrips_through_error_of():
    response = failed_response(
        "read_terminal", "m1", "SessionNotFound", "会话不存在", extra={"sid": "s1"}
    )
    assert not is_ok(response)
    failure = error_of(response)
    assert failure is not None
    assert failure.code == "SessionNotFound"
    assert failure.message == "会话不存在"
    assert failure.extra == {"sid": "s1"}


def test_data_of_rejects_failure():
    with pytest.raises(ValueError, match="不是成功响应"):
        data_of(failed_response("x", "m", "E", "boom"))


def test_error_of_tolerates_malformed_error_field():
    request = make_request("x")
    response = ok_response("x", request.mid)
    object.__setattr__(response.payload, "output", {"ok": False})
    failure = error_of(response)
    assert failure is not None
    assert failure.code == "MalformedError"

import json

import pytest

from agentic_tty.protocol.envelope import (
    DIR_REQUEST,
    DIR_RESPONSE,
    PROTO_VERSION,
    Envelope,
    Payload,
    from_json,
    make_request,
    make_response,
    to_json,
)
from agentic_tty.protocol.errors import EnvelopeError


def test_payload_groups_roundtrip():
    payload = Payload(op={"sid": "s1"}, condition={"timeout": 3}, output={"ok": True})
    assert Payload.from_dict(payload.to_dict()) == payload


def test_payload_defaults_every_group():
    assert Payload.from_dict(None) == Payload()
    restored = Payload.from_dict({"op": {"a": 1}})
    assert restored.op == {"a": 1}
    assert restored.condition == {}
    assert restored.output == {}
    assert restored.io == {}


def test_request_carries_id_and_timestamp():
    request = make_request("read_terminal", op={"sid": "s1"})
    assert request.direction == DIR_REQUEST
    assert request.proto == PROTO_VERSION
    assert request.mid
    assert request.ts
    assert request.payload.op == {"sid": "s1"}
    assert request.payload.condition == {}


def test_response_reuses_request_mid():
    request = make_request("list_sessions")
    response = make_response("list_sessions", request.mid, output={"ok": True})
    assert response.direction == DIR_RESPONSE
    assert response.mid == request.mid


def test_json_roundtrip_preserves_everything():
    request = make_request(
        "input_into_terminal", op={"sid": "s1"}, condition={"idle": 0.5}, kind="screen"
    )
    assert from_json(to_json(request)) == request


def test_json_is_compact_and_utf8():
    request = make_request("x", op={"cwd": "C:/用户", "n": 1})
    wire = to_json(request)
    assert "用户".encode() in wire
    assert b" " not in wire
    assert json.loads(wire.decode("utf-8"))["payload"]["op"]["cwd"] == "C:/用户"


def test_wire_keys_match_documented_names():
    raw = json.loads(to_json(make_request("x")).decode("utf-8"))
    assert set(raw) == {"proto", "dir", "type", "mid", "ts", "kind", "auth", "payload"}
    assert set(raw["payload"]) == {"op", "condition", "output", "io"}


def test_auth_defaults_to_null_on_the_wire():
    assert json.loads(to_json(make_request("x")).decode("utf-8"))["auth"] is None


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({}, "proto"),
        ({"proto": "1"}, "proto"),
        ({"proto": PROTO_VERSION + 1, "dir": "request", "type": "t", "mid": "m"}, "版本不匹配"),
        ({"proto": PROTO_VERSION}, "方向"),
        ({"proto": PROTO_VERSION, "dir": "nope", "type": "t", "mid": "m"}, "方向"),
        ({"proto": PROTO_VERSION, "dir": "request", "mid": "m"}, "type"),
        ({"proto": PROTO_VERSION, "dir": "request", "type": "", "mid": "m"}, "type"),
        ({"proto": PROTO_VERSION, "dir": "request", "type": "t"}, "mid"),
        ({"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": ""}, "mid"),
        (
            {"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": "m", "auth": 5},
            "auth",
        ),
        (
            {"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": "m", "ts": 123},
            "ts",
        ),
        (
            {"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": "m", "kind": []},
            "kind",
        ),
        (
            {"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": "m", "payload": []},
            "payload",
        ),
        (
            {
                "proto": PROTO_VERSION,
                "dir": "request",
                "type": "t",
                "mid": "m",
                "payload": {"op": []},
            },
            "op",
        ),
    ],
)
def test_malformed_envelope_is_rejected(raw, match):
    with pytest.raises(EnvelopeError, match=match):
        Envelope.from_dict(raw)


def test_non_json_payload_is_rejected():
    with pytest.raises(EnvelopeError, match="合法"):
        from_json(b"not json")


def test_invalid_utf8_is_rejected():
    with pytest.raises(EnvelopeError, match="合法"):
        from_json(b"\xff\xfe")


def test_missing_optional_fields_fall_back_to_defaults():
    envelope = Envelope.from_dict(
        {"proto": PROTO_VERSION, "dir": "request", "type": "t", "mid": "m"}
    )
    assert envelope.kind == "text"
    assert envelope.auth is None
    assert envelope.ts == ""
    assert envelope.payload == Payload()

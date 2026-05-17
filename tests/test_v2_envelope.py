"""PR#1 testovi za clade.envelope.Envelope (v2.0 protokol)."""

import pytest
from pydantic import ValidationError

from clade.envelope import Envelope


def test_new_generates_msg_id_nonce_timestamp():
    """Envelope.new() popunjava obavezna auto-polja."""
    e = Envelope.new(from_agent="alice", to_agent="bob", kind="send", payload={"x": 1})
    assert e.msg_id
    assert len(e.msg_id) == 36  # uuid4 standard
    assert e.nonce
    assert len(e.nonce) == 32  # secrets.token_hex(16) = 32 hex chars
    assert e.timestamp_ms > 1_700_000_000_000  # >2023
    assert e.protocol_version == "2.0.0"
    assert e.hmac is None  # opcioni za on-host
    assert e.correlation_id is None
    assert e.thread_id is None
    assert e.reply_to is None


def test_thread_id_reply_to_are_top_level():
    """thread_id i reply_to su top-level polja u v2.0, ne payload._meta."""
    e = Envelope.new(
        from_agent="alice", to_agent="bob", kind="send",
        payload={"text": "hi"}, thread_id="t-1", reply_to="msg-123",
    )
    assert e.thread_id == "t-1"
    assert e.reply_to == "msg-123"
    # Payload NE sadrzi _thread_id ili _reply_to (top-level, ne meta)
    assert "_thread_id" not in e.payload
    assert "_reply_to" not in e.payload


def test_extra_fields_rejected():
    """extra='forbid' u model_config — nepoznata polja → ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        Envelope(
            msg_id="m1", from_agent="alice", to_agent="bob", kind="send",
            payload={}, nonce="n1", timestamp_ms=1,
            unknown_field="boom",  # type: ignore[call-arg]
        )
    assert "unknown_field" in str(exc_info.value)


def test_invalid_kind_rejected():
    """kind je Literal['send','ask','reply'] — drugo pada validation."""
    with pytest.raises(ValidationError):
        Envelope(
            msg_id="m1", from_agent="alice", to_agent="bob",
            kind="evil",  # type: ignore[arg-type]
            payload={}, nonce="n1", timestamp_ms=1,
        )


def test_roundtrip_json_bytes():
    """to_json_bytes / from_json_bytes preserve sva polja."""
    orig = Envelope.new(
        from_agent="alice", to_agent="bob", kind="ask",
        payload={"question": "7*8"},
        correlation_id="corr-1", thread_id="t-1", reply_to="parent-msg",
    )
    orig_hmac = "deadbeef" * 8
    orig = orig.model_copy(update={"hmac": orig_hmac})

    raw = orig.to_json_bytes()
    assert isinstance(raw, bytes)
    rt = Envelope.from_json_bytes(raw)
    assert rt.msg_id == orig.msg_id
    assert rt.from_agent == "alice"
    assert rt.to_agent == "bob"
    assert rt.kind == "ask"
    assert rt.payload == {"question": "7*8"}
    assert rt.nonce == orig.nonce
    assert rt.timestamp_ms == orig.timestamp_ms
    assert rt.hmac == orig_hmac
    assert rt.correlation_id == "corr-1"
    assert rt.thread_id == "t-1"
    assert rt.reply_to == "parent-msg"
    assert rt.protocol_version == "2.0.0"


def test_from_json_bytes_rejects_invalid():
    """Invalid JSON → ValidationError (ne tihi None)."""
    with pytest.raises(ValidationError):
        Envelope.from_json_bytes(b'{"msg_id": "x"}')  # missing required fields


def test_payload_default_empty_dict():
    """Default payload je prazan dict, ne None."""
    e = Envelope.new(from_agent="a", to_agent="b", kind="send")
    assert e.payload == {}

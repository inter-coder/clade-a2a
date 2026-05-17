"""v2.2.0 unit testovi za clade.hmac sign + verify."""

import pytest

from clade.envelope import Envelope
from clade.hmac import canonical_payload, sign, verify

SECRET = "deadbeef" * 8  # 64 hex chars
OTHER_SECRET = "cafebabe" * 8


def test_canonical_payload_deterministic():
    """sort_keys + bez whitespace-a — deterministicki za isti dict."""
    p1 = canonical_payload({"b": 2, "a": 1, "c": [3, 4]})
    p2 = canonical_payload({"a": 1, "c": [3, 4], "b": 2})
    assert p1 == p2
    assert " " not in p1  # no whitespace


def test_canonical_payload_handles_utf8():
    """ensure_ascii=False — utf8 ostaje kao text."""
    text = canonical_payload({"text": "Šta"})
    assert "Šta" in text


def test_sign_verify_roundtrip():
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send",
                       payload={"text": "test"})
    env_signed = env.model_copy(update={"hmac": sign(env, SECRET)})
    assert verify(env_signed, SECRET) is True


def test_sign_with_correlation_id():
    """correlation_id ulazi u canonical message (za ask/reply parove)."""
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="ask",
                       payload={"q": "?"}, correlation_id="c-1")
    sig = sign(env, SECRET)
    env_signed = env.model_copy(update={"hmac": sig})
    assert verify(env_signed, SECRET)

    # Razlicit correlation_id → razlicit hmac
    env2 = env.model_copy(update={"correlation_id": "c-2", "hmac": sig})
    assert verify(env2, SECRET) is False


def test_verify_fails_on_wrong_secret():
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
    env_signed = env.model_copy(update={"hmac": sign(env, SECRET)})
    assert verify(env_signed, OTHER_SECRET) is False


def test_verify_fails_on_tampered_payload():
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send",
                       payload={"text": "original"})
    env_signed = env.model_copy(update={"hmac": sign(env, SECRET)})

    # Tamper payload bez resign-a
    tampered = env_signed.model_copy(update={"payload": {"text": "evil"}})
    assert verify(tampered, SECRET) is False


def test_verify_fails_on_missing_hmac():
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
    # env.hmac is None by default
    assert verify(env, SECRET) is False


def test_verify_fails_on_invalid_secret_hex():
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
    env_signed = env.model_copy(update={"hmac": sign(env, SECRET)})
    # Garbage secret_hex → ValueError u bytes.fromhex
    with pytest.raises(ValueError):
        verify(env_signed, "not-hex-garbage!!")


def test_payload_order_irrelevant():
    """Dva razlicita key-order dict-a → isti HMAC (canonical sort)."""
    env1 = Envelope.new(from_agent="a", to_agent="b", kind="send",
                        payload={"a": 1, "b": 2, "c": 3})
    env2_payload = {"c": 3, "a": 1, "b": 2}
    env2 = Envelope(
        msg_id=env1.msg_id, from_agent="a", to_agent="b", kind="send",
        payload=env2_payload, nonce=env1.nonce, timestamp_ms=env1.timestamp_ms,
    )
    assert sign(env1, SECRET) == sign(env2, SECRET)

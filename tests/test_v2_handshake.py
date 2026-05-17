"""PR#6 testovi za protocol version handshake (§2.10)."""

import pytest

from clade.envelope import (
    PROTOCOL_MAJOR,
    PROTOCOL_VERSION,
    Envelope,
    check_protocol_compat,
    parse_major,
)


def test_protocol_constants_consistent():
    """PROTOCOL_VERSION i PROTOCOL_MAJOR su u sinhronu."""
    assert parse_major(PROTOCOL_VERSION) == PROTOCOL_MAJOR


def test_parse_major_basic():
    assert parse_major("2.0.0") == 2
    assert parse_major("2.5.13") == 2
    assert parse_major("1.0.0") == 1
    assert parse_major("3.0.0") == 3
    assert parse_major("invalid") is None
    assert parse_major("") is None
    assert parse_major("v2.0.0") is None  # leading v not supported


def test_check_protocol_compat_same_major():
    """Iste major verzije prihvatamo bez obzira na minor/patch."""
    ok, err = check_protocol_compat("2.0.0")
    assert ok is True
    assert err is None
    ok, err = check_protocol_compat("2.5.7")  # loose minor/patch
    assert ok is True


def test_check_protocol_compat_old_major():
    """v1.x.y → reject."""
    ok, err = check_protocol_compat("1.2.0")
    assert ok is False
    assert "protocol_mismatch" in err  # type: ignore[operator]
    assert "1.2.0" in err  # type: ignore[operator]


def test_check_protocol_compat_future_major():
    """v3.x.y → reject (peer noviji od nas)."""
    ok, err = check_protocol_compat("3.0.0")
    assert ok is False
    assert "protocol_mismatch" in err  # type: ignore[operator]


def test_check_protocol_compat_invalid_format():
    ok, err = check_protocol_compat("garbage")
    assert ok is False
    assert "invalid protocol_version" in err  # type: ignore[operator]


def test_envelope_default_protocol_version():
    """Novi Envelope.new() dobija trenutnu PROTOCOL_VERSION."""
    e = Envelope.new(from_agent="a", to_agent="b", kind="send")
    assert e.protocol_version == PROTOCOL_VERSION

"""HMAC sign + verify za cross-host relay transport.

Portovano iz v1 agent/main.py canonical-payload pristupa. Deterministicki
JSON serijalization (sort_keys, no whitespace) → SHA256 HMAC sa shared
hex secret-om.

**Kada se koristi:**
- transport=relay: HMAC required (relay je polu-poverljiv, T3 §9)
- transport=unix (on-host): HMAC opcioni — filesystem permissions guard

Oba peer-a u paru moraju imati IDENTICAN `secret_hex` u peers.yaml pod
uzajamnim kljucevima. `clade init` generise pairwise (u v2.2.0 wizard
cross-host mode)."""

from __future__ import annotations

import hashlib
import hmac as _hmac_module
import json

from clade.envelope import Envelope


def canonical_payload(payload: dict) -> str:
    """Deterministicka JSON serijalizacija za HMAC. Oba peer-a MORAJU koristiti istu."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_message(env: Envelope) -> bytes:
    """Sastavi canonical bytes za HMAC. Format isti kao v1 (vidi protokol §3)."""
    parts = [
        env.msg_id, env.from_agent, env.to_agent, env.kind,
        canonical_payload(env.payload), env.nonce, str(env.timestamp_ms),
    ]
    if env.correlation_id:
        parts.append(env.correlation_id)
    return "|".join(parts).encode("utf-8")


def sign(envelope: Envelope, secret_hex: str) -> str:
    """Vrati hex SHA256 HMAC za envelope. Caller postavi `envelope.hmac = sign(...)`."""
    secret = bytes.fromhex(secret_hex)
    return _hmac_module.new(secret, _canonical_message(envelope), hashlib.sha256).hexdigest()


def verify(envelope: Envelope, secret_hex: str) -> bool:
    """Constant-time provera. Vraca False ako envelope.hmac fali ili ne odgovara."""
    if not envelope.hmac:
        return False
    expected = sign(envelope, secret_hex)
    return _hmac_module.compare_digest(expected, envelope.hmac)

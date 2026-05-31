"""v1.18.0 — pure wire-level helpers (HMAC sign/verify + canonical payload).

Extracted from agent/main.py so other code paths (e.g. clade_cli/web_sender.py)
can sign/verify without triggering agent.main's module-level config load.
agent/main.py re-exports these to keep its public surface unchanged.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json


def canonical_payload(payload: dict) -> str:
    """Deterministic JSON serialization for HMAC. Both peers MUST use this exact form."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(secret_hex: str, msg_id: str, from_a: str, to_a: str, kind: str,
         payload: dict, nonce: str, ts_ms: int, correlation_id: str | None = None) -> str:
    secret = bytes.fromhex(secret_hex)
    parts = [msg_id, from_a, to_a, kind, canonical_payload(payload), nonce, str(ts_ms)]
    if correlation_id:
        parts.append(correlation_id)
    msg = "|".join(parts).encode("utf-8")
    return hmac_module.new(secret, msg, hashlib.sha256).hexdigest()


def verify(secret_hex: str, env: dict) -> bool:
    expected = sign(
        secret_hex,
        env["msg_id"],
        env["from_agent"],
        env["to_agent"],
        env["kind"],
        env["payload"],
        env["nonce"],
        env["timestamp_ms"],
        env.get("correlation_id"),
    )
    return hmac_module.compare_digest(expected, env["hmac"])

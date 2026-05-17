"""Envelope — single source of truth za v2.0 protokol.

Promene u odnosu na v1.x (`agent.main._make_envelope`):
- `thread_id` i `reply_to` su **TOP-LEVEL** polja (ne `payload._thread_id`).
  Cisce, type-checkable, ne mesa metadata sa user content-om.
- `protocol_version` polje (default "2.0.0") — strict major match handshake (§2.10).
- `hmac` opcioni (None) za on-host unix socket transport — required samo za
  cross-host kroz relay (`--remote`).
- `kind` je `Literal["send", "ask", "reply"]` (pydantic enforce-uje).
- `extra="forbid"`: nepoznata polja → ValidationError (ne tihi drop).

Pydantic je vec u dependency tree (relay/main.py:Envelope). Ne uvodimo msgspec
da ne bi imali dva validation libra (samozapazanja open question #3)."""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Trenutna verzija protokola. Strict major match handshake po §2.10:
# - v2.x.y prihvata bilo koji v2.*.* envelope (loose minor/patch)
# - v1.x.y i v3.x.y odbija
PROTOCOL_VERSION = "2.0.0"
PROTOCOL_MAJOR = 2


def parse_major(version: str) -> int | None:
    """Vrati major component verzije ('2.1.5' → 2). None ako parse fail."""
    parts = version.split(".")
    if not parts or not parts[0].isdigit():
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def check_protocol_compat(received: str) -> tuple[bool, str | None]:
    """Vrati (ok, error_msg). ok=True znaci kompatibilan major.
    error_msg ima `expected/received` format pogodan za 426 odgovor."""
    received_major = parse_major(received)
    if received_major is None:
        return False, f"invalid protocol_version format: '{received}'"
    if received_major != PROTOCOL_MAJOR:
        return False, (
            f"protocol_mismatch: this clade expects v{PROTOCOL_MAJOR}.x.y, "
            f"received '{received}'. Upgrade peer to clade>=2.0."
        )
    return True, None


class Envelope(BaseModel):
    """Poruka koja putuje kroz Transport sloj. Immutable po konvenciji
    (pydantic ne forsira frozen, ali ne menjamo polja posle konstrukcije)."""

    model_config = ConfigDict(extra="forbid")

    msg_id: str
    from_agent: str
    to_agent: str
    kind: Literal["send", "ask", "reply"]
    payload: dict[str, Any] = Field(default_factory=dict)
    nonce: str
    timestamp_ms: int
    hmac: str | None = None
    correlation_id: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    protocol_version: str = "2.0.0"

    @classmethod
    def new(
        cls,
        from_agent: str,
        to_agent: str,
        kind: Literal["send", "ask", "reply"],
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        thread_id: str | None = None,
        reply_to: str | None = None,
        hmac: str | None = None,
    ) -> "Envelope":
        """Konstruktor sa auto-generisanim msg_id/nonce/timestamp_ms.

        Pozivac postavlja HMAC kasnije (posle canonical signing) ako ide na
        cross-host transport; za on-host unix socket hmac=None je OK."""
        return cls(
            msg_id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent,
            kind=kind,
            payload=payload or {},
            nonce=secrets.token_hex(16),
            timestamp_ms=int(time.time() * 1000),
            hmac=hmac,
            correlation_id=correlation_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    def to_json_bytes(self) -> bytes:
        """Serijalizacija za on-wire transport (length-prefixed framing)."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "Envelope":
        """Deserijalizacija + strict validation. Baca ValidationError ako
        nepoznata polja ili tipovi ne odgovaraju."""
        return cls.model_validate_json(data)

"""Audit log — SQLite write-through, jedini source of truth za istoriju.

Schema iz samozapazanja §2.5. Sve poruke (in/out, send/ask/reply, sve statuse)
zapisuju se OVDE; ThreadCache je in-memory derivat (§2.6). WAL mode + NORMAL
synchronous za balans throughput-a i durability-ja (WAL+NORMAL je industry
standard; FULL je 5-10x sporiji a gubi max nekoliko ms na power loss).

Nije threadovo-bezbedan namerno: clade serve ima jedan asyncio loop, sve audit
pisce idu kroz isti event loop. Ako se kasnije pojavi need za threading,
treba dodati connection-per-thread + lock pattern."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from clade.envelope import Envelope

Direction = Literal["in", "out"]
Status = Literal["delivered", "rejected", "failed", "pending"]


@dataclass(frozen=True)
class AuditRow:
    msg_id: str
    ts_ms: int
    direction: Direction
    peer: str
    kind: str
    correlation_id: str | None
    thread_id: str | None
    payload: dict[str, Any]
    status: Status
    error: str | None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit (
    msg_id          TEXT PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    direction       TEXT NOT NULL CHECK(direction IN ('in', 'out')),
    peer            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK(kind IN ('send', 'ask', 'reply')),
    correlation_id  TEXT,
    thread_id       TEXT,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('delivered', 'rejected', 'failed', 'pending')),
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts          ON audit(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_audit_peer_ts     ON audit(peer, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_audit_thread      ON audit(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit(correlation_id) WHERE correlation_id IS NOT NULL;
"""

_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous  = NORMAL",
    "PRAGMA wal_autocheckpoint = 1000",
]


class Audit:
    """Write-through audit log. Otvara DB na __init__, zatvara na close()."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        # isolation_level=None znaci autocommit; mi sami radimo BEGIN/COMMIT
        # kad nam treba transaction. Default je deferred sto izaziva implicit
        # transactions koje WAL ne voli.
        for pragma in _PRAGMAS:
            self._conn.execute(pragma)
        self._conn.executescript(_SCHEMA_SQL)

    # ---- Write API ----

    def record(self, envelope: Envelope, direction: Direction,
               status: Status = "delivered", error: str | None = None) -> None:
        """Insert envelope u audit. Idempotent po msg_id (INSERT OR REPLACE).

        Konfliktuje samo ako dve poruke imaju isti uuid4 — verovatnoca ~0.
        REPLACE umesto IGNORE zato sto bi REPLACE update-ovao status (npr.
        'pending' → 'delivered' kasnije)."""
        peer = envelope.to_agent if direction == "out" else envelope.from_agent
        self._conn.execute(
            """INSERT OR REPLACE INTO audit
               (msg_id, ts_ms, direction, peer, kind, correlation_id,
                thread_id, payload_json, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                envelope.msg_id,
                envelope.timestamp_ms,
                direction,
                peer,
                envelope.kind,
                envelope.correlation_id,
                envelope.thread_id,
                json.dumps(envelope.payload, ensure_ascii=False),
                status,
                error,
            ),
        )

    def update_status(self, msg_id: str, status: Status, error: str | None = None) -> None:
        """Update samo status/error za vec-postojeci red. Korisno za outbox
        flow: 'pending' → 'delivered' posle uspesnog retry-ja."""
        self._conn.execute(
            "UPDATE audit SET status = ?, error = ? WHERE msg_id = ?",
            (status, error, msg_id),
        )

    # ---- Read API ----

    def by_msg_id(self, msg_id: str) -> AuditRow | None:
        row = self._conn.execute(
            """SELECT msg_id, ts_ms, direction, peer, kind, correlation_id,
                      thread_id, payload_json, status, error
               FROM audit WHERE msg_id = ?""",
            (msg_id,),
        ).fetchone()
        return _row_to_dataclass(row) if row else None

    def by_thread(self, thread_id: str, max_messages: int = 10) -> list[AuditRow]:
        """Vrati posljednjih N poruka tagovanih sa thread_id-em, HRONOLOSKI
        (oldest first). Koristi se kao izvor za ThreadCache prefil na startup-u."""
        rows = self._conn.execute(
            """SELECT msg_id, ts_ms, direction, peer, kind, correlation_id,
                      thread_id, payload_json, status, error
               FROM audit
               WHERE thread_id = ?
               ORDER BY ts_ms DESC LIMIT ?""",
            (thread_id, max_messages),
        ).fetchall()
        return [_row_to_dataclass(r) for r in reversed(rows)]

    def tail(self, n: int = 100, peer: str | None = None) -> list[AuditRow]:
        """Posljednjih N poruka, hronoloski opadajuce. Opciono filter po peer-u."""
        if peer:
            rows = self._conn.execute(
                """SELECT msg_id, ts_ms, direction, peer, kind, correlation_id,
                          thread_id, payload_json, status, error
                   FROM audit WHERE peer = ?
                   ORDER BY ts_ms DESC LIMIT ?""",
                (peer, n),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT msg_id, ts_ms, direction, peer, kind, correlation_id,
                          thread_id, payload_json, status, error
                   FROM audit ORDER BY ts_ms DESC LIMIT ?""",
                (n,),
            ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    # ---- Lifecycle ----

    def checkpoint(self) -> None:
        """PASSIVE checkpoint — flush WAL u main DB bez blokiranja readera.
        Pozvati na graceful shutdown da bi sledeci open imao cist start."""
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self) -> None:
        try:
            self.checkpoint()
        finally:
            self._conn.close()


def _row_to_dataclass(row: Iterable[Any]) -> AuditRow:
    msg_id, ts_ms, direction, peer, kind, corr, thread_id, payload_json, status, error = row
    return AuditRow(
        msg_id=msg_id,
        ts_ms=ts_ms,
        direction=direction,
        peer=peer,
        kind=kind,
        correlation_id=corr,
        thread_id=thread_id,
        payload=json.loads(payload_json),
        status=status,
        error=error,
    )

"""Outbox buffer + retry sa exponencijalnim backoff-om.

Cilj: ako relay flapne ili VPN tunel padne, agent NE GUBI poslate poruke.
Umesto da returnuje gresku Claude-u, poruka se persistuje u SQLite outbox.
Sledeci tool poziv (bilo koji) pokusa da flush-uje outbox pre nego sto
uradi svoj posao.

Schema (zajednicka SQLite baza sa audit_log-om):
    outbox(id PK, kind, to_agent, endpoint, envelope_json,
           attempts, next_retry_ms, last_error, created_ts_ms)

Backoff: 1s, 2s, 4s, 8s, 16s, 30s. Max 6 attempts. Posle: mark as
"dead", ostaje u tabeli za debug (rucno se brise).
"""

import json
import sqlite3
import time
from typing import Any

BACKOFF_SCHEDULE_S = [1, 2, 4, 8, 16, 30]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_S)


def init_outbox(conn: sqlite3.Connection) -> None:
    """Kreira outbox tabelu (deli istu DB sa audit log-om)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,           -- "send" | "reply"
            to_agent TEXT NOT NULL,
            endpoint TEXT NOT NULL,       -- "/send" | "/reply"
            envelope_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_ms INTEGER NOT NULL,
            last_error TEXT,
            created_ts_ms INTEGER NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0  -- 0=pending, 1=delivered, 2=dead
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered, next_retry_ms)")
    conn.commit()


def enqueue(conn: sqlite3.Connection, kind: str, to: str, endpoint: str, envelope: dict, error: str) -> int:
    """Dodaj poruku u outbox za retry. Vraca id rowa."""
    now = int(time.time() * 1000)
    cur = conn.execute(
        """INSERT INTO outbox
           (kind, to_agent, endpoint, envelope_json, attempts, next_retry_ms, last_error, created_ts_ms)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
        (kind, to, endpoint, json.dumps(envelope), now + BACKOFF_SCHEDULE_S[0] * 1000, error, now),
    )
    conn.commit()
    return cur.lastrowid or 0


def pending_rows(conn: sqlite3.Connection, max_items: int = 20) -> list[dict[str, Any]]:
    """Vraca redove koji su spremni za retry sad (next_retry_ms <= sada).
    Iskljucuje delivered i dead."""
    now = int(time.time() * 1000)
    rows = conn.execute(
        """SELECT id, kind, to_agent, endpoint, envelope_json, attempts, last_error, created_ts_ms
           FROM outbox
           WHERE delivered = 0 AND next_retry_ms <= ?
           ORDER BY next_retry_ms ASC LIMIT ?""",
        (now, max_items),
    ).fetchall()
    return [
        {
            "id": r[0],
            "kind": r[1],
            "to_agent": r[2],
            "endpoint": r[3],
            "envelope": json.loads(r[4]),
            "attempts": r[5],
            "last_error": r[6],
            "created_ts_ms": r[7],
        }
        for r in rows
    ]


def mark_delivered(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("UPDATE outbox SET delivered = 1 WHERE id = ?", (row_id,))
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: int, error: str) -> bool:
    """Inkrementuje attempts + schedule-uje sledeci retry. Vraca True ako jos
    ima pokusaja preostalo; False ako je doslo do MAX_ATTEMPTS (mark dead)."""
    row = conn.execute("SELECT attempts FROM outbox WHERE id = ?", (row_id,)).fetchone()
    if not row:
        return False
    attempts = row[0]
    if attempts >= MAX_ATTEMPTS:
        conn.execute("UPDATE outbox SET delivered = 2, last_error = ? WHERE id = ?", (error, row_id))
        conn.commit()
        return False
    delay_s = BACKOFF_SCHEDULE_S[min(attempts, MAX_ATTEMPTS - 1)]
    now_ms = int(time.time() * 1000)
    conn.execute(
        "UPDATE outbox SET attempts = attempts + 1, next_retry_ms = ?, last_error = ? WHERE id = ?",
        (now_ms + delay_s * 1000, error, row_id),
    )
    conn.commit()
    return True


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Brzi pregled stanja outbox-a."""
    counts = {"pending": 0, "delivered": 0, "dead": 0}
    for row in conn.execute("SELECT delivered, COUNT(*) FROM outbox GROUP BY delivered"):
        if row[0] == 0:
            counts["pending"] = row[1]
        elif row[0] == 1:
            counts["delivered"] = row[1]
        elif row[0] == 2:
            counts["dead"] = row[1]
    return counts

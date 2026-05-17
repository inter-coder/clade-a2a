"""Outbox — sender-driven retry sa exponentijalnim backoff-om.

Arhitektura (samozapazanja §2.7):
- In-process retry: RETRY_SCHEDULE_S = [0.1, 0.5, 2.0] (3 pokusaja, ~2.6s)
- Outbox enqueue: audit DB sa status='pending' + zasebna `outbox_meta` tabela
  za attempts/next_retry_ms/last_error (mutable scheduling state)
- Background retry loop: svakih 30s, deliver svaku ready poruku
- Dead letter: posle MAX_ATTEMPTS=20 (~10 min), audit status → 'failed',
  outbox_meta ostaje za forenziku (rucno se cisti)

**Pristup ka audit DB-u kao single source of truth (§2.5):** audit `status`
polje pokriva 'pending' kao deo CHECK constraint-a. outbox_meta SAMO drzi
retry scheduling — kad red prelazi u 'delivered' ili 'failed', meta se obrise.

**Push notification (v1.2.0 P2#8) se sece.** Connection refused pri send-u →
fallback na outbox je sam po sebi signal. Nema OUTBOX_STALE_WARN_S log-a niti
alert-a u terminalu — /health endpoint pokazuje outbox_pending broj."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from clade.audit import Audit
from clade.envelope import Envelope
from clade.transport.types import DeliveryResult

LOG = logging.getLogger("clade.outbox")

RETRY_SCHEDULE_S: list[float] = [0.1, 0.5, 2.0]
OUTBOX_RETRY_INTERVAL_S: float = 30.0
MAX_ATTEMPTS: int = 20

# Callable koji uzima Envelope + to_url → DeliveryResult.
# Drzimo apstrakno (Transport ili funkcija) — outbox ne zna o tipovima konkretnih
# transporta. Pozivac (Server) wire-uje sa pravim transportom.
DeliverFn = Callable[[Envelope, str], Awaitable[DeliveryResult]]


_OUTBOX_META_SQL = """
CREATE TABLE IF NOT EXISTS outbox_meta (
    msg_id          TEXT PRIMARY KEY,
    to_url          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_ms   INTEGER NOT NULL,
    last_error      TEXT,
    enqueued_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_meta_retry ON outbox_meta(next_retry_ms);
"""


@dataclass(frozen=True)
class OutboxEntry:
    """Outbox red — audit envelope + meta retry state, spojeno za retry loop."""
    envelope: Envelope
    to_url: str
    attempts: int
    next_retry_ms: int
    last_error: str | None
    enqueued_at_ms: int


class Outbox:
    """Outbox wrapper iznad Audit-a. Drzi sopstvenu outbox_meta tabelu u istom
    SQLite fajlu kao audit (deli connection — minimum locking risk u single
    asyncio loop-u)."""

    def __init__(self, audit: Audit) -> None:
        self.audit = audit
        # Direktan pristup connection-u za schema bootstrap; mutacije idu
        # kroz Audit.* metode za status, kroz nasu klasu za meta.
        self.audit._conn.executescript(_OUTBOX_META_SQL)

    # ---- Write API ----

    def enqueue(self, envelope: Envelope, to_url: str, last_error: str) -> None:
        """Posle iscrpljenih in-process retries, predaj outbox-u.
        - Audit ima vec record sa status='pending' (vidi send_with_retry)
        - Mi insertujemo meta sa attempts=0 i sledeci retry za OUTBOX_RETRY_INTERVAL_S
        """
        now_ms = int(time.time() * 1000)
        self.audit._conn.execute(
            """INSERT OR REPLACE INTO outbox_meta
               (msg_id, to_url, attempts, next_retry_ms, last_error, enqueued_at_ms)
               VALUES (?, ?, 0, ?, ?, ?)""",
            (envelope.msg_id, to_url, now_ms + int(OUTBOX_RETRY_INTERVAL_S * 1000),
             last_error, now_ms),
        )

    def mark_delivered(self, msg_id: str) -> None:
        """Uspesan retry — update audit + obrisi meta."""
        self.audit.update_status(msg_id, "delivered")
        self.audit._conn.execute("DELETE FROM outbox_meta WHERE msg_id = ?", (msg_id,))

    def bump_attempts(self, msg_id: str, error: str) -> None:
        """Neuspesan retry koji nije dosao do max attempts → schedule sledeci."""
        now_ms = int(time.time() * 1000)
        self.audit._conn.execute(
            """UPDATE outbox_meta
               SET attempts = attempts + 1,
                   next_retry_ms = ?,
                   last_error = ?
               WHERE msg_id = ?""",
            (now_ms + int(OUTBOX_RETRY_INTERVAL_S * 1000), error, msg_id),
        )

    def mark_dead(self, msg_id: str, error: str) -> None:
        """Iscrpe max attempts → mark audit kao 'failed', meta ostaje za forenziku."""
        self.audit.update_status(msg_id, "failed", error=f"dead-lettered: {error}")
        # NAMERNO ne brisemo outbox_meta — forenzika moze gledati attempts.

    # ---- Read API ----

    def ready_for_retry(self, now_ms: int | None = None, limit: int = 50) -> list[OutboxEntry]:
        """Vrati outbox redove koji su spremni za retry sad (next_retry_ms <= now)
        i jos uvek su 'pending'. Sortirano po next_retry_ms (najstarije prvo)."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        rows = self.audit._conn.execute(
            """SELECT a.msg_id, a.ts_ms, a.direction, a.peer, a.kind,
                      a.correlation_id, a.thread_id, a.payload_json,
                      m.to_url, m.attempts, m.next_retry_ms, m.last_error, m.enqueued_at_ms
               FROM audit a
               JOIN outbox_meta m ON a.msg_id = m.msg_id
               WHERE a.status = 'pending' AND m.next_retry_ms <= ?
               ORDER BY m.next_retry_ms ASC
               LIMIT ?""",
            (now_ms, limit),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def pending_count(self) -> int:
        return self.audit._conn.execute(
            """SELECT COUNT(*) FROM audit a
               JOIN outbox_meta m ON a.msg_id = m.msg_id
               WHERE a.status = 'pending'"""
        ).fetchone()[0]

    def dead_count(self) -> int:
        return self.audit._conn.execute(
            """SELECT COUNT(*) FROM audit
               WHERE status = 'failed' AND error LIKE 'dead-lettered:%'"""
        ).fetchone()[0]

    def stats(self) -> dict[str, int]:
        return {
            "pending": self.pending_count(),
            "dead": self.dead_count(),
        }

    def _row_to_entry(self, row: sqlite3.Row | tuple) -> OutboxEntry:
        import json as _json  # noqa: PLC0415
        (msg_id, ts_ms, direction, peer, kind, corr, thread_id,
         payload_json, to_url, attempts, next_retry_ms, last_error, enqueued_at_ms) = row
        # Rekonstrui Envelope iz audit kolona — direction='out' uvek za outbox.
        # to_agent = peer (jer audit cuva peer = to_agent za direction='out').
        from_agent = "" if direction == "in" else "self"  # placeholder; outbound from_agent
        # NAPOMENA: audit ne cuva originalnog from_agent-a eksplicitno — peer
        # je to_agent za out, from_agent za in. Za outbox samo radimo out, pa
        # iz envelope-a nemamo izolovan from_agent. Resenje: pozivac (Server)
        # zna svoj me_id; injektujemo ga tek u retry call-u. Za sad pravimo
        # Envelope sa from_agent='' i ostavljamo Server-u da to popravi.
        env = Envelope(
            msg_id=msg_id,
            from_agent=from_agent,
            to_agent=peer,
            kind=kind,
            payload=_json.loads(payload_json),
            nonce="",  # outbox ne cuva nonce — regeneracija nije nuzna jer pre-pending
            timestamp_ms=ts_ms,
            correlation_id=corr,
            thread_id=thread_id,
        )
        return OutboxEntry(
            envelope=env, to_url=to_url, attempts=attempts,
            next_retry_ms=next_retry_ms, last_error=last_error,
            enqueued_at_ms=enqueued_at_ms,
        )


# ---- Sender-driven retry helper ----

@dataclass(frozen=True)
class SendResult:
    """Rezultat send_with_retry()."""
    delivered: bool                # uspesan deliver pre outbox-a
    queued: bool                   # zaglavljen u outbox-u za kasniji retry
    response: Envelope | None      # za 'ask' kind: peer-ov reply
    error: str | None              # poslednja greska (ako delivered=False)


async def send_with_retry(
    deliver_fn: DeliverFn,
    envelope: Envelope,
    to_url: str,
    audit: Audit,
    outbox: Outbox,
) -> SendResult:
    """In-process retry [0.1, 0.5, 2.0]s; ako i to ne uspe, enqueue u outbox.

    Audit semantika:
    - Pre prvog pokusaja: audit.record(env, direction='out', status='pending')
    - Uspeh: audit.update_status('delivered')
    - Iscrpljeno + enqueued: ostaje 'pending' (outbox ce ga update-ovati)
    - Tehnicka 4xx/validation greska (npr. bad envelope): vraca se kao 'rejected'
      bez ulaska u outbox (retry ne bi pomogao)."""
    audit.record(envelope, direction="out", status="pending")

    last_error: str | None = None
    for i, delay in enumerate(RETRY_SCHEDULE_S):
        try:
            res = await deliver_fn(envelope, to_url)
        except Exception as e:  # bilo koji asinhroni transport exception
            last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(delay)
            continue

        if res.ok:
            audit.update_status(envelope.msg_id, "delivered")
            return SendResult(delivered=True, queued=False,
                              response=res.response, error=None)

        # ok=False: retry samo na "soft" greske (connection refused, timeout).
        # Sve ostalo (4xx, bad signature, permanent rejection) tretiramo kao terminal.
        last_error = res.error
        if _is_retryable(res.error or ""):
            await asyncio.sleep(delay)
            continue

        # Hard error — ne stavi u outbox, mark audit 'rejected'
        audit.update_status(envelope.msg_id, "rejected", error=last_error)
        return SendResult(delivered=False, queued=False, response=None, error=last_error)

    # Sva 3 retry-ja iscrpljena — predaj outbox-u za background retry
    outbox.enqueue(envelope, to_url, last_error=last_error or "exhausted in-process retries")
    LOG.info("outbox enqueue: msg_id=%s, last_error=%s", envelope.msg_id, last_error)
    return SendResult(delivered=False, queued=True, response=None, error=last_error)


def _is_retryable(error_msg: str) -> bool:
    """Heuristika: connection-refused / timeout / 5xx → retry. 4xx / validation → ne."""
    m = error_msg.lower()
    if "connection" in m and ("refused" in m or "reset" in m):
        return True
    if "timeout" in m or "timed out" in m:
        return True
    if "filenotfounderror" in m:  # unix socket nije up — retry
        return True
    if "http 5" in m:  # 5xx
        return True
    if "http 429" in m:  # rate limit
        return True
    return False


# ---- Background retry loop ----

async def outbox_retry_loop(
    outbox: Outbox,
    deliver_fn: DeliverFn,
    shutdown_event: asyncio.Event,
    me_id: str,
) -> None:
    """Pozivac (Server) drzi ovaj task kao background. Svakih OUTBOX_RETRY_INTERVAL_S
    sekundi prosetra pending outbox redove i pokusa deliver. Failed retry-jevi
    se reschedule-uju; uspesan se mark-uje 'delivered'; preko MAX_ATTEMPTS se
    mark-uje 'failed' (dead-letter, ne crash-uje loop).

    `me_id` je nas peer ID — injektujemo ga u envelope.from_agent jer audit
    ne cuva originalni from_agent (peer kolona pamti to_agent za 'out')."""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=OUTBOX_RETRY_INTERVAL_S)
            return  # shutdown trigger-ovan
        except asyncio.TimeoutError:
            pass  # vreme za prolazak

        entries = outbox.ready_for_retry()
        if not entries:
            continue

        for entry in entries:
            if entry.attempts >= MAX_ATTEMPTS:
                outbox.mark_dead(entry.envelope.msg_id,
                                 error=entry.last_error or "max attempts exceeded")
                LOG.warning("outbox DEAD: msg_id=%s, attempts=%d",
                            entry.envelope.msg_id, entry.attempts)
                continue

            # Injektuj me_id u from_agent (audit ne pamti, vidi _row_to_entry note)
            env = entry.envelope.model_copy(update={"from_agent": me_id})

            try:
                res = await deliver_fn(env, entry.to_url)
            except Exception as e:
                outbox.bump_attempts(env.msg_id, error=f"{type(e).__name__}: {e}")
                continue

            if res.ok:
                outbox.mark_delivered(env.msg_id)
                LOG.info("outbox delivered: msg_id=%s posle %d attempts",
                         env.msg_id, entry.attempts + 1)
            else:
                outbox.bump_attempts(env.msg_id, error=res.error or "unknown")

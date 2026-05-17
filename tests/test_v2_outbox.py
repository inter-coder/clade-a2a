"""PR#3 testovi za clade.outbox.* (sender-driven retry + background loop)."""

import asyncio
import time
from pathlib import Path

import pytest

from clade.audit import Audit
from clade.envelope import Envelope
from clade.outbox import (
    MAX_ATTEMPTS,
    Outbox,
    SendResult,
    outbox_retry_loop,
    send_with_retry,
)
from clade.transport.types import DeliveryResult


@pytest.fixture
def db_and_outbox(tmp_path: Path):
    """Audit + Outbox sa privremenom DB. Yields (audit, outbox)."""
    audit = Audit(tmp_path / "audit.db")
    outbox = Outbox(audit)
    yield audit, outbox
    audit.close()


# ---- Outbox CRUD ----


def test_enqueue_creates_pending_audit_and_meta(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, to_url="unix:///tmp/bob.sock", last_error="fake")

    row = audit.by_msg_id(env.msg_id)
    assert row is not None
    assert row.status == "pending"
    assert outbox.pending_count() == 1
    assert outbox.dead_count() == 0


def test_ready_for_retry_respects_next_retry_ms(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, to_url="unix:///tmp/bob.sock", last_error="fake")

    # Tek enqueue-ovan red ima next_retry_ms u buducnosti (~30s)
    now_ms = int(time.time() * 1000)
    assert outbox.ready_for_retry(now_ms=now_ms) == []

    # Pomaknemo "sad" 31s napred → ready
    ready = outbox.ready_for_retry(now_ms=now_ms + 31_000)
    assert len(ready) == 1
    assert ready[0].envelope.msg_id == env.msg_id


def test_mark_delivered_removes_from_outbox(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, to_url="unix:///x", last_error="fake")
    assert outbox.pending_count() == 1

    outbox.mark_delivered(env.msg_id)
    assert outbox.pending_count() == 0
    row = audit.by_msg_id(env.msg_id)
    assert row.status == "delivered"  # type: ignore[union-attr]


def test_bump_attempts_reschedules_and_counts_up(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, to_url="unix:///x", last_error="initial")

    outbox.bump_attempts(env.msg_id, error="second try failed")
    outbox.bump_attempts(env.msg_id, error="third try failed")

    rows = outbox.ready_for_retry(now_ms=int(time.time() * 1000) + 60_000)
    assert len(rows) == 1
    assert rows[0].attempts == 2
    assert rows[0].last_error == "third try failed"


def test_mark_dead_sets_failed_status(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, to_url="unix:///x", last_error="never")
    outbox.mark_dead(env.msg_id, error="exhausted")

    row = audit.by_msg_id(env.msg_id)
    assert row.status == "failed"  # type: ignore[union-attr]
    assert "dead-lettered" in row.error  # type: ignore[union-attr]
    assert outbox.dead_count() == 1
    assert outbox.pending_count() == 0


# ---- send_with_retry helper ----


@pytest.mark.asyncio
async def test_send_with_retry_delivered_first_try(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")

    async def fake_deliver(e, url) -> DeliveryResult:
        return DeliveryResult(ok=True)

    res = await send_with_retry(fake_deliver, env, "unix:///bob.sock", audit, outbox)
    assert res.delivered is True
    assert res.queued is False
    assert audit.by_msg_id(env.msg_id).status == "delivered"  # type: ignore[union-attr]
    assert outbox.pending_count() == 0


@pytest.mark.asyncio
async def test_send_with_retry_succeeds_on_second_attempt(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    call_count = 0

    async def flaky_deliver(e, url) -> DeliveryResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return DeliveryResult(ok=False, error="ConnectionRefusedError: nope")
        return DeliveryResult(ok=True)

    res = await send_with_retry(flaky_deliver, env, "unix:///bob.sock", audit, outbox)
    assert res.delivered is True
    assert call_count == 2
    assert outbox.pending_count() == 0


@pytest.mark.asyncio
async def test_send_with_retry_all_fail_enqueues_outbox(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")

    async def always_refuse(e, url) -> DeliveryResult:
        return DeliveryResult(ok=False, error="ConnectionRefusedError: down")

    res = await send_with_retry(always_refuse, env, "unix:///bob.sock", audit, outbox)
    assert res.delivered is False
    assert res.queued is True
    assert outbox.pending_count() == 1
    row = audit.by_msg_id(env.msg_id)
    assert row.status == "pending"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_with_retry_terminal_error_skips_outbox(db_and_outbox):
    """4xx-style permanent greska → 'rejected' bez ulaska u outbox."""
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")

    async def bad_request(e, url) -> DeliveryResult:
        return DeliveryResult(ok=False, error="HTTP 400: bad signature")

    res = await send_with_retry(bad_request, env, "unix:///bob.sock", audit, outbox)
    assert res.delivered is False
    assert res.queued is False  # NE ide u outbox
    assert outbox.pending_count() == 0
    row = audit.by_msg_id(env.msg_id)
    assert row.status == "rejected"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_with_retry_exception_treated_as_retryable(db_and_outbox):
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")

    async def raises(e, url) -> DeliveryResult:
        raise ConnectionRefusedError("simulated")

    res = await send_with_retry(raises, env, "unix:///bob.sock", audit, outbox)
    assert res.queued is True


@pytest.mark.asyncio
async def test_send_with_retry_ask_response_propagated(db_and_outbox):
    """Za 'ask' kind, peer-ov reply envelope se vraca u SendResult.response."""
    audit, outbox = db_and_outbox
    env = Envelope.new(from_agent="me", to_agent="bob", kind="ask",
                       correlation_id="c1", thread_id="t1")
    reply = Envelope.new(from_agent="bob", to_agent="me", kind="reply",
                         payload={"answer": "42"}, correlation_id="c1")

    async def ok_deliver(e, url) -> DeliveryResult:
        return DeliveryResult(ok=True, response=reply)

    res = await send_with_retry(ok_deliver, env, "unix:///b.sock", audit, outbox)
    assert res.delivered is True
    assert res.response is not None
    assert res.response.payload == {"answer": "42"}


# ---- outbox_retry_loop ----


@pytest.mark.asyncio
async def test_outbox_retry_loop_delivers_pending(db_and_outbox, monkeypatch):
    """Background loop pokupi ready entries i delivers, ili bumps attempts."""
    audit, outbox = db_and_outbox

    # Patch retry interval na 0.05s da test bude brz
    monkeypatch.setattr("clade.outbox.OUTBOX_RETRY_INTERVAL_S", 0.05)

    # Enqueue jedan envelope, sa next_retry_ms u proslosti (force ready odmah)
    env = Envelope.new(from_agent="me", to_agent="bob", kind="send",
                       payload={"text": "queued"})
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, "unix:///b.sock", last_error="initial")
    # Force next_retry u proslost
    audit._conn.execute("UPDATE outbox_meta SET next_retry_ms = 0 WHERE msg_id = ?",
                        (env.msg_id,))

    delivered_ids: list[str] = []

    async def deliver(e, url) -> DeliveryResult:
        delivered_ids.append(e.msg_id)
        return DeliveryResult(ok=True)

    shutdown = asyncio.Event()
    loop_task = asyncio.create_task(outbox_retry_loop(outbox, deliver, shutdown, me_id="me"))

    # Saceka da loop prosetra (>0.05s) + ponovi do 1s
    for _ in range(20):
        await asyncio.sleep(0.1)
        if outbox.pending_count() == 0:
            break

    shutdown.set()
    await asyncio.wait_for(loop_task, timeout=2)

    assert env.msg_id in delivered_ids
    assert outbox.pending_count() == 0
    assert audit.by_msg_id(env.msg_id).status == "delivered"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_outbox_retry_loop_dead_letters_after_max_attempts(db_and_outbox, monkeypatch):
    """Posle MAX_ATTEMPTS → mark_dead, ne crash loop."""
    audit, outbox = db_and_outbox

    monkeypatch.setattr("clade.outbox.OUTBOX_RETRY_INTERVAL_S", 0.02)

    env = Envelope.new(from_agent="me", to_agent="bob", kind="send")
    audit.record(env, direction="out", status="pending")
    outbox.enqueue(env, "unix:///b.sock", last_error="initial")
    # Direktno setujemo attempts blizu MAX (= MAX, da sledeci retry ide u dead)
    audit._conn.execute(
        "UPDATE outbox_meta SET attempts = ?, next_retry_ms = 0 WHERE msg_id = ?",
        (MAX_ATTEMPTS, env.msg_id),
    )

    async def always_fail(e, url) -> DeliveryResult:
        return DeliveryResult(ok=False, error="ConnectionRefusedError: down")

    shutdown = asyncio.Event()
    loop_task = asyncio.create_task(outbox_retry_loop(outbox, always_fail, shutdown, me_id="me"))

    for _ in range(20):
        await asyncio.sleep(0.05)
        if outbox.dead_count() > 0:
            break

    shutdown.set()
    await asyncio.wait_for(loop_task, timeout=2)

    assert outbox.dead_count() == 1
    row = audit.by_msg_id(env.msg_id)
    assert row.status == "failed"  # type: ignore[union-attr]

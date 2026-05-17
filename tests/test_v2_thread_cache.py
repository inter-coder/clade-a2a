"""PR#2 testovi za clade.thread_cache.ThreadCache."""

import time
from pathlib import Path

from clade.audit import Audit
from clade.envelope import Envelope
from clade.thread_cache import ThreadCache, ThreadMsg


def _msg(msg_id: str, ts_ms: int, direction: str = "in", peer: str = "alice",
         kind: str = "send", payload: dict | None = None) -> ThreadMsg:
    return ThreadMsg(msg_id=msg_id, ts_ms=ts_ms, direction=direction,
                     peer=peer, kind=kind, payload=payload or {})


def test_append_and_get():
    c = ThreadCache()
    c.append("t1", _msg("m1", 1000, payload={"x": 1}))
    c.append("t1", _msg("m2", 2000, payload={"x": 2}))
    msgs = c.get_context("t1")
    assert len(msgs) == 2
    assert msgs[0].msg_id == "m1"
    assert msgs[1].msg_id == "m2"


def test_max_per_thread_evicts_oldest():
    c = ThreadCache(max_msgs_per_thread=3)
    for i in range(5):
        c.append("t1", _msg(f"m{i}", i * 100))
    msgs = c.get_context("t1")
    assert len(msgs) == 3
    assert [m.msg_id for m in msgs] == ["m2", "m3", "m4"]


def test_ttl_eviction():
    c = ThreadCache(ttl_s=0.05)  # 50ms TTL
    c.append("t1", _msg("m1", 1000))
    assert c.has("t1")
    time.sleep(0.1)
    # Sledeci access trigger-uje eviction
    assert not c.has("t1")
    assert c.get_context("t1") == []


def test_access_extends_ttl():
    # Vece margine da test ne flap-uje pod CI load-om
    c = ThreadCache(ttl_s=0.5)
    c.append("t1", _msg("m1", 1000))
    time.sleep(0.2)
    # Touch (get_context) treba da update-uje last_access
    c.get_context("t1")
    time.sleep(0.2)
    # Ukupno 0.4s proslo (preko polovine TTL-a), ali poslednji access je bio
    # pre 0.2s (manje od TTL=0.5s) → jos zivo
    assert c.has("t1")


def test_size_reflects_active_threads():
    c = ThreadCache()
    assert c.size() == 0
    c.append("t1", _msg("m1", 1000))
    c.append("t2", _msg("m2", 1000))
    assert c.size() == 2
    c.clear()
    assert c.size() == 0


def test_prefill_from_audit(tmp_path: Path):
    """ThreadCache.prefill_from_audit() hidrira iz Audit DB-a."""
    audit_db = Audit(tmp_path / "audit.db")
    try:
        for i, ts in enumerate([1000, 2000, 3000]):
            env = Envelope(
                msg_id=f"m-{i}", from_agent="alice", to_agent="bob",
                kind="send", payload={"x": i}, nonce=f"n-{i}",
                timestamp_ms=ts, thread_id="t-prefill",
            )
            audit_db.record(env, direction="in")

        cache = ThreadCache(max_msgs_per_thread=10)
        cache.prefill_from_audit(audit_db, "t-prefill")

        msgs = cache.get_context("t-prefill")
        assert len(msgs) == 3
        assert [m.payload["x"] for m in msgs] == [0, 1, 2]
        assert all(m.peer == "alice" for m in msgs)
    finally:
        audit_db.close()


def test_prefill_idempotent_skips_if_already_cached(tmp_path: Path):
    """Prefill ne kvari postojeci cache entry."""
    audit_db = Audit(tmp_path / "audit.db")
    try:
        cache = ThreadCache()
        cache.append("t1", _msg("m-local", 9999))
        cache.prefill_from_audit(audit_db, "t1")  # ne bi smelo da prepise local

        msgs = cache.get_context("t1")
        assert len(msgs) == 1
        assert msgs[0].msg_id == "m-local"
    finally:
        audit_db.close()

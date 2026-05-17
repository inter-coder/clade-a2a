"""PR#2 testovi za clade.audit.Audit."""

from pathlib import Path

from clade.audit import Audit
from clade.envelope import Envelope


def test_record_and_read_by_msg_id(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        env = Envelope.new(from_agent="alice", to_agent="bob", kind="ask",
                           payload={"q": "?"}, correlation_id="c1", thread_id="t1")
        db.record(env, direction="out")
        row = db.by_msg_id(env.msg_id)
        assert row is not None
        assert row.peer == "bob"  # direction=out → peer=to_agent
        assert row.kind == "ask"
        assert row.correlation_id == "c1"
        assert row.thread_id == "t1"
        assert row.payload == {"q": "?"}
        assert row.status == "delivered"
    finally:
        db.close()


def test_record_inbound_peer_is_from_agent(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
        db.record(env, direction="in")
        row = db.by_msg_id(env.msg_id)
        assert row is not None
        assert row.peer == "alice"  # direction=in → peer=from_agent
    finally:
        db.close()


def test_update_status(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
        db.record(env, direction="out", status="pending")
        assert db.by_msg_id(env.msg_id).status == "pending"  # type: ignore[union-attr]
        db.update_status(env.msg_id, "delivered")
        assert db.by_msg_id(env.msg_id).status == "delivered"  # type: ignore[union-attr]
    finally:
        db.close()


def test_by_thread_returns_chronological(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        # Tri poruke u istom threadu, sa razlicitim timestamp-ima
        for i, ts in enumerate([1000, 2000, 3000]):
            env = Envelope(
                msg_id=f"m-{i}", from_agent="alice", to_agent="bob",
                kind="send", payload={"x": i}, nonce=f"n-{i}",
                timestamp_ms=ts, thread_id="t-order",
            )
            db.record(env, direction="out")

        rows = db.by_thread("t-order", max_messages=10)
        assert [r.payload["x"] for r in rows] == [0, 1, 2]  # oldest first

        # Limit poslednjih 2 — i dalje hronoloski (najstarija ipak prvi u rezultatu)
        rows = db.by_thread("t-order", max_messages=2)
        assert [r.payload["x"] for r in rows] == [1, 2]
    finally:
        db.close()


def test_tail_with_peer_filter(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        # 2 poruke ka bobu, 1 ka karolu
        for to in ("bob", "bob", "carol"):
            env = Envelope.new(from_agent="alice", to_agent=to, kind="send")
            db.record(env, direction="out")

        bobs = db.tail(n=10, peer="bob")
        assert len(bobs) == 2
        assert all(r.peer == "bob" for r in bobs)

        all_rows = db.tail(n=10)
        assert len(all_rows) == 3
    finally:
        db.close()


def test_wal_mode_enabled(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        db.close()


def test_checkpoint_does_not_crash(tmp_path: Path):
    db = Audit(tmp_path / "audit.db")
    try:
        env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
        db.record(env, direction="out")
        db.checkpoint()  # ne bi smelo da pukne na praznoj WAL fajlu ni inace
        db.checkpoint()
    finally:
        db.close()

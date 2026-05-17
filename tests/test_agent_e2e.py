"""End-to-end agent testovi: HMAC signing/verify, outbox, ask/reply."""

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module(config_path: Path):
    """Ucitaj agent modul sa specificnim config-om. Reload-uje da pokupi novi config."""
    os.environ["CLADE_CONFIG"] = str(config_path)
    # Ako je vec ucitan, reload
    if "agent.main" in sys.modules:
        del sys.modules["agent.main"]
    if "agent.outbox" in sys.modules:
        del sys.modules["agent.outbox"]
    import agent.main  # noqa: PLC0415
    return agent.main


@pytest.mark.asyncio
async def test_alice_send_to_bob_delivered(relay_process, alice_config, bob_config, token_for_bob, relay_url):
    """Alice salje, Bob preko relay-a cita inbox + verifikuje HMAC."""
    # Alice side
    alice = _load_agent_module(alice_config)
    r = await alice.clade_send(to="bob", payload={"text": "hello bob"})
    assert r.get("ok") is True
    assert "msg_id" in r
    assert "queued" not in r  # delivered odmah, ne queued

    # Bob side
    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1, f"Bob expected 1 msg, got {inbox}"
    assert len(inbox.get("rejected", [])) == 0, f"Should not have rejected msgs: {inbox['rejected']}"
    msg = inbox["messages"][0]
    assert msg["from_agent"] == "alice"
    assert msg["payload"] == {"text": "hello bob"}


@pytest.mark.asyncio
async def test_tampered_hmac_rejected_by_receiver(relay_process, alice_config, bob_config, relay_url, token_for_alice):
    """Tampered HMAC: relay accepts (ne zna secret), bob odbacuje."""
    # Alice salje rucno preko curl-a sa tampered HMAC-om
    alice = _load_agent_module(alice_config)
    env = alice._make_envelope("send", "bob", {"text": "tampered"})
    env["hmac"] = "deadbeef" * 8
    # nonce mora biti svez
    import secrets
    env["nonce"] = secrets.token_hex(16)

    async with httpx.AsyncClient() as c:
        r = await c.post(f"{relay_url}/send", json=env,
                         headers={"Authorization": f"Bearer {token_for_alice}"}, timeout=3)
    assert r.status_code == 200  # relay accept

    # Bob cita inbox
    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 0, "Tampered msg should NOT be in verified messages"
    assert len(inbox["rejected"]) == 1
    assert "HMAC" in inbox["rejected"][0]["reason"]


@pytest.mark.asyncio
async def test_full_ask_reply_roundtrip(relay_process, alice_config, bob_config):
    """Alice ask → bob reply → alice prima verified response."""
    alice = _load_agent_module(alice_config)
    bob = _load_agent_module(bob_config)

    # Alice salje ask u background-u
    ask_task = asyncio.create_task(
        alice.clade_ask(to="bob", payload={"question": "7*8"}, timeout_s=10)
    )
    await asyncio.sleep(0.5)  # daj malo vremena da ask stigne u inbox

    # Bob cita inbox + odgovara
    bob = _load_agent_module(bob_config)  # reload da pokupi alice-ovu poruku
    # NAPOMENA: re-importing alice modul moze zbrkati outbox/audit fajlove —
    # za ovaj test koristimo httpx direktno za bob da izbegnemo to.

    import httpx as h
    import sys as s

    # Ucitaj bob config eksplicitno
    s.modules.pop("agent.main", None)
    s.modules.pop("agent.outbox", None)
    os.environ["CLADE_CONFIG"] = str(bob_config)
    import agent.main as bobm

    inbox = await bobm.clade_inbox()
    assert inbox["count"] == 1
    ask_msg = inbox["messages"][0]
    assert ask_msg["kind"] == "ask"
    corr = ask_msg["correlation_id"]

    # Bob odgovori
    reply_result = await bobm.clade_reply(correlation_id=corr, response={"answer": "56"}, to="alice")
    assert reply_result.get("ok") is True

    # Cekaj alice da dobije
    alice_result = await ask_task
    assert alice_result.get("ok") is True
    assert alice_result["response"] == {"answer": "56"}
    assert alice_result["correlation_id"] == corr


@pytest.mark.asyncio
async def test_outbox_queue_on_relay_down(workdir, alice_config, shared_secret, token_for_alice):
    """Send kad je relay DOWN → poruka u outbox-u, vraca queued=True."""
    # Promenimo relay_url u ne-postojeci endpoint
    import yaml
    cfg_data = yaml.safe_load(alice_config.read_text())
    cfg_data["relay_url"] = "http://127.0.0.1:1"  # garantovano DOWN
    alice_config.write_text(yaml.dump(cfg_data))

    alice = _load_agent_module(alice_config)
    r = await alice.clade_send(to="bob", payload={"text": "queued"})
    assert r.get("ok") is True
    assert r.get("queued") is True
    assert "outbox_row" in r

    # Status izvestava 1 pending
    status = await alice.clade_outbox_status()
    assert status["stats"]["pending"] >= 1


# ---- v1.0.0 clade_message + lock tests ----


@pytest.mark.asyncio
async def test_clade_message_send_fire_and_forget(relay_process, alice_config, bob_config):
    """clade_message(expect_reply=False) == fire-and-forget send."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(to="bob", content="hello via clade_message")
    assert r.get("ok") is True
    assert "msg_id" in r
    assert "response" not in r

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1
    msg = inbox["messages"][0]
    assert msg["kind"] == "send"
    assert msg["payload"]["text"] == "hello via clade_message"


@pytest.mark.asyncio
async def test_clade_message_dict_content(relay_process, alice_config, bob_config):
    """content kao dict se prosledjuje as-is."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(to="bob", content={"custom": "field", "x": 42})
    assert r.get("ok") is True

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1
    assert inbox["messages"][0]["payload"] == {"custom": "field", "x": 42}


@pytest.mark.asyncio
async def test_clade_message_reply_to_threading(relay_process, alice_config, bob_config):
    """reply_to + thread_id se serijalizuju u payload kao _reply_to / _thread_id."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(
        to="bob",
        content="follow-up",
        reply_to="parent-msg-123",
        thread_id="thread-abc",
    )
    assert r.get("ok") is True

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    payload = inbox["messages"][0]["payload"]
    assert payload["_reply_to"] == "parent-msg-123"
    assert payload["_thread_id"] == "thread-abc"
    assert payload["text"] == "follow-up"


@pytest.mark.asyncio
async def test_clade_message_expect_reply_roundtrip(relay_process, alice_config, bob_config):
    """clade_message(expect_reply=True) ide kroz /ask, blokira do reply-a."""
    alice = _load_agent_module(alice_config)
    bob = _load_agent_module(bob_config)

    ask_task = asyncio.create_task(
        alice.clade_message(to="bob", content="3+4", expect_reply=True, timeout_s=10)
    )
    await asyncio.sleep(0.5)

    # Bob reload (drugi config) + reply
    import sys as s
    s.modules.pop("agent.main", None)
    s.modules.pop("agent.outbox", None)
    os.environ["CLADE_CONFIG"] = str(bob_config)
    import agent.main as bobm  # noqa: PLC0415

    inbox = await bobm.clade_inbox()
    assert inbox["count"] == 1
    ask_msg = inbox["messages"][0]
    assert ask_msg["kind"] == "ask"
    corr = ask_msg["correlation_id"]
    assert corr is not None

    await bobm.clade_reply(correlation_id=corr, response={"answer": "7"}, to="alice")

    res = await ask_task
    assert res.get("ok") is True
    assert res["response"] == {"answer": "7"}


@pytest.mark.asyncio
async def test_inbox_blocked_by_active_daemon_lock(relay_process, alice_config):
    """Ako lock fajl postoji + PID zivi → clade_inbox vraca busy."""
    alice = _load_agent_module(alice_config)
    lock = alice._daemon_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # nasa PID = sigurno zivi
    try:
        result = await alice.clade_inbox()
        assert "error" in result
        assert "busy" in result["error"].lower()
        assert str(os.getpid()) in result["error"]
    finally:
        lock.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_inbox_passes_when_lock_is_stale(relay_process, alice_config):
    """Stale lock (PID koji ne postoji) ne blokira clade_inbox."""
    alice = _load_agent_module(alice_config)
    lock = alice._daemon_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    # PID 999999 — gotovo sigurno ne postoji na ovoj masini
    lock.write_text("999999")
    try:
        result = await alice.clade_inbox()
        # Treba ili da uspesno citra (count=0) ili da vrati error koji NIJE busy
        assert "error" not in result or "busy" not in result.get("error", "").lower()
    finally:
        lock.unlink(missing_ok=True)

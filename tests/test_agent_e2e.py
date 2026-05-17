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

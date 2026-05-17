"""Faza 1 security checks na relay endpoint-ima."""

import time

import httpx
import pytest


@pytest.mark.asyncio
async def test_health_ok(relay_process, relay_url):
    r = httpx.get(f"{relay_url}/health", timeout=3)
    data = r.json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert "alice" in data["known_agents"]
    assert "bob" in data["known_agents"]


@pytest.mark.asyncio
async def test_send_without_auth_returns_401(relay_process, relay_url):
    r = httpx.post(
        f"{relay_url}/send",
        json={"msg_id": "x", "from_agent": "alice", "to_agent": "bob",
              "kind": "send", "payload": {}, "nonce": "n1",
              "timestamp_ms": int(time.time() * 1000), "hmac": "x"},
        timeout=3,
    )
    assert r.status_code == 401
    assert "Authorization" in r.json()["detail"] or "token" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_send_with_bad_token_returns_401(relay_process, relay_url):
    r = httpx.post(
        f"{relay_url}/send",
        json={"msg_id": "x", "from_agent": "alice", "to_agent": "bob",
              "kind": "send", "payload": {}, "nonce": "n1",
              "timestamp_ms": int(time.time() * 1000), "hmac": "x"},
        headers={"Authorization": "Bearer NOPE"},
        timeout=3,
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_stale_timestamp_rejected(relay_process, relay_url, token_for_alice):
    """Timestamp >5min stari = 400."""
    r = httpx.post(
        f"{relay_url}/send",
        json={"msg_id": "x", "from_agent": "alice", "to_agent": "bob",
              "kind": "send", "payload": {}, "nonce": "stale-nonce",
              "timestamp_ms": 0, "hmac": "x"},
        headers={"Authorization": f"Bearer {token_for_alice}"},
        timeout=3,
    )
    assert r.status_code == 400
    assert "Timestamp" in r.json()["detail"]


@pytest.mark.asyncio
async def test_replay_detection(relay_process, relay_url, token_for_alice):
    """Iste nonce dva puta = drugi put 400."""
    payload = {"msg_id": "x", "from_agent": "alice", "to_agent": "bob",
               "kind": "send", "payload": {},
               "nonce": f"replay-{time.time_ns()}",
               "timestamp_ms": int(time.time() * 1000), "hmac": "x"}
    r1 = httpx.post(f"{relay_url}/send", json=payload,
                    headers={"Authorization": f"Bearer {token_for_alice}"}, timeout=3)
    assert r1.status_code == 200
    r2 = httpx.post(f"{relay_url}/send", json=payload,
                    headers={"Authorization": f"Bearer {token_for_alice}"}, timeout=3)
    assert r2.status_code == 400
    assert "Replay" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_inbox_cannot_be_read_as_other_agent(relay_process, relay_url, token_for_alice):
    """Alice ne moze citati Bob-ov inbox."""
    r = httpx.get(
        f"{relay_url}/inbox/bob",
        headers={"Authorization": f"Bearer {token_for_alice}"},
        timeout=3,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sender_must_match_token(relay_process, relay_url, token_for_alice):
    """Token za alice ne moze poslati poruku 'kao bob'."""
    r = httpx.post(
        f"{relay_url}/send",
        json={"msg_id": "x", "from_agent": "bob",   # ← spoofing!
              "to_agent": "alice", "kind": "send", "payload": {},
              "nonce": f"spoof-{time.time_ns()}",
              "timestamp_ms": int(time.time() * 1000), "hmac": "x"},
        headers={"Authorization": f"Bearer {token_for_alice}"},
        timeout=3,
    )
    assert r.status_code == 400
    assert "from_agent" in r.json()["detail"]

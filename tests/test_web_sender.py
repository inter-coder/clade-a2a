"""v1.18.0 — tests for web_sender (HMAC + relay POST as any peer) and the
POST /api/setup/{token}/chat/send endpoint.

For the unit-level web_sender tests, we run a tiny FastAPI relay stub on a
free port that records what was POSTed. This avoids mocking httpx internals
and gives a much stronger guarantee that the wire format matches what the
real relay would accept.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI, Request

from agent.wire import sign, verify
from clade_cli import web_sender


# ---- relay stub ----

class StubRelay:
    """Minimal in-process relay: records /send envelopes, replies to /ask with
    an HMAC-signed reply envelope using the reverse direction's pair-secret."""

    def __init__(self, port: int, peer_secrets: dict[tuple[str, str], str], tokens: dict[str, str]):
        self.port = port
        # pair_secret keyed by frozenset({from, to}) (symmetric)
        self.secrets = {frozenset(k): v for k, v in peer_secrets.items()}
        self.tokens = tokens  # bearer_token -> peer_id
        self.received_sends: list[dict] = []
        self.received_asks: list[dict] = []
        self.app = FastAPI()
        self._setup_routes()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _secret(self, a: str, b: str) -> str:
        return self.secrets[frozenset({a, b})]

    def _setup_routes(self):
        @self.app.post("/send")
        async def _send(req: Request):
            auth = req.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return {"error": "no bearer"}
            tok = auth[len("Bearer "):]
            if tok not in self.tokens:
                return {"error": "bad bearer"}
            env = await req.json()
            self.received_sends.append(env)
            return {"ok": True, "msg_id": env["msg_id"]}

        @self.app.post("/ask")
        async def _ask(req: Request):
            body = await req.json()
            env = body["env"]
            self.received_asks.append(env)
            # Build a reply envelope using the reverse direction's HMAC secret.
            reply_payload = {"echo": env["payload"].get("text", ""), "ack": True}
            from_a = env["to_agent"]   # original recipient becomes reply sender
            to_a = env["from_agent"]
            import secrets as _secrets
            import uuid as _uuid
            secret = self._secret(from_a, to_a)
            msg_id = str(_uuid.uuid4())
            nonce = _secrets.token_hex(16)
            ts_ms = int(time.time() * 1000)
            sig = sign(secret, msg_id, from_a, to_a, "reply", reply_payload, nonce, ts_ms,
                        env.get("correlation_id"))
            reply_env = {
                "msg_id": msg_id, "from_agent": from_a, "to_agent": to_a, "kind": "reply",
                "payload": reply_payload, "nonce": nonce, "timestamp_ms": ts_ms, "hmac": sig,
                "correlation_id": env.get("correlation_id"),
            }
            return {"ok": True, "response": reply_env}

    def start(self):
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # wait for bind
        for _ in range(50):
            try:
                with httpx.Client(timeout=0.2) as c:
                    c.get(f"http://127.0.0.1:{self.port}/openapi.json")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("stub relay never started")

    def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay(tmp_path):
    """Start a stub relay + write alice.yaml and bob.yaml with matching pair-secret."""
    pair_secret = "ab" * 32  # 64 hex chars
    alice_bearer, bob_bearer = "alice-tok", "bob-tok"
    port = _free_port()
    relay = StubRelay(
        port=port,
        peer_secrets={("alice", "bob"): pair_secret},
        tokens={alice_bearer: "alice", bob_bearer: "bob"},
    )
    relay.start()
    relay_url = f"http://127.0.0.1:{port}"

    alice_yaml = tmp_path / "alice.yaml"
    alice_yaml.write_text(yaml.dump({
        "my_id": "alice", "bearer_token": alice_bearer, "relay_url": relay_url,
        "peers": {"bob": {"secret": pair_secret, "name": "Bob", "role": "tester"}},
        "audit_db": str(tmp_path / "alice-audit.db"),
    }))
    bob_yaml = tmp_path / "bob.yaml"
    bob_yaml.write_text(yaml.dump({
        "my_id": "bob", "bearer_token": bob_bearer, "relay_url": relay_url,
        "peers": {"alice": pair_secret},
        "audit_db": str(tmp_path / "bob-audit.db"),
    }))
    yield {"relay": relay, "alice_yaml": alice_yaml, "bob_yaml": bob_yaml, "secret": pair_secret}
    relay.stop()


# ---- web_sender unit tests ----

def test_send_fire_and_forget(relay):
    result = web_sender.send_message(relay["alice_yaml"], "bob", "hello bob",
                                      expect_reply=False)
    assert result["ok"] is True
    assert result["from"] == "alice" and result["to"] == "bob"
    assert "msg_id" in result
    # Relay received exactly one envelope, well-formed + HMAC valid
    sends = relay["relay"].received_sends
    assert len(sends) == 1
    env = sends[0]
    assert env["from_agent"] == "alice" and env["to_agent"] == "bob"
    assert env["kind"] == "send"
    assert env["payload"] == {"text": "hello bob"}
    assert verify(relay["secret"], env)


def test_send_expect_reply_returns_response(relay):
    result = web_sender.send_message(relay["alice_yaml"], "bob", "ping",
                                      expect_reply=True, timeout_s=5)
    assert result["ok"] is True
    assert result["response"] == {"echo": "ping", "ack": True}
    assert result["correlation_id"]
    assert relay["relay"].received_asks
    env = relay["relay"].received_asks[0]
    assert env["kind"] == "ask"
    assert "correlation_id" in env


def test_send_with_thread_id_attached(relay):
    result = web_sender.send_message(relay["alice_yaml"], "bob", "thread msg",
                                      expect_reply=False, thread_id="t-123")
    assert result["thread_id"] == "t-123"
    env = relay["relay"].received_sends[0]
    assert env["payload"]["_thread_id"] == "t-123"


def test_send_unknown_to_peer_raises(relay):
    with pytest.raises(web_sender.WebSendError):
        web_sender.send_message(relay["alice_yaml"], "carol", "hi", expect_reply=False)


def test_send_missing_yaml_raises(tmp_path):
    with pytest.raises(web_sender.WebSendError):
        web_sender.send_message(tmp_path / "nonexistent.yaml", "bob", "hi")


def test_send_dict_content_passthrough(relay):
    result = web_sender.send_message(relay["alice_yaml"], "bob",
                                      {"foo": "bar", "n": 42}, expect_reply=False)
    assert result["ok"]
    env = relay["relay"].received_sends[0]
    assert env["payload"] == {"foo": "bar", "n": 42}


# ---- endpoint integration ----

def test_chat_send_endpoint_via_TestClient(tmp_path, monkeypatch):
    """End-to-end: TestClient + real stub relay + AppState with one setup."""
    from fastapi.testclient import TestClient
    from clade_cli.setup_server import (
        AppState, PeerInput, SetupForm, _generate_setup, _save_setup, create_app,
    )
    pair_secret = "cd" * 32
    alice_bearer, bob_bearer = "atok", "btok"
    port = _free_port()
    relay = StubRelay(port=port,
                      peer_secrets={("alice", "bob"): pair_secret},
                      tokens={alice_bearer: "alice", bob_bearer: "bob"})
    relay.start()
    try:
        # Build a setup manually; override bearers + secrets to match relay
        state = AppState(data_dir=tmp_path / "data", public_url_base="http://localhost")
        state.data_dir.mkdir(parents=True)
        form = SetupForm(project_name="kt", relay_host="127.0.0.1",
                         relay_url_host="127.0.0.1", relay_port=port, start_relay=False,
                         peers=[PeerInput(peer_id="alice"), PeerInput(peer_id="bob")])
        setup = _generate_setup(form, state.data_dir)
        # Rewrite each yaml with the exact bearers + pair-secret the relay expects
        for pid, bearer in (("alice", alice_bearer), ("bob", bob_bearer)):
            yp = state.data_dir / setup.project_token / f"{pid}.yaml"
            d = yaml.safe_load(yp.read_text())
            d["bearer_token"] = bearer
            other = "bob" if pid == "alice" else "alice"
            d["peers"] = {other: pair_secret}
            yp.write_text(yaml.dump(d, default_flow_style=False, sort_keys=False))
            setup.peers[pid].bearer_token = bearer  # in-memory consistency
        _save_setup(setup, state.data_dir)
        state.setups[setup.project_token] = setup

        client = TestClient(create_app(state))
        r = client.post(f"/api/setup/{setup.project_token}/chat/send",
                         json={"from_peer": "alice", "to_peer": "bob",
                               "content": "endpoint test", "expect_reply": True,
                               "timeout_s": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["response"]["echo"] == "endpoint test"

        # Also test 400 on unknown peers
        r = client.post(f"/api/setup/{setup.project_token}/chat/send",
                         json={"from_peer": "alice", "to_peer": "ghost",
                               "content": "x"})
        assert r.status_code == 400

        # Same-peer
        r = client.post(f"/api/setup/{setup.project_token}/chat/send",
                         json={"from_peer": "alice", "to_peer": "alice",
                               "content": "x"})
        assert r.status_code == 400
    finally:
        relay.stop()

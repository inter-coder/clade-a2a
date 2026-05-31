"""v1.18.0 — send a clade_message as any peer from the web UI.

Loads the from-peer's yaml (bearer + pair-secret for the recipient), builds an
HMAC-signed envelope, and POSTs to the relay's `/send` (fire-and-forget) or
`/ask` (sync expect_reply). Mirrors the on-the-wire behavior of
`agent.main.clade_message` without needing the agent's MCP server or daemon
running.

Used by `POST /api/setup/{token}/chat/send` in `setup_server.py`. Does NOT
write to the from-peer's audit DB or thread_history — those are owned by the
peer's daemon process. Repeated sends with the same `thread_id` will still
build up history on the RECIPIENT's daemon side (so the recipient sees the
thread), but the sender's web UI view is ephemeral.
"""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from agent.wire import sign, verify  # pure funcs, no config side-effects


class WebSendError(Exception):
    """Raised when a chat send fails (config / network / verification)."""


def _load_peer_cfg(yaml_path: Path) -> dict:
    if not yaml_path.exists():
        raise WebSendError(f"yaml not found: {yaml_path}")
    data = yaml.safe_load(yaml_path.read_text()) or {}
    for required in ("my_id", "bearer_token", "relay_url", "peers"):
        if required not in data:
            raise WebSendError(f"yaml missing '{required}': {yaml_path}")
    return data


def _pair_secret(cfg: dict, to_peer: str) -> str:
    entry = cfg["peers"].get(to_peer)
    if entry is None:
        raise WebSendError(f"peer '{to_peer}' not in '{cfg['my_id']}.yaml' peers section")
    if isinstance(entry, str):
        return entry
    return entry.get("secret") or ""


def _build_envelope(cfg: dict, to_peer: str, kind: str, payload: dict,
                    correlation_id: str | None = None) -> dict:
    secret = _pair_secret(cfg, to_peer)
    if not secret:
        raise WebSendError(f"no HMAC secret configured for peer '{to_peer}'")
    msg_id = str(uuid.uuid4())
    nonce = secrets.token_hex(16)
    ts_ms = int(time.time() * 1000)
    sig = sign(secret, msg_id, cfg["my_id"], to_peer, kind, payload, nonce, ts_ms, correlation_id)
    env = {
        "msg_id": msg_id,
        "from_agent": cfg["my_id"],
        "to_agent": to_peer,
        "kind": kind,
        "payload": payload,
        "nonce": nonce,
        "timestamp_ms": ts_ms,
        "hmac": sig,
    }
    if correlation_id:
        env["correlation_id"] = correlation_id
    return env


def send_message(
    from_yaml_path: Path,
    to_peer: str,
    content: str | dict,
    *,
    expect_reply: bool = False,
    timeout_s: int = 90,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send a clade_message as `from_peer` (identified by yaml file) to `to_peer`.

    Returns a dict mirroring `clade_message`:
      expect_reply=False → {ok, msg_id, to, from, thread_id?}
      expect_reply=True  → {ok, response, correlation_id, msg_id, thread_id?}
      error              → {error: "..."}
    """
    cfg = _load_peer_cfg(from_yaml_path)
    payload: dict[str, Any] = {"text": content} if isinstance(content, str) else dict(content)
    if thread_id:
        payload["_thread_id"] = thread_id

    if expect_reply:
        correlation_id = str(uuid.uuid4())
        env = _build_envelope(cfg, to_peer, "ask", payload, correlation_id=correlation_id)
        try:
            with httpx.Client(timeout=timeout_s + 10) as client:
                r = client.post(
                    f"{cfg['relay_url']}/ask",
                    json={"env": env, "timeout_s": timeout_s},
                    headers={"Authorization": f"Bearer {cfg['bearer_token']}"},
                )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return {"error": f"network: {type(e).__name__}: {e}"}
        if r.status_code != 200:
            return {"error": f"relay HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        reply_env = data.get("response")
        if not reply_env:
            return {"error": "no response field in relay reply"}
        if not verify(_pair_secret(cfg, to_peer), reply_env):
            return {"error": "reply HMAC verification failed — potential MITM"}
        return {
            "ok": True,
            "msg_id": env["msg_id"],
            "correlation_id": correlation_id,
            "response": reply_env["payload"],
            "thread_id": payload.get("_thread_id"),
            "from": cfg["my_id"],
            "to": to_peer,
        }

    # fire-and-forget
    env = _build_envelope(cfg, to_peer, "send", payload)
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                f"{cfg['relay_url']}/send",
                json=env,
                headers={"Authorization": f"Bearer {cfg['bearer_token']}"},
            )
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return {"error": f"network: {type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"error": f"relay HTTP {r.status_code}: {r.text[:200]}"}
    return {
        "ok": True,
        "msg_id": env["msg_id"],
        "thread_id": payload.get("_thread_id"),
        "from": cfg["my_id"],
        "to": to_peer,
    }

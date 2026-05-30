"""Clade Relay — Faza 2.

Message broker sa:
- Bearer token authentication (tokens.json mapuje token → agent_id) [Faza 1]
- Nonce dedup + timestamp window — anti-replay [Faza 1]
- E2E HMAC ne validira (peer secret nije njegova briga) [Faza 1]
- Pluggable storage backend: in-memory (dev) ili Redis (prod) [Faza 2]
- Production deploy preko Docker + Caddy + Let's Encrypt [Faza 2]

Production-grade dodaci za Faza 3+: rate limiting po token-u,
geo-anomaly detekcija, web UI za audit.
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from relay.store import Store, make_store


# ---- Config ----

TOKENS_PATH = Path(__file__).parent / "tokens.json"
NONCE_TTL_S = 300       # 5min anti-replay window
TS_SKEW_MS = 300_000    # 5min timestamp skew tolerance
INBOX_MAX = 1000        # safety cap per agent
AUDIT_MAX = 10_000
# v1.5.0: back-pressure — broj istovremenih pending ask-ova ka istom peer-u.
# Stress test je pokazao da peer daemon spawn-uje claude --print sa
# semaforom CLADE_DAEMON_CONCURRENCY (default 2) pa visak samo cetka u
# inbox-u. Relay sad odbija sa 503 + Retry-After ako vec ima vise od ovog.
MAX_PENDING_PER_PEER = int(os.environ.get("CLADE_RELAY_MAX_PENDING_PER_PEER", "4"))
# v1.8.0: presence — peer je "online" ako je heartbeat-ao u zadnjih PRESENCE_TTL_S.
# Daemon salje POST /presence svakih PRESENCE_HEARTBEAT_S; ako 2 propuste, peer
# pada offline. Drzimo in-memory (kao pending_asks) — restart relay-a → svi
# offline dok ne stigne sledeci heartbeat (~15s). Nije serijabilno na disk.
PRESENCE_TTL_S = int(os.environ.get("CLADE_RELAY_PRESENCE_TTL_S", "35"))


# ---- State ----

# Storage backend (in-memory ili Redis) — inicijalizovan u lifespan-u
store: Store | None = None

# pending_asks[correlation_id] = asyncio.Future
# NAMERA: ovo OSTAJE u memoriji. asyncio.Future nije serijabilan, a ako
# relay restartuje, sve in-flight asks fail-uju (klijent retry-uje).
pending_asks: dict[str, asyncio.Future] = {}
# v1.5.0: per-peer pending count za back-pressure. corr_id → target_peer.
# Sync sa pending_asks (add/remove zajedno).
pending_target_by_corr: dict[str, str] = {}

# tokens[token_str] = agent_id — ucitano sa diska, immutable
tokens: dict[str, str] = {}

# audit ring buffer — u memoriji za sad, Redis stream u Faza 3
audit: list[dict[str, Any]] = []

# v1.8.0: presence tracker — peer_id → unix timestamp (sekunde) zadnjeg /presence
# heartbeat-a. In-process namerno; ako relay restartuje, daemon-i ce u 15s
# ponovo registrovati prisustvo.
last_seen_by_peer: dict[str, float] = {}


# ---- Models ----

class Envelope(BaseModel):
    """Poruka koja putuje kroz relay. HMAC se validira E2E na agent strani —
    relay samo proverava nonce + timestamp + bearer token mapping."""
    msg_id: str
    from_agent: str
    to_agent: str
    kind: str  # "send" | "ask"
    correlation_id: str | None = None
    payload: dict[str, Any]
    nonce: str
    timestamp_ms: int
    hmac: str


class ReplyEnvelope(BaseModel):
    """Reply na pending ask. Takodje HMAC-potpisana. Mora da sadrzi
    `kind: "reply"` polje da bi HMAC verifikacija na original-sender strani
    bila ponovljiva (sign() ukljucuje kind u canonical message)."""
    msg_id: str
    from_agent: str
    to_agent: str
    kind: str = "reply"
    correlation_id: str
    payload: dict[str, Any]
    nonce: str
    timestamp_ms: int
    hmac: str


class AskBody(BaseModel):
    env: Envelope
    timeout_s: int = 90


# ---- Auth ----

async def authenticate(authorization: Annotated[str | None, Header()] = None) -> str:
    """Vrati agent_id ako je Bearer token validan, inace 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    token = authorization[7:]
    agent_id = tokens.get(token)
    if not agent_id:
        raise HTTPException(401, "Invalid bearer token")
    return agent_id


# ---- Anti-replay ----

def check_timestamp(ts_ms: int) -> bool:
    """Vrati True ako je timestamp u prozoru (max TS_SKEW_MS skju)."""
    now_ms = int(time.time() * 1000)
    return abs(now_ms - ts_ms) <= TS_SKEW_MS


async def validate_envelope(env_dict: dict, sender: str) -> str | None:
    """Brze provere koje relay moze da uradi (sender, nonce, timestamp).
    NE proverava HMAC — to je E2E na agent strani.

    Vraca error message ili None ako je OK."""
    if env_dict["from_agent"] != sender:
        return f"from_agent ('{env_dict['from_agent']}') ne odgovara autentikovanom sender-u ('{sender}')"
    if not check_timestamp(env_dict["timestamp_ms"]):
        return f"Timestamp izvan prozora od {TS_SKEW_MS}ms"
    assert store is not None
    if not await store.check_nonce(env_dict["nonce"], NONCE_TTL_S):
        return "Replay detected: nonce vec videno"
    return None


# ---- Audit ----

def log(env: dict, status: str) -> None:
    entry = {
        "ts": int(time.time() * 1000),
        "msg_id": env.get("msg_id"),
        "from": env.get("from_agent"),
        "to": env.get("to_agent"),
        "kind": env.get("kind") or env.get("correlation_id") and "reply",
        "status": status,
    }
    audit.append(entry)
    if len(audit) > AUDIT_MAX:
        del audit[: AUDIT_MAX // 2]
    print(f"[relay] {status:25s} {env.get('from_agent')} → {env.get('to_agent')} ({env.get('kind', 'reply')})")


# ---- Lifecycle ----

def load_tokens() -> dict[str, str]:
    if not TOKENS_PATH.exists():
        raise RuntimeError(f"tokens.json not found at {TOKENS_PATH}. Run scripts/gen-keys.sh first.")
    with open(TOKENS_PATH) as f:
        return json.load(f)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global store
    tokens.update(load_tokens())
    store = await make_store()
    print(f"[relay] startup — loaded {len(tokens)} tokens, store={type(store).__name__}")
    print(f"[relay] known agents: {sorted(set(tokens.values()))}")
    yield
    print("[relay] shutdown")


app = FastAPI(title="Clade Relay (Faza 2)", lifespan=lifespan)


# ---- Endpoints ----

def _presence_snapshot() -> dict[str, dict[str, Any]]:
    """v1.8.0: izracunaj presence po peer-u. Vraca {peer_id: {online, last_seen_ms,
    secs_ago}}. Uključuje SVAKOG registrovanog peer-a (iz tokens), ne samo one
    koji su heartbeat-ali — peer koji nikad nije poslao /presence ima
    online=false i last_seen_ms=null."""
    now = time.time()
    snap: dict[str, dict[str, Any]] = {}
    for peer in sorted(set(tokens.values())):
        ts = last_seen_by_peer.get(peer)
        if ts is None:
            snap[peer] = {"online": False, "last_seen_ms": None, "secs_ago": None}
        else:
            secs = now - ts
            snap[peer] = {
                "online": secs < PRESENCE_TTL_S,
                "last_seen_ms": int(ts * 1000),
                "secs_ago": round(secs, 1),
            }
    return snap


@app.get("/health")
async def health() -> dict[str, Any]:
    assert store is not None
    store_health = await store.health()
    # v1.7.0 (C1): per-peer pending count — sender Claude moze proaktivno
    # load-balance pre nego sto dobije 503 back-pressure.
    pending_by_peer: dict[str, int] = {}
    for target in pending_target_by_corr.values():
        pending_by_peer[target] = pending_by_peer.get(target, 0) + 1
    presence = _presence_snapshot()
    online_peers = sorted([p for p, s in presence.items() if s["online"]])
    return {
        "ok": True,
        "phase": 2,
        "known_agents": sorted(set(tokens.values())),
        "online_agents": online_peers,
        "pending_asks": len(pending_asks),
        "pending_by_peer": pending_by_peer,
        "max_pending_per_peer": MAX_PENDING_PER_PEER,
        "audit_count": len(audit),
        "store": store_health,
        "presence_ttl_s": PRESENCE_TTL_S,
    }


@app.post("/presence")
async def presence_heartbeat(sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """v1.8.0: peer daemon kuca svakih ~15s da javi 'zivim'. Bearer auth → peer_id
    iz tokens mappinga; ne treba body. Idempotentno: samo update-uje last_seen."""
    last_seen_by_peer[sender] = time.time()
    return {"ok": True, "peer": sender, "ttl_s": PRESENCE_TTL_S}


@app.get("/presence")
async def presence_snapshot(sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """v1.8.0: vrati ko je online u celoj mrezi. Bearer auth (svaki authenticated
    peer moze citati — to je jedna od stvari koju treba da zna).

    Response: {peers: {id: {online, last_seen_ms, secs_ago}}, ttl_s, server_time_ms}"""
    return {
        "peers": _presence_snapshot(),
        "ttl_s": PRESENCE_TTL_S,
        "server_time_ms": int(time.time() * 1000),
        "you": sender,
    }


@app.post("/send")
async def send(env: Envelope, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Fire-and-forget. Bearer + nonce + timestamp validacija."""
    env_dict = env.model_dump()
    err = await validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    assert store is not None
    ok = await store.inbox_push(env.to_agent, env_dict, INBOX_MAX)
    if not ok:
        raise HTTPException(503, f"Inbox for {env.to_agent} full ({INBOX_MAX} items)")
    log(env_dict, "delivered")
    return {"ok": True, "msg_id": env.msg_id}


@app.post("/ask")
async def ask(body: AskBody, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Sinhroni ask — blokira do reply ili timeout.

    v1.5.0: back-pressure — vraca 503 + Retry-After ako ka istom peer-u vec
    postoji MAX_PENDING_PER_PEER pending ask-ova. Sprecava 5-u-jedan stress
    koji preopretacuje peer-ov claude --print semafor (default 2)."""
    env_dict = body.env.model_dump()
    if not body.env.correlation_id:
        raise HTTPException(400, "correlation_id required for ask")

    err = await validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    target = body.env.to_agent
    pending_for_target = sum(1 for t in pending_target_by_corr.values() if t == target)
    if pending_for_target >= MAX_PENDING_PER_PEER:
        log(env_dict, f"backpressure:{pending_for_target}_pending")
        retry_s = max(5, body.timeout_s // 6)
        raise HTTPException(
            503,
            {
                "detail": f"Peer '{target}' vec ima {pending_for_target} pending ask-ova "
                          f"(max {MAX_PENDING_PER_PEER}). Retry kroz ~{retry_s}s.",
                "retry_after_s": retry_s,
                "pending_count": pending_for_target,
                "target_peer": target,
            },
            headers={"Retry-After": str(retry_s)},
        )

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    pending_asks[body.env.correlation_id] = fut
    pending_target_by_corr[body.env.correlation_id] = target

    assert store is not None
    ok = await store.inbox_push(body.env.to_agent, env_dict, INBOX_MAX)
    if not ok:
        pending_asks.pop(body.env.correlation_id, None)
        pending_target_by_corr.pop(body.env.correlation_id, None)
        raise HTTPException(503, f"Inbox for {body.env.to_agent} full")
    log(env_dict, "asked")

    try:
        response = await asyncio.wait_for(fut, timeout=body.timeout_s)
        log(env_dict, "ask_replied")
        return {"ok": True, "response": response, "msg_id": body.env.msg_id}
    except asyncio.TimeoutError:
        log(env_dict, "ask_timeout")
        raise HTTPException(504, f"Ask timed out after {body.timeout_s}s")
    finally:
        pending_asks.pop(body.env.correlation_id, None)
        pending_target_by_corr.pop(body.env.correlation_id, None)


@app.post("/reply")
async def reply(reply_env: ReplyEnvelope, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Reply na pending ask. Mora biti HMAC-potpisana (validacija E2E na original-sender strani
    kad procita inbox), ali relay forward-uje payload + HMAC zajedno."""
    env_dict = reply_env.model_dump()
    err = await validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    fut = pending_asks.get(reply_env.correlation_id)
    if fut is None:
        raise HTTPException(404, f"No pending ask with correlation_id={reply_env.correlation_id}")
    if fut.done():
        raise HTTPException(409, "Ask already resolved")

    fut.set_result(env_dict)
    log(env_dict, "reply_delivered")
    return {"ok": True}


@app.get("/inbox/{agent_id}")
async def get_inbox(agent_id: str, sender: Annotated[str, Depends(authenticate)], max_items: int = 50) -> dict[str, Any]:
    """Drenira inbox za agent_id. Sender MORA biti agent_id (ne moze citati tudji inbox)."""
    if agent_id != sender:
        raise HTTPException(403, f"Cannot read inbox of '{agent_id}' as '{sender}'")

    assert store is not None
    items = await store.inbox_drain(agent_id, max_items)
    if items:
        log({"from_agent": "-", "to_agent": agent_id, "kind": "inbox"}, f"drained_{len(items)}")
    return {"messages": items, "count": len(items)}


@app.get("/audit")
async def get_audit(sender: Annotated[str, Depends(authenticate)], tail: int = 100) -> dict[str, Any]:
    """Audit pristup — svaki authenticated agent moze citati."""
    return {"entries": audit[-tail:], "total": len(audit)}


@app.post("/admin/reload-tokens")
async def reload_tokens(_sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """v1.11.0: re-read tokens.json from disk into the in-memory `tokens` dict.
    Used by clade-add-peer after appending a new bearer — without this, the
    new peer would get 401 until the relay restarts. Any authenticated agent
    can call this (it doesn't grant any privilege, just refreshes shared state)."""
    new_tokens = load_tokens()
    before = set(tokens.values())
    after = set(new_tokens.values())
    tokens.clear()
    tokens.update(new_tokens)
    return {
        "ok": True,
        "known_agents": sorted(after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
    }


# ---- Web UI (Faza 5) ----

_UI_PATH = Path(__file__).parent / "ui" / "audit.html"


@app.get("/ui", response_class=HTMLResponse)
@app.get("/ui/audit", response_class=HTMLResponse)
async def ui_audit() -> str:
    """Static HTML audit viewer. Korisnik unosi Bearer token u formu,
    page polnja /audit endpoint preko XHR-a (same-origin)."""
    if not _UI_PATH.exists():
        raise HTTPException(404, "UI nije nadjen — verovatno paket nije instaliran sa relay/ui/")
    return _UI_PATH.read_text(encoding="utf-8")


def main() -> None:
    """Entry point za `clade-relay` console script (vidi pyproject.toml)."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Clade A2A Relay")
    parser.add_argument("--host", default=os.environ.get("CLADE_RELAY_HOST", "127.0.0.1"),
                        help="Host za listen (default: 127.0.0.1; za LAN: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLADE_RELAY_PORT", "7777")),
                        help="Port (default: 7777)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--tokens", default=None,
                        help="Path do tokens.json (override default-a relay/tokens.json)")
    args = parser.parse_args()

    if args.tokens:
        global TOKENS_PATH
        TOKENS_PATH = Path(args.tokens)

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()

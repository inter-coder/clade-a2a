"""Clade Relay — Faza 1.

In-memory message broker sa:
- Bearer token authentication (tokens.json mapuje token → agent_id)
- Nonce dedup (5min window) — anti-replay
- Timestamp window check (5min skew) — anti-replay
- NE validira HMAC (nije sopstveni problem — relay je dumb dispatcher,
  HMAC validacija je E2E na agent strani sa per-pair shared secret-om
  koji relay ne zna)

Production-grade dodaci (Faza 2+): Redis persistence, TLS preko Caddy,
rate limiting, geo-anomaly detekcija.
"""

import asyncio
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


# ---- Config ----

TOKENS_PATH = Path(__file__).parent / "tokens.json"
NONCE_TTL_S = 300       # 5min anti-replay window
TS_SKEW_MS = 300_000    # 5min timestamp skew tolerance
INBOX_MAX = 1000        # safety cap per agent
AUDIT_MAX = 10_000


# ---- State ----

# inbox[agent_id] = [envelope, ...]
inbox: dict[str, list[dict[str, Any]]] = defaultdict(list)

# pending_asks[correlation_id] = asyncio.Future
pending_asks: dict[str, asyncio.Future] = {}

# tokens[token_str] = agent_id
tokens: dict[str, str] = {}

# nonces[nonce] = expire_unix_s
nonces: dict[str, float] = {}

# audit ring buffer
audit: list[dict[str, Any]] = []


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
    timeout_s: int = 120


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

def check_nonce(nonce: str) -> bool:
    """Vrati True ako je nonce nov i zabelezen; False ako je vec videno."""
    now = time.time()
    # Cleanup expired
    if len(nonces) > 10_000:
        expired = [n for n, exp in nonces.items() if exp < now]
        for n in expired:
            nonces.pop(n, None)
    if nonce in nonces:
        return False
    nonces[nonce] = now + NONCE_TTL_S
    return True


def check_timestamp(ts_ms: int) -> bool:
    """Vrati True ako je timestamp u prozoru (max TS_SKEW_MS skju)."""
    now_ms = int(time.time() * 1000)
    return abs(now_ms - ts_ms) <= TS_SKEW_MS


def validate_envelope(env_dict: dict, sender: str) -> str | None:
    """Brze provere koje relay moze da uradi (sender, nonce, timestamp).
    NE proverava HMAC — to je E2E na agent strani.

    Vraca error message ili None ako je OK."""
    if env_dict["from_agent"] != sender:
        return f"from_agent ('{env_dict['from_agent']}') ne odgovara autentikovanom sender-u ('{sender}')"
    if not check_timestamp(env_dict["timestamp_ms"]):
        return f"Timestamp izvan prozora od {TS_SKEW_MS}ms"
    if not check_nonce(env_dict["nonce"]):
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
    tokens.update(load_tokens())
    print(f"[relay] startup — loaded {len(tokens)} tokens, listening on http://localhost:7777")
    print(f"[relay] known agents: {sorted(set(tokens.values()))}")
    yield
    print("[relay] shutdown")


app = FastAPI(title="Clade Relay (Faza 1)", lifespan=lifespan)


# ---- Endpoints ----

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "phase": 1,
        "known_agents": sorted(set(tokens.values())),
        "inbox_sizes": {k: len(v) for k, v in inbox.items() if v},
        "pending_asks": len(pending_asks),
        "audit_count": len(audit),
        "nonce_cache_size": len(nonces),
    }


@app.post("/send")
async def send(env: Envelope, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Fire-and-forget. Bearer + nonce + timestamp validacija."""
    env_dict = env.model_dump()
    err = validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    if len(inbox[env.to_agent]) >= INBOX_MAX:
        raise HTTPException(503, f"Inbox for {env.to_agent} full ({INBOX_MAX} items)")

    inbox[env.to_agent].append(env_dict)
    log(env_dict, "delivered")
    return {"ok": True, "msg_id": env.msg_id}


@app.post("/ask")
async def ask(body: AskBody, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Sinhroni ask — blokira do reply ili timeout."""
    env_dict = body.env.model_dump()
    if not body.env.correlation_id:
        raise HTTPException(400, "correlation_id required for ask")

    err = validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    pending_asks[body.env.correlation_id] = fut

    inbox[body.env.to_agent].append(env_dict)
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


@app.post("/reply")
async def reply(reply_env: ReplyEnvelope, sender: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
    """Reply na pending ask. Mora biti HMAC-potpisana (validacija E2E na original-sender strani
    kad procita inbox), ali relay forward-uje payload + HMAC zajedno.

    Faza 1 simplifikacija: reply payload se vraca preko Future-a kao raw dict;
    HMAC inner-payload validacija u Faza 1.5 (sad relay samo proverava nonce + timestamp + sender)."""
    env_dict = reply_env.model_dump()
    err = validate_envelope(env_dict, sender)
    if err:
        log(env_dict, f"rejected:{err[:30]}")
        raise HTTPException(400, err)

    fut = pending_asks.get(reply_env.correlation_id)
    if fut is None:
        raise HTTPException(404, f"No pending ask with correlation_id={reply_env.correlation_id}")
    if fut.done():
        raise HTTPException(409, "Ask already resolved")

    # Sklopi reply envelope kao response za alice (sa HMAC-om koji alice moze da validira)
    fut.set_result(env_dict)
    log(env_dict, "reply_delivered")
    return {"ok": True}


@app.get("/inbox/{agent_id}")
async def get_inbox(agent_id: str, sender: Annotated[str, Depends(authenticate)], max_items: int = 50) -> dict[str, Any]:
    """Drenira inbox za agent_id. Sender MORA biti agent_id (ne moze citati tudji inbox)."""
    if agent_id != sender:
        raise HTTPException(403, f"Cannot read inbox of '{agent_id}' as '{sender}'")

    items = inbox[agent_id][:max_items]
    del inbox[agent_id][:max_items]
    if items:
        log({"from_agent": "-", "to_agent": agent_id, "kind": "inbox"}, f"drained_{len(items)}")
    return {"messages": items, "count": len(items)}


@app.get("/audit")
async def get_audit(sender: Annotated[str, Depends(authenticate)], tail: int = 100) -> dict[str, Any]:
    """Audit pristup — admin-only u Fazi 2+; sada svaki authenticated agent moze."""
    return {"entries": audit[-tail:], "total": len(audit)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7777, log_level="warning")

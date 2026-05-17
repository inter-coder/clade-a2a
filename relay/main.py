"""Clade Relay — Faza 0 MVP.

In-memory message broker. Bez auth, bez HMAC, bez persistence. Localhost only.
Cilj: dokazati protokol radi. Production-grade dolazi u Faza 1+.
"""

import asyncio
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ---- In-memory state ----

# inbox[agent_id] = list of envelopes (FIFO via append + drain)
inbox: dict[str, list[dict[str, Any]]] = defaultdict(list)

# pending_asks[correlation_id] = asyncio.Future, resolved by /reply
pending_asks: dict[str, asyncio.Future] = {}

# Audit log u memoriji — za prvi debug
audit: list[dict[str, Any]] = []


# ---- Models ----

class Envelope(BaseModel):
    """Poruka koja putuje kroz relay."""
    msg_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_agent: str
    to_agent: str
    kind: str  # "send" | "ask" | "reply"
    correlation_id: str | None = None
    payload: dict[str, Any]
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class ReplyBody(BaseModel):
    correlation_id: str
    response: dict[str, Any]


class AskTimeout(BaseModel):
    timeout_s: int = 120


# ---- Lifecycle ----

@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("[relay] startup — in-memory mode, no auth, listening on http://localhost:7777")
    yield
    print("[relay] shutdown")


app = FastAPI(title="Clade Relay (POC)", lifespan=lifespan)


# ---- Helpers ----

def log(env: dict[str, Any], status: str) -> None:
    entry = {
        "ts": int(time.time() * 1000),
        "msg_id": env.get("msg_id"),
        "from": env.get("from_agent"),
        "to": env.get("to_agent"),
        "kind": env.get("kind"),
        "status": status,
    }
    audit.append(entry)
    if len(audit) > 1000:
        del audit[:500]  # trim
    print(f"[relay] {status:20s} {env.get('from_agent')} → {env.get('to_agent')} ({env.get('kind')})")


# ---- Endpoints ----

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "inbox_sizes": {k: len(v) for k, v in inbox.items()},
        "pending_asks": len(pending_asks),
        "audit_count": len(audit),
    }


@app.post("/send")
async def send(env: Envelope) -> dict[str, Any]:
    """Fire-and-forget. Stavlja u inbox primaoca."""
    body = env.model_dump()
    inbox[env.to_agent].append(body)
    log(body, "delivered")
    return {"ok": True, "msg_id": env.msg_id}


@app.post("/ask")
async def ask(env: Envelope, body: AskTimeout = AskTimeout()) -> dict[str, Any]:
    """Sinhroni ask. Stavlja u inbox + blokira do reply ili timeout-a."""
    if not env.correlation_id:
        raise HTTPException(400, "correlation_id required for ask")

    envelope = env.model_dump()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    pending_asks[env.correlation_id] = fut

    inbox[env.to_agent].append(envelope)
    log(envelope, "asked")

    try:
        response = await asyncio.wait_for(fut, timeout=body.timeout_s)
        log(envelope, "ask_replied")
        return {"ok": True, "response": response, "msg_id": env.msg_id}
    except asyncio.TimeoutError:
        log(envelope, "ask_timeout")
        raise HTTPException(504, f"Ask timed out after {body.timeout_s}s")
    finally:
        pending_asks.pop(env.correlation_id, None)


@app.post("/reply")
async def reply(body: ReplyBody) -> dict[str, Any]:
    """Odgovor na pending ask. Otkljucava /ask poziv."""
    fut = pending_asks.get(body.correlation_id)
    if fut is None:
        raise HTTPException(404, f"No pending ask with correlation_id={body.correlation_id}")
    if fut.done():
        raise HTTPException(409, "Ask already resolved")
    fut.set_result(body.response)
    log({"msg_id": body.correlation_id, "from_agent": "?", "to_agent": "?", "kind": "reply"}, "reply_received")
    return {"ok": True}


@app.get("/inbox/{agent_id}")
async def get_inbox(agent_id: str, max_items: int = 50) -> dict[str, Any]:
    """Drenira inbox za agent_id (do max_items)."""
    items = inbox[agent_id][:max_items]
    del inbox[agent_id][:max_items]
    if items:
        log({"msg_id": "-", "from_agent": "-", "to_agent": agent_id, "kind": "inbox_drain"}, f"drained_{len(items)}")
    return {"messages": items, "count": len(items)}


@app.get("/audit")
async def get_audit(tail: int = 100) -> dict[str, Any]:
    """Vraca poslednjih `tail` audit entry-ja."""
    return {"entries": audit[-tail:], "total": len(audit)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7777, log_level="warning")

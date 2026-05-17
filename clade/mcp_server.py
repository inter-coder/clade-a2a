"""MCP HTTP endpoint za clade serve — Claude Code klijent se ovde konektuje.

Wraps fastmcp 3.x `FastMCP.http_app()` (Starlette ASGI), dodaje `/health`
rutu, vraca jedan ASGI app spreman za uvicorn. Tools (`clade_message`,
`clade_outbox_status`) zatvaraju nad pozivajucim `Server` instance-om kao
closure — clean DI bez globalnih varijabli.

PR#4.5 popunjava gap iz PR#2 (samozapazanja §2.1: HTTP server treba da ima
i /mcp i /health). Sad obe rute idu kroz fastmcp Starlette app na istom
http_port-u (peer.http_port iz peers.yaml)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from clade import __version__
from clade.envelope import Envelope
from clade.outbox import send_with_retry
from clade.thread_cache import ThreadMsg

if TYPE_CHECKING:
    from clade.server import Server

LOG = logging.getLogger("clade.mcp")


def build_mcp_app(server: "Server") -> Any:
    """Vrati Starlette ASGI app sa MCP rutama + /health.

    `server` se cuva kao closure — tools mu pristupaju za audit, outbox,
    thread_cache, cfg. Vraca tip je intentional `Any` jer Starlette tip iz
    fastmcp nije stabilno expose-ovan; uvicorn ga prihvata svaki ASGI."""
    mcp = FastMCP(f"clade-{server.me_id}")

    @mcp.tool()
    async def clade_message(
        to: str,
        content: dict[str, Any] | str,
        reply_to: str | None = None,
        expect_reply: bool = False,
        timeout_s: int = 90,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Posalji poruku peer-u. Unifikovan outbound tool (v2 protokol §5).

        Args:
            to: peer ID iz allowlist-a
            content: dict ili string (string se omotava u {"text": str})
            reply_to: msg_id roditeljske poruke (top-level polje u v2)
            expect_reply: True = sinhroni ask, blokira do reply
            timeout_s: gornji limit (samo kad expect_reply=True)
            thread_id: thread persistence ID (top-level polje u v2)

        Returns: za fire-and-forget {"ok": True, "msg_id"} ili
            {"ok": True, "msg_id", "queued": True}; za expect_reply=True
            {"ok": True, "response": <payload>}; ili {"error": str}.
        """
        del timeout_s  # PR#4.5 koristi default iz transport-a (130s); per-call timeout
                      # za HttpRemoteTransport relay path-a dolazi kasnije
        return await _do_clade_message(
            server, to, content, reply_to, expect_reply, thread_id,
        )

    @mcp.tool()
    async def clade_outbox_status() -> dict[str, Any]:
        """Status outbox-a (pending + dead-letter brojevi)."""
        assert server.outbox is not None
        stats = server.outbox.stats()
        return {
            "peer": server.me_id,
            "pending": stats["pending"],
            "dead": stats["dead"],
        }

    # fastmcp http_app vraca Starlette app; dodajemo /health kao top-level rutu
    app = mcp.http_app(transport="http")

    async def health(_req):  # type: ignore[no-untyped-def]
        assert server.audit is not None
        assert server.outbox is not None
        outbox_stats = server.outbox.stats()
        return JSONResponse({
            "peer": server.me_id,
            "version": __version__,
            "protocol_version": "2.0.0",
            "uptime_s": int(time.monotonic() - server._start_time_s),
            "inbox_processed_total": server._metrics_inbox_count,
            "last_message_at_ms": server._metrics_last_msg_ts_ms,
            "thread_cache_size": server.thread_cache.size(),
            "audit_count": server.audit.count(),
            "outbox_pending": outbox_stats["pending"],
            "outbox_dead": outbox_stats["dead"],
            "socket": server.me.socket,
        })

    app.routes.append(Route("/health", health, methods=["GET"]))
    return app


async def _do_clade_message(
    server: "Server",
    to: str,
    content: dict[str, Any] | str,
    reply_to: str | None,
    expect_reply: bool,
    thread_id: str | None,
) -> dict[str, Any]:
    """Pure logika clade_message tool-a — odvojeno radi testabilnosti."""
    # Build payload
    if isinstance(content, str):
        payload: dict[str, Any] = {"text": content}
    elif isinstance(content, dict):
        payload = dict(content)
    else:
        return {"error": f"content mora biti str ili dict, dobio {type(content).__name__}"}

    # Resolve peer
    try:
        peer = server.cfg.other(to)
    except ValueError as e:
        return {"error": str(e)}

    # Routing po peer.transport (v2.2.0 podrzava unix + relay)
    if peer.transport == "unix":
        if not peer.socket:
            return {"error": f"peer '{to}' nema socket path u peers.yaml"}
        to_url = f"unix://{peer.socket}"
    elif peer.transport == "relay":
        if not peer.relay_url:
            return {"error": f"peer '{to}' nema relay_url u peers.yaml"}
        if not peer.secret_hex:
            return {"error": f"peer '{to}' nema secret_hex (HMAC pair) u peers.yaml"}
        to_url = peer.relay_url
    else:
        return {"error": f"peer '{to}' ima nepoznat transport={peer.transport}"}

    # Build envelope (thread_id i reply_to su top-level u v2 — vidi §5)
    env = Envelope.new(
        from_agent=server.me_id,
        to_agent=to,
        kind="ask" if expect_reply else "send",
        payload=payload,
        correlation_id=str(uuid.uuid4()) if expect_reply else None,
        thread_id=thread_id,
        reply_to=reply_to,
    )

    # HMAC sign za relay path (v2.2.0). Unix path ne treba — filesystem perms guard.
    if peer.transport == "relay":
        from clade.hmac import sign as hmac_sign  # noqa: PLC0415
        env = env.model_copy(update={"hmac": hmac_sign(env, peer.secret_hex)})

    # Record outbound u thread cache (ako ima thread_id)
    if thread_id:
        server.thread_cache.append(thread_id, ThreadMsg(
            msg_id=env.msg_id, ts_ms=env.timestamp_ms, direction="out",
            peer=to, kind=env.kind, payload=env.payload,
        ))

    # Send sa retry + outbox semantikom
    assert server.audit is not None and server.outbox is not None
    result = await send_with_retry(
        server._deliver_via_transport, env, to_url,
        server.audit, server.outbox,
    )

    if result.delivered:
        if expect_reply:
            if result.response is None:
                return {"error": "expect_reply=True but no response received"}
            # Record reply u thread cache (incoming)
            if thread_id:
                server.thread_cache.append(thread_id, ThreadMsg(
                    msg_id=result.response.msg_id,
                    ts_ms=result.response.timestamp_ms,
                    direction="in", peer=to, kind="reply",
                    payload=result.response.payload,
                ))
            return {
                "ok": True,
                "response": result.response.payload,
                "correlation_id": env.correlation_id,
            }
        return {"ok": True, "msg_id": env.msg_id}

    if result.queued:
        return {
            "ok": True, "msg_id": env.msg_id, "queued": True,
            "note": "relay/peer unreachable, queued for retry",
        }

    return {"error": result.error or "delivery failed"}

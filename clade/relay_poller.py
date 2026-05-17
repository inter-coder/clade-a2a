"""Relay inbox poller — cross-host receive path.

Za peer-a sa `self.transport == "relay"`: pollu-je relay `/inbox/<self_id>`
svake `POLL_INTERVAL_S` sekunde. Za svaku poruku verifikuje HMAC po
pair-wise secret-u, prosledjuje u `Server._on_peer_envelope` kao da je
stigla direktno (isti handler logic).

Reply path: ako handler vrati `Response(envelope=reply)`, reply ide nazad
kroz `HttpRemoteTransport.deliver()` na `/reply` endpoint. relay reroutes
preko pending_asks future-a senderu.

Sluzi za multi-machine deploy (v2.2.0 §11)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from clade.envelope import Envelope, check_protocol_compat
from clade.hmac import verify as hmac_verify

if TYPE_CHECKING:
    from clade.server import Server

LOG = logging.getLogger("clade.relay_poll")

POLL_INTERVAL_S = 2.0
INBOX_MAX_DRAIN = 50
HTTP_TIMEOUT_S = 10.0


async def relay_poll_loop(server: "Server") -> None:
    """Background task — pollu-je relay /inbox/<self_id> dok shutdown_event ne stigne.

    Konekcija na relay je per-iteration (kratkog veka) — drzimo httpx.AsyncClient
    persistent kroz loop ali svaki request je samostalan. Connection refused /
    timeout → log + nastavi (relay moze biti privremeno down)."""
    me_entry = server.me
    me_id = server.me_id

    if me_entry.transport != "relay":
        LOG.warning("relay_poll_loop called for non-relay self peer; not polling")
        return
    if not me_entry.relay_url:
        LOG.error("relay_poll_loop: self peer %s nema relay_url", me_id)
        return
    if not me_entry.bearer_token:
        LOG.error("relay_poll_loop: self peer %s nema bearer_token (auth ka relay-u)", me_id)
        return

    relay_url = me_entry.relay_url.rstrip("/")
    headers = {"Authorization": f"Bearer {me_entry.bearer_token}"}
    LOG.info("relay poll loop: %s/inbox/%s svake %.1fs", relay_url, me_id, POLL_INTERVAL_S)

    consecutive_errors = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        while not server._shutdown_event.is_set():
            try:
                await _drain_inbox_once(server, client, relay_url, me_id, headers)
                consecutive_errors = 0
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                consecutive_errors += 1
                if consecutive_errors == 1 or consecutive_errors % 30 == 0:
                    LOG.warning("relay unreachable (%s) — retry...", type(e).__name__)
            except Exception as e:
                LOG.exception("relay poll iteration unexpected error: %s", e)

            try:
                await asyncio.wait_for(server._shutdown_event.wait(), timeout=POLL_INTERVAL_S)
                return  # shutdown
            except asyncio.TimeoutError:
                pass  # nastavi sledecu iteraciju


async def _drain_inbox_once(
    server: "Server", client: httpx.AsyncClient, relay_url: str,
    me_id: str, headers: dict,
) -> None:
    """Jedan poll cycle: GET /inbox/<me>, parse, forward svaku poruku."""
    r = await client.get(
        f"{relay_url}/inbox/{me_id}",
        params={"max_items": INBOX_MAX_DRAIN},
        headers=headers,
    )
    if r.status_code != 200:
        LOG.warning("relay /inbox/%s HTTP %d: %s", me_id, r.status_code, r.text[:200])
        return

    data = r.json()
    messages = data.get("messages") or []
    if not messages:
        return

    for raw_env in messages:
        await _handle_relay_envelope(server, raw_env, client, relay_url, headers)


async def _handle_relay_envelope(
    server: "Server", raw_env: dict, client: httpx.AsyncClient,
    relay_url: str, headers: dict,
) -> None:
    """Parse + verify HMAC + forward u Server._on_peer_envelope. Ako handler
    vrati reply envelope (kind=ask response), POST na relay /reply."""
    try:
        env = Envelope.model_validate(raw_env)
    except Exception as e:
        LOG.warning("relay envelope invalid schema: %s", e)
        return

    # Protocol version check (defense in depth — Server._on_peer_envelope vec radi
    # to, ali ovde rano prelama na log nivou bez audit drift-a)
    ok, err = check_protocol_compat(env.protocol_version)
    if not ok:
        LOG.warning("protocol mismatch in relay envelope from %s: %s", env.from_agent, err)
        # I dalje prosledi u handler — handler ce vratiti error reply

    # HMAC verify (kriticno za cross-host — relay je polu-poverljiv)
    peer_entry = server.cfg.peers.get(env.from_agent)
    if peer_entry is None:
        LOG.warning("relay envelope from unknown peer '%s' — rejecting", env.from_agent)
        return
    if peer_entry.secret_hex:
        if not hmac_verify(env, peer_entry.secret_hex):
            LOG.warning("relay envelope from %s HMAC FAIL — rejecting", env.from_agent)
            assert server.audit is not None
            server.audit.record(env, direction="in", status="rejected",
                                error="HMAC verification failed")
            return
    else:
        LOG.warning("peer %s nema secret_hex u peers.yaml — primam bez HMAC verifikacije",
                    env.from_agent)

    # Forward u standardni handler
    response = await server._on_peer_envelope(env)
    if response.envelope is None:
        return  # fire-and-forget primljen, nista za odgovor

    # Reply: posalji preko relay /reply. Mora biti HMAC-signed isto.
    reply_env = response.envelope
    if peer_entry.secret_hex:
        from clade.hmac import sign as hmac_sign  # noqa: PLC0415
        signed_hmac = hmac_sign(reply_env, peer_entry.secret_hex)
        reply_env = reply_env.model_copy(update={"hmac": signed_hmac})

    try:
        r = await client.post(
            f"{relay_url}/reply", json=reply_env.model_dump(), headers=headers,
        )
        if r.status_code != 200:
            LOG.warning("relay /reply HTTP %d for msg=%s: %s",
                        r.status_code, reply_env.msg_id, r.text[:200])
        else:
            LOG.info("relay reply delivered: msg=%s → %s (corr=%s)",
                     reply_env.msg_id, reply_env.to_agent, reply_env.correlation_id)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        LOG.warning("relay /reply failed: %s — sender ce dobiti timeout", e)

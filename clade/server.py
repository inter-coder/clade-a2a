"""`clade serve --peer X` — always-on proces po peer-u.

Spaja sledece u jedan asyncio event loop (samozapazanja §2.1):
- UnixSocketTransport.serve() — peer-to-peer inbound poruke
- aiohttp HTTP server — /health endpoint (MCP HTTP endpoint dolazi kasnije)
- Audit DB write-through na svaku inbound poruku
- ThreadCache update na poruke sa thread_id-em
- sd_notify watchdog ping (READY/WATCHDOG/STOPPING)
- Graceful shutdown: SIGTERM → drain in-flight, checkpoint WAL, cleanup socket

Ne implementira (jos):
- MCP HTTP endpoint (Claude Code klijent) — PR#4/5
- Outbox sender-driven retry — PR#3
- peers.yaml v2 schema (transport/role polja) — PR#4
- Cross-host kroz HttpRemoteTransport (relay polling) — PR#4

PR#2 cilj: 'serve' moze da prima envelope-e preko unix socket-a, da ih trajno
zapise u audit DB, da odrazi thread cache, i da gracefully zatvori. To je
dovoljno za side-by-side test sa v1.2 daemon-om (oni se ne kose — drugaciji
transport)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from clade import __version__
from clade.audit import Audit
from clade.envelope import Envelope
from clade.peers_config import PeerEntry, PeersConfig
from clade.thread_cache import ThreadCache, ThreadMsg
from clade.transport.types import Response
from clade.transport.unix import UnixSocketTransport

LOG = logging.getLogger("clade.serve")

WATCHDOG_INTERVAL_S = 30.0
SHUTDOWN_DRAIN_TIMEOUT_S = 10.0

# Server-side handler signature (envelope → Response)
PeerHandler = Callable[[Envelope], Awaitable[Response]]


class Server:
    """Glavni `clade serve` orchestrator. Drzi sve resurse + lifecycle."""

    def __init__(self, cfg: PeersConfig) -> None:
        self.cfg = cfg
        self.me_id = cfg.self
        self.me: PeerEntry = cfg.me()
        self._validate_me()

        # Resurse — sve se inicijalizuje u start(), zatvara u stop()
        self.audit: Audit | None = None
        self.thread_cache = ThreadCache()
        self.unix_transport: UnixSocketTransport | None = None
        self.http_runner: web.AppRunner | None = None
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._start_time_s: float = 0.0
        self._metrics_inbox_count = 0
        self._metrics_last_msg_ts_ms: int | None = None

    def _validate_me(self) -> None:
        if not self.me.socket:
            raise ValueError(f"peers.yaml: peers['{self.me_id}'].socket je obavezan za serve")
        if not self.me.http_port:
            raise ValueError(f"peers.yaml: peers['{self.me_id}'].http_port je obavezan za serve")
        if not self.me.audit_db:
            raise ValueError(f"peers.yaml: peers['{self.me_id}'].audit_db je obavezan za serve")

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Pokreni sve listenere + background task-ove. Vraca se kad je sve up."""
        self._start_time_s = time.monotonic()
        LOG.info("starting clade-%s (v%s)", self.me_id, __version__)

        assert self.me.audit_db is not None
        self.audit = Audit(self.me.audit_db)
        LOG.info("audit db: %s", self.me.audit_db)

        # Peer-to-peer unix socket listener
        assert self.me.socket is not None
        self.unix_transport = UnixSocketTransport(socket_path=self.me.socket)
        self._tasks.append(asyncio.create_task(
            self.unix_transport.serve(self._on_peer_envelope),
            name="unix_serve",
        ))
        LOG.info("unix socket listener: %s", self.me.socket)

        # HTTP /health server (aiohttp)
        self.http_runner = await self._start_http_server()
        LOG.info("http /health: http://127.0.0.1:%d/health", self.me.http_port)

        # sd_notify integration (no-op ako ne tece pod systemd)
        notify_ready()
        self._tasks.append(asyncio.create_task(self._watchdog_loop(), name="watchdog"))

        LOG.info("clade-%s ready.", self.me_id)

    async def stop(self) -> None:
        """Graceful shutdown — sva background task-ovi, drain in-flight, cleanup."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        LOG.info("stopping clade-%s...", self.me_id)
        notify_stopping()

        # Stop peer transport prvo (sprecava nove inbound poruke)
        if self.unix_transport is not None:
            await self.unix_transport.stop()
        # Stop HTTP server
        if self.http_runner is not None:
            await self.http_runner.cleanup()
        # Cancel background tasks
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # Checkpoint WAL + close audit
        if self.audit is not None:
            self.audit.close()
        LOG.info("clade-%s stopped.", self.me_id)

    async def wait_forever(self) -> None:
        """Drzi proces ziv do SIGTERM/SIGINT."""
        await self._shutdown_event.wait()

    # ---- Handlers ----

    async def _on_peer_envelope(self, env: Envelope) -> Response:
        """Handler za UnixSocketTransport — poziva se za svaku inbound poruku
        od drugog peer-a. PR#2 minimum: validate, audit-record, thread-cache update.
        Za 'ask' poruke, jos uvek vracamo placeholder reply (jer MCP-spawn-Claude
        integracija dolazi kasnije)."""
        # Validacija peer-a
        if env.from_agent not in self.cfg.peers:
            LOG.warning("unknown peer '%s' — rejecting", env.from_agent)
            assert self.audit is not None
            self.audit.record(env, direction="in", status="rejected",
                              error=f"unknown peer: {env.from_agent}")
            err_env = Envelope.new(
                from_agent=self.me_id, to_agent=env.from_agent, kind="reply",
                payload={"_error": f"unknown peer: {env.from_agent}"},
                correlation_id=env.correlation_id,
            )
            return Response(envelope=err_env)
        if env.to_agent != self.me_id:
            LOG.warning("envelope to_agent='%s' but I am '%s'", env.to_agent, self.me_id)
            assert self.audit is not None
            self.audit.record(env, direction="in", status="rejected",
                              error=f"to_agent mismatch (expected {self.me_id})")
            return Response(envelope=None)

        # Audit + thread cache
        assert self.audit is not None
        self.audit.record(env, direction="in", status="delivered")
        if env.thread_id:
            self.thread_cache.append(env.thread_id, ThreadMsg(
                msg_id=env.msg_id, ts_ms=env.timestamp_ms, direction="in",
                peer=env.from_agent, kind=env.kind, payload=env.payload,
            ))
        self._metrics_inbox_count += 1
        self._metrics_last_msg_ts_ms = env.timestamp_ms
        LOG.info("← %s [%s] from %s (%s)",
                 env.msg_id[:8], env.kind, env.from_agent, env.thread_id or "no-thread")

        # PR#2 placeholder za 'ask': vrati ack reply.
        # PR#4/5 ce ovo zameniti pravom claude --print spawn integracijom.
        if env.kind == "ask":
            ack = Envelope.new(
                from_agent=self.me_id, to_agent=env.from_agent, kind="reply",
                payload={"_ack": "received, claude integration coming in PR#4"},
                correlation_id=env.correlation_id,
                thread_id=env.thread_id,
            )
            self.audit.record(ack, direction="out", status="delivered")
            return Response(envelope=ack)

        return Response(envelope=None)

    # ---- HTTP server (/health) ----

    async def _start_http_server(self) -> web.AppRunner:
        app = web.Application()
        app.router.add_get("/health", self._http_health)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        assert self.me.http_port is not None
        site = web.TCPSite(runner, "127.0.0.1", self.me.http_port)
        await site.start()
        return runner

    async def _http_health(self, _req: web.Request) -> web.Response:
        assert self.audit is not None
        body: dict[str, Any] = {
            "peer": self.me_id,
            "version": __version__,
            "protocol_version": "2.0.0",
            "uptime_s": int(time.monotonic() - self._start_time_s),
            "inbox_processed_total": self._metrics_inbox_count,
            "last_message_at_ms": self._metrics_last_msg_ts_ms,
            "thread_cache_size": self.thread_cache.size(),
            "audit_count": self.audit.count(),
            "socket": self.me.socket,
        }
        return web.json_response(body)

    # ---- sd_notify watchdog ----

    async def _watchdog_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=WATCHDOG_INTERVAL_S)
                return  # shutdown trigger-ovan
            except asyncio.TimeoutError:
                notify_watchdog()


# ---- sd_notify helpers (no-op ako ne tece pod systemd) ----

def notify_ready() -> None:
    _try_notify("READY=1")


def notify_stopping() -> None:
    _try_notify("STOPPING=1")


def notify_watchdog() -> None:
    _try_notify("WATCHDOG=1")


def _try_notify(msg: str) -> None:
    """Saljemo na NOTIFY_SOCKET ako je set, inace tihi no-op.
    Direktan AF_UNIX socket bez sdnotify dependency-a — ovo je sve sto treba
    (samozapazanja open question #2: sdnotify package nije nuzan)."""
    sock_addr = os.environ.get("NOTIFY_SOCKET")
    if not sock_addr:
        return
    # Abstract sockets (@ prefix) ili obican path
    if sock_addr.startswith("@"):
        sock_addr = "\0" + sock_addr[1:]
    try:
        import socket  # noqa: PLC0415
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(sock_addr)
            s.sendall(msg.encode("utf-8"))
    except OSError:
        # Ako systemd nije up, ili NOTIFY_SOCKET je stale — ignore
        pass


# ---- Entry point ----

async def _run(cfg_path: Path, peer: str | None) -> None:
    from clade.peers_config import load  # noqa: PLC0415

    cfg = load(cfg_path)
    if peer:
        # Override self za testiranje (npr. start ali kao drugi peer u istom config-u)
        cfg = cfg.model_copy(update={"self": peer})

    server = Server(cfg)
    loop = asyncio.get_running_loop()

    def _handle_signal(sig: int) -> None:
        LOG.info("signal %s received, stopping", signal.Signals(sig).name)
        asyncio.create_task(server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await server.start()
        await server.wait_forever()
    finally:
        await server.stop()


def main() -> int:
    """`python -m clade serve --peer X --config path/to/peers.yaml`."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="clade", description="Clade A2A v2.0 — always-on peer process")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Pokreni always-on proces za peer")
    serve.add_argument("--peer", default=None,
                       help="Override 'self' iz peers.yaml (za testiranje)")
    serve.add_argument("--config", default="~/.config/clade/peers.yaml",
                       help="Path do peers.yaml (default: ~/.config/clade/peers.yaml)")
    serve.add_argument("--log-level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = parser.parse_args()

    if args.cmd == "serve":
        logging.basicConfig(
            level=args.log_level,
            format="%(asctime)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        cfg_path = Path(args.config).expanduser()
        if not cfg_path.exists():
            print(f"clade: peers.yaml ne postoji: {cfg_path}", file=sys.stderr)
            return 1
        try:
            asyncio.run(_run(cfg_path, args.peer))
        except KeyboardInterrupt:
            pass
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

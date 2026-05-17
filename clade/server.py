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

import uvicorn

from clade import __version__
from clade.ask_handler import extract_question, handle_ask
from clade.audit import Audit
from clade.envelope import (
    Envelope, PROTOCOL_MAJOR, PROTOCOL_VERSION, check_protocol_compat,
)
from clade.outbox import Outbox, outbox_retry_loop
from clade.peers_config import PeerEntry, PeersConfig
from clade.thread_cache import ThreadCache, ThreadMsg
from clade.transport.types import DeliveryResult, Response
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
        self.outbox: Outbox | None = None
        self.thread_cache = ThreadCache()
        self.unix_transport: UnixSocketTransport | None = None
        self.uvicorn_server: uvicorn.Server | None = None
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
        self.outbox = Outbox(self.audit)
        LOG.info("audit db: %s (pending outbox: %d)",
                 self.me.audit_db, self.outbox.pending_count())

        # Peer-to-peer unix socket listener
        assert self.me.socket is not None
        self.unix_transport = UnixSocketTransport(socket_path=self.me.socket)
        self._tasks.append(asyncio.create_task(
            self.unix_transport.serve(self._on_peer_envelope),
            name="unix_serve",
        ))
        LOG.info("unix socket listener: %s", self.me.socket)

        # HTTP server: /health + MCP (uvicorn + fastmcp Starlette app)
        self._tasks.append(asyncio.create_task(self._serve_http(), name="http_serve"))
        LOG.info("http /health + MCP: http://127.0.0.1:%d", self.me.http_port)

        # Outbox retry loop u pozadini (v2 PR#3)
        self._tasks.append(asyncio.create_task(
            outbox_retry_loop(self.outbox, self._deliver_via_transport,
                              self._shutdown_event, self.me_id),
            name="outbox_retry",
        ))

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
        # Stop HTTP/MCP server (uvicorn)
        if self.uvicorn_server is not None:
            self.uvicorn_server.should_exit = True
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
        integracija dolazi kasnije).

        Protocol version handshake (§2.10, v2.0): odbija envelope sa major
        mismatch-om PRE peer validation-a — clean wire-level check."""
        # Protocol version check (v2.0 handshake)
        ok, err = check_protocol_compat(env.protocol_version)
        if not ok:
            LOG.warning("protocol mismatch from %s: %s", env.from_agent, err)
            assert self.audit is not None
            self.audit.record(env, direction="in", status="rejected", error=err)
            err_env = Envelope.new(
                from_agent=self.me_id, to_agent=env.from_agent, kind="reply",
                payload={
                    "_error": "protocol_mismatch",
                    "expected": f"{PROTOCOL_MAJOR}.x.y",
                    "received": env.protocol_version,
                    "hint": f"Upgrade peer to clade>={PROTOCOL_MAJOR}.0",
                },
                correlation_id=env.correlation_id,
            )
            return Response(envelope=err_env)

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

        # v2.1.0: real ask handler — spawn claude --print sa thread context-om
        if env.kind == "ask":
            return await self._handle_ask(env)

        return Response(envelope=None)

    async def _handle_ask(self, env: Envelope) -> Response:
        """Async helper za 'ask' kind. Spawn-uje claude --print kroz
        clade.ask_handler i salje reply. Ako workdir nije konfigurisan,
        vraca error reply umesto crash-a."""
        if not self.me.workdir:
            err_msg = f"peer '{self.me_id}' nema 'workdir' u peers.yaml — ask handler ne moze da spawn-uje claude"
            LOG.warning(err_msg)
            err_env = Envelope.new(
                from_agent=self.me_id, to_agent=env.from_agent, kind="reply",
                payload={"_error": err_msg},
                correlation_id=env.correlation_id, thread_id=env.thread_id,
            )
            assert self.audit is not None
            self.audit.record(err_env, direction="out", status="delivered")
            return Response(envelope=err_env)

        question = extract_question(env.payload)
        thread_history = (
            self.thread_cache.get_context(env.thread_id) if env.thread_id else []
        )
        # Iskljuci trenutnu ask poruku iz history-ja (vec smo je append-ovali iznad)
        thread_history = [m for m in thread_history if m.msg_id != env.msg_id]

        from pathlib import Path  # noqa: PLC0415
        answer = await handle_ask(
            question=question,
            from_peer=env.from_agent,
            my_id=self.me_id,
            workdir=Path(self.me.workdir),
            thread_history=thread_history,
        )

        reply = Envelope.new(
            from_agent=self.me_id, to_agent=env.from_agent, kind="reply",
            payload={"answer": answer},
            correlation_id=env.correlation_id,
            thread_id=env.thread_id,
        )
        assert self.audit is not None
        self.audit.record(reply, direction="out", status="delivered")
        # Record reply u thread cache (outgoing)
        if env.thread_id:
            from clade.thread_cache import ThreadMsg  # noqa: PLC0415
            self.thread_cache.append(env.thread_id, ThreadMsg(
                msg_id=reply.msg_id, ts_ms=reply.timestamp_ms, direction="out",
                peer=env.from_agent, kind="reply", payload=reply.payload,
            ))
        return Response(envelope=reply)

    # ---- HTTP server: MCP + /health (uvicorn + fastmcp Starlette) ----

    async def _serve_http(self) -> None:
        """Pokrene uvicorn na 127.0.0.1:http_port sa MCP + /health rutama.
        Blokira do should_exit=True (postavljeno u stop()).

        NAPOMENA: uvicorn po default-u postavi sopstvene signal handler-e za
        SIGTERM/SIGINT koji bi pojeli nase (vidi `main._handle_signal`).
        Eksplicitno ih iskljucujemo da nas `Server.stop()` — koji brise unix
        socket + WAL checkpoint + outbox cleanup — bude pozvan na shutdown."""
        from clade.mcp_server import build_mcp_app  # noqa: PLC0415

        app = build_mcp_app(self)
        assert self.me.http_port is not None
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.me.http_port,
            log_level="warning", access_log=False, lifespan="on",
        )
        self.uvicorn_server = uvicorn.Server(config)
        self.uvicorn_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        await self.uvicorn_server.serve()

    # ---- Transport dispatch (za outbox retry) ----

    async def _deliver_via_transport(self, envelope: Envelope, to_url: str) -> DeliveryResult:
        """Bira odgovarajuci transport na osnovu `to_url` schema-a.
        - unix:// → UnixSocketTransport client mode
        - http(s):// → HttpRemoteTransport (relay-mediated, dolazi u PR#4)

        Trenutno samo unix; remote dolazi sa peers.yaml v2 schema-om."""
        if to_url.startswith("unix://"):
            client = UnixSocketTransport()
            return await client.deliver(envelope, to_url=to_url)
        # PR#4: HttpRemoteTransport ako transport=relay
        return DeliveryResult(ok=False, error=f"unsupported to_url scheme: {to_url}")

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
    """`python -m clade <subcommand>`. Subkomande: serve, init."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="clade", description="Clade A2A v2.0 — peer-to-peer agent komunikacija")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # serve
    serve_p = sub.add_parser("serve", help="Pokreni always-on proces za peer")
    serve_p.add_argument("--peer", default=None,
                         help="Override 'self' iz peers.yaml (za testiranje)")
    serve_p.add_argument("--config", default="~/.config/clade/peers.yaml",
                         help="Path do peers.yaml (default: ~/.config/clade/peers.yaml)")
    serve_p.add_argument("--log-level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # init
    init_p = sub.add_parser("init", help="Bootstrap peers.yaml + systemd unit + .mcp.json")
    init_p.add_argument("--self", required=True, dest="self",
                        help="Ime peer-a na ovoj masini (npr. katana)")
    init_p.add_argument("--peer", action="append", default=[],
                        help="Drugi peer (moze ponoviti: --peer dusan --peer predrag). "
                             "Ignorise se ako peers.yaml vec postoji.")
    init_p.add_argument("--apply", action="store_true",
                        help="Stvarno upisi fajlove (default: dry-run preview)")
    init_p.add_argument("--config", default=None,
                        help="Override default ~/.config/clade/peers.yaml path-a")

    # status
    status_p = sub.add_parser("status", help="Pingaj /health i prikazi citljivo")
    status_p.add_argument("--peer", default=None,
                          help="Override 'self' (citaj health drugog peer-a na istoj masini)")
    status_p.add_argument("--config", default="~/.config/clade/peers.yaml")

    # logs
    logs_p = sub.add_parser("logs", help="Tail audit DB (opciono + journalctl)")
    logs_p.add_argument("--peer", default=None, help="Override 'self'")
    logs_p.add_argument("--peer-filter", default=None, dest="peer_filter",
                        help="Filter audit po peer kolone (suzava log na razmenu sa jednim peer-om)")
    logs_p.add_argument("--tail", type=int, default=20,
                        help="Broj zapisa za prikazati (default 20)")
    logs_p.add_argument("--journal", action="store_true",
                        help="I prikazi journalctl --user -u clade-<peer> output")
    logs_p.add_argument("--config", default="~/.config/clade/peers.yaml")

    # send
    send_p = sub.add_parser("send", help="One-shot poruka peer-u (debug)")
    send_p.add_argument("--to", required=True, help="Target peer ID")
    send_p.add_argument("message", help="Sadrzaj poruke (string)")
    send_p.add_argument("--expect-reply", action="store_true", dest="expect_reply",
                        help="Sinhroni ask — blokira do reply ili timeout")
    send_p.add_argument("--timeout", type=int, default=90,
                        help="timeout_s za --expect-reply (default 90)")
    send_p.add_argument("--thread", default=None, help="thread_id (opciono)")
    send_p.add_argument("--config", default="~/.config/clade/peers.yaml")

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
            print(f"clade: pokreni `clade init --self <ime> --peer <drugi>` prvo", file=sys.stderr)
            return 1
        try:
            asyncio.run(_run(cfg_path, args.peer))
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "init":
        from clade.init import cli_init  # noqa: PLC0415
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        return cli_init(args)

    if args.cmd == "status":
        from clade.cli import cli_status  # noqa: PLC0415
        return cli_status(args)

    if args.cmd == "logs":
        from clade.cli import cli_logs  # noqa: PLC0415
        return cli_logs(args)

    if args.cmd == "send":
        from clade.cli import cli_send  # noqa: PLC0415
        return cli_send(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())

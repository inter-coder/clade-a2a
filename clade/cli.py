"""CLI subkomande za clade: status, logs, send.

Po samozapazanja §2.13. Sve tri ocekuju da je clade serve pokrenut na localhost
(citaju peers.yaml za pronaci http_port + audit_db + socket path-ove).

- `clade status [--peer X]` — httpx GET /health, formatira citljivo
- `clade logs [--peer X] [--tail N] [--journal]` — read audit DB tail; sa
  --journal i journalctl --user output
- `clade send --to X <message>` — fastmcp.Client poziva clade_message tool"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx

from clade.audit import Audit
from clade.peers_config import load


def cli_status(args) -> int:  # type: ignore[no-untyped-def]
    """`clade status` — pingaj /health i prikaze citljivo."""
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"clade status: peers.yaml ne postoji: {cfg_path}", file=sys.stderr)
        return 1
    try:
        cfg = load(cfg_path)
    except Exception as e:
        print(f"clade status: invalid peers.yaml: {e}", file=sys.stderr)
        return 1

    target = args.peer or cfg.self
    if target not in cfg.peers:
        print(f"clade status: peer '{target}' nije u peers.yaml", file=sys.stderr)
        return 1
    peer = cfg.peers[target]
    if not peer.http_port:
        print(f"clade status: peer '{target}' nema http_port (transport={peer.transport})",
              file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{peer.http_port}/health"
    try:
        r = httpx.get(url, timeout=2)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"clade status: {target} unreachable na {url}: {type(e).__name__}",
              file=sys.stderr)
        print(f"clade status: proveri sa: systemctl --user status clade-{target}",
              file=sys.stderr)
        return 2

    if r.status_code != 200:
        print(f"clade status: HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 2

    data = r.json()
    _print_status_table(data)
    return 0


def _print_status_table(data: dict) -> None:
    """Formatiraj /health JSON kao citljiv blok."""
    print(f"peer:                {data.get('peer', '?')}")
    print(f"version:             {data.get('version', '?')}")
    print(f"protocol:            {data.get('protocol_version', '?')}")
    uptime = data.get('uptime_s', 0)
    print(f"uptime:              {_fmt_duration(uptime)}")
    print(f"audit records:       {data.get('audit_count', 0)}")
    print(f"inbox processed:     {data.get('inbox_processed_total', 0)}")
    print(f"thread cache:        {data.get('thread_cache_size', 0)} threads")
    pending = data.get('outbox_pending', 0)
    dead = data.get('outbox_dead', 0)
    if pending or dead:
        print(f"outbox:              {pending} pending, {dead} dead-letter")
    else:
        print(f"outbox:              0")
    last_ms = data.get('last_message_at_ms')
    if last_ms:
        from datetime import datetime  # noqa: PLC0415
        ts = datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        print(f"last message:        {ts}")
    sock = data.get('socket')
    if sock:
        print(f"peer socket:         {sock}")


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m"


def cli_logs(args) -> int:  # type: ignore[no-untyped-def]
    """`clade logs` — audit DB tail. Sa --journal: i journalctl output."""
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"clade logs: peers.yaml ne postoji: {cfg_path}", file=sys.stderr)
        return 1
    cfg = load(cfg_path)
    target = args.peer or cfg.self
    if target not in cfg.peers:
        print(f"clade logs: peer '{target}' nije u peers.yaml", file=sys.stderr)
        return 1
    peer = cfg.peers[target]
    if not peer.audit_db:
        print(f"clade logs: peer '{target}' nema audit_db (verovatno not self)",
              file=sys.stderr)
        return 1

    audit_path = Path(peer.audit_db).expanduser()
    if not audit_path.exists():
        print(f"clade logs: audit DB ne postoji: {audit_path}", file=sys.stderr)
        print(f"clade logs: clade serve mora bar jednom da pokrene da kreira DB",
              file=sys.stderr)
        return 2

    audit = Audit(audit_path)
    try:
        rows = audit.tail(n=args.tail, peer=args.peer_filter)
    finally:
        audit.close()

    if not rows:
        print("(nema audit zapisa)")
    else:
        for row in rows:
            from datetime import datetime  # noqa: PLC0415
            ts = datetime.fromtimestamp(row.ts_ms / 1000).strftime("%H:%M:%S")
            arrow = "←" if row.direction == "in" else "→"
            thr = f" thread={row.thread_id[:8]}" if row.thread_id else ""
            payload_str = json.dumps(row.payload, ensure_ascii=False)[:120]
            print(f"{ts}  {arrow} {row.peer:12s}  [{row.kind:5s}] {row.status:10s}{thr}  {payload_str}")

    if args.journal:
        print("\n--- journalctl --user -u clade-{} (last {} lines) ---".format(target, args.tail))
        try:
            subprocess.run(
                ["journalctl", "--user", "-u", f"clade-{target}",
                 "--no-pager", "-n", str(args.tail)],
                check=False,
            )
        except FileNotFoundError:
            print("clade logs: journalctl nije nadjen (samo na systemd masinama)",
                  file=sys.stderr)
    return 0


def cli_send(args) -> int:  # type: ignore[no-untyped-def]
    """`clade send --to X <message>` — one-shot send kroz local clade serve."""
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"clade send: peers.yaml ne postoji: {cfg_path}", file=sys.stderr)
        return 1
    cfg = load(cfg_path)
    me = cfg.me()
    if not me.http_port:
        print(f"clade send: self peer '{cfg.self}' nema http_port", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{me.http_port}/mcp/"

    async def go() -> dict:
        from fastmcp import Client  # noqa: PLC0415
        async with Client(url) as c:
            kwargs: dict = {"to": args.to, "content": args.message}
            if args.expect_reply:
                kwargs["expect_reply"] = True
                kwargs["timeout_s"] = args.timeout
            if args.thread:
                kwargs["thread_id"] = args.thread
            result = await c.call_tool("clade_message", kwargs)
            return result.structured_content or {}

    try:
        data = asyncio.run(go())
    except Exception as e:
        print(f"clade send: poziv neuspesan: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"clade send: proveri da clade serve tece za '{cfg.self}'",
              file=sys.stderr)
        return 2

    # fastmcp wraps return u {"result": ...} ako tool vrati dict
    if "result" in data:
        data = data["result"]
    if "error" in data:
        print(f"error: {data['error']}", file=sys.stderr)
        return 3
    if data.get("queued"):
        print(f"queued: msg_id={data.get('msg_id')} (peer unreachable, ce retry)")
    elif "response" in data:
        print(f"reply: {json.dumps(data['response'], ensure_ascii=False)}")
    else:
        print(f"delivered: msg_id={data.get('msg_id')}")
    return 0

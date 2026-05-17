"""PR#4.5 integration testovi za MCP HTTP endpoint u clade serve.

Pokrene `python -m clade serve` u subprocess-u, kreira fastmcp.Client koji
se konektuje na http://127.0.0.1:PORT (isti port kao /health), proverava da
clade_message i clade_outbox_status tools rade end-to-end."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastmcp import Client

from clade.envelope import Envelope
from clade.transport import UnixSocketTransport
from clade.transport.types import Response

ROOT = Path(__file__).parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve_proc_pair(tmp_path: Path):
    """Spawn-ujemo katana clade serve + dusan side-server (samo unix socket).

    Katana: full clade serve sa HTTP MCP endpoint.
    Dusan: lightweight UnixSocketTransport handler koji odgovara na ask-ove.

    Test: Claude Code (simuliran fastmcp.Client) → katana clade_message →
    unix socket → dusan handler → reply nazad."""
    port = _free_port()
    katana_sock = tmp_path / "katana.sock"
    dusan_sock = tmp_path / "dusan.sock"
    audit_db = tmp_path / "katana-audit.db"

    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "version": 2,
        "self": "katana",
        "peers": {
            "katana": {
                "transport": "unix",
                "role": "interactive",
                "socket": str(katana_sock),
                "http_port": port,
                "audit_db": str(audit_db),
            },
            "dusan": {
                "transport": "unix",
                "role": "headless",
                "socket": str(dusan_sock),
            },
        },
    }))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)

    proc = subprocess.Popen(
        [PY, "-m", "clade", "serve", "--config", str(cfg_path), "--log-level", "WARNING"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )

    # Saceka HTTP ready (max 8s — uvicorn + fastmcp je sporiji od aiohttp)
    deadline = time.time() + 8
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ready:
        proc.terminate()
        out, err = proc.communicate(timeout=3)
        raise RuntimeError(f"clade serve nije starovao za 8s. stderr={err!r}")

    yield {
        "port": port,
        "katana_sock": katana_sock,
        "dusan_sock": dusan_sock,
        "audit_db": audit_db,
        "proc": proc,
    }

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.asyncio
async def test_health_endpoint_through_uvicorn(serve_proc_pair):
    """Posle PR#4.5: /health i dalje radi (sad kroz fastmcp Starlette + uvicorn)."""
    r = httpx.get(f"http://127.0.0.1:{serve_proc_pair['port']}/health", timeout=2)
    assert r.status_code == 200
    data = r.json()
    assert data["peer"] == "katana"
    assert data["protocol_version"] == "2.0.0"
    assert "outbox_pending" in data


@pytest.mark.asyncio
async def test_mcp_client_lists_clade_tools(serve_proc_pair):
    """fastmcp.Client se konektuje na MCP endpoint i vidi clade tools."""
    async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
        tools = await c.list_tools()
        tool_names = {t.name for t in tools}
        assert "clade_message" in tool_names
        assert "clade_outbox_status" in tool_names


@pytest.mark.asyncio
async def test_mcp_clade_outbox_status_callable(serve_proc_pair):
    """Poziv clade_outbox_status preko MCP → vraca stanje outbox-a (prazno)."""
    async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
        result = await c.call_tool("clade_outbox_status", {})
        # fastmcp.Client.call_tool vraca structured_content ili content list
        data = result.structured_content or {}
        if "result" in data:  # neki tools wrap-uju
            data = data["result"]
        assert data["peer"] == "katana"
        assert data["pending"] == 0
        assert data["dead"] == 0


@pytest.mark.asyncio
async def test_mcp_clade_message_send_to_unknown_peer(serve_proc_pair):
    """clade_message ka peer-u koji nije u allowlist-u → vraca error."""
    async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
        result = await c.call_tool("clade_message", {
            "to": "predrag",  # nije u peers.yaml
            "content": "test",
        })
        data = result.structured_content or {}
        if "result" in data:
            data = data["result"]
        assert "error" in data
        assert "unknown peer" in data["error"]


@pytest.mark.asyncio
async def test_mcp_clade_message_send_to_unreachable_peer_queues(serve_proc_pair):
    """clade_message ka peer-u sa unreachable socket-om → poruka u outbox-u.

    Dusan-ov socket nije up u ovom testu — server nema dusan-side handler."""
    async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
        result = await c.call_tool("clade_message", {
            "to": "dusan",
            "content": "queued msg",
        })
        data = result.structured_content or {}
        if "result" in data:
            data = data["result"]
        # Posle in-process retry-ja iscrpljenja, ide u outbox
        assert data.get("ok") is True
        assert data.get("queued") is True

    # Health pokazuje outbox_pending=1
    r = httpx.get(f"http://127.0.0.1:{serve_proc_pair['port']}/health", timeout=2)
    assert r.json()["outbox_pending"] == 1


@pytest.mark.asyncio
async def test_mcp_clade_message_send_to_live_peer_delivers(serve_proc_pair):
    """clade_message ka peer-u koji ima ziv unix socket handler → uspesan delivery."""
    # Spawn lightweight dusan-side handler u istom procesu
    dusan_sock = serve_proc_pair["dusan_sock"]
    server = UnixSocketTransport(socket_path=dusan_sock)
    received: list[Envelope] = []

    async def handler(env: Envelope) -> Response:
        received.append(env)
        return Response(envelope=None)  # fire-and-forget — peer ne odgovara

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if dusan_sock.exists():
            break
        await asyncio.sleep(0.02)

    try:
        async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
            result = await c.call_tool("clade_message", {
                "to": "dusan",
                "content": "live test",
                "thread_id": "t-live",
            })
            data = result.structured_content or {}
            if "result" in data:
                data = data["result"]
            assert data.get("ok") is True
            assert "msg_id" in data
            assert data.get("queued") is not True

        # Daj 200ms da handler dovrši
        await asyncio.sleep(0.2)
        assert len(received) == 1
        assert received[0].from_agent == "katana"
        assert received[0].to_agent == "dusan"
        assert received[0].kind == "send"
        assert received[0].payload == {"text": "live test"}
        assert received[0].thread_id == "t-live"  # top-level u v2 envelope-u
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_mcp_clade_message_ask_roundtrip(serve_proc_pair):
    """clade_message sa expect_reply=True → ask, blokira do reply-a."""
    dusan_sock = serve_proc_pair["dusan_sock"]
    server = UnixSocketTransport(socket_path=dusan_sock)

    async def handler(env: Envelope) -> Response:
        if env.kind == "ask":
            reply = Envelope.new(
                from_agent="dusan", to_agent=env.from_agent, kind="reply",
                payload={"answer": "42"},
                correlation_id=env.correlation_id,
                thread_id=env.thread_id,
            )
            return Response(envelope=reply)
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if dusan_sock.exists():
            break
        await asyncio.sleep(0.02)

    try:
        async with Client(f"http://127.0.0.1:{serve_proc_pair['port']}/mcp/") as c:
            result = await c.call_tool("clade_message", {
                "to": "dusan",
                "content": "Sta je 7*6?",
                "expect_reply": True,
                "thread_id": "t-ask",
            })
            data = result.structured_content or {}
            if "result" in data:
                data = data["result"]
            assert data.get("ok") is True
            assert data["response"] == {"answer": "42"}
            assert "correlation_id" in data
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass

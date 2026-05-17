"""PR#2 integration test za clade.server (clade serve always-on proces).

Spawn-uje `python -m clade serve` u subprocess-u sa privremenim peers.yaml.
Posalje envelope kroz UnixSocketTransport.deliver(), proverava /health
endpoint i audit DB. Onda graceful shutdown."""

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

from clade.envelope import Envelope
from clade.transport import UnixSocketTransport

ROOT = Path(__file__).parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve_proc(tmp_path: Path):
    """Spawn `python -m clade serve` u izolovanom env-u."""
    port = _free_port()
    sock_path = tmp_path / "test.sock"
    audit_db = tmp_path / "audit.db"
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "self": "katana",
        "peers": {
            "katana": {
                "socket": str(sock_path),
                "http_port": port,
                "audit_db": str(audit_db),
            },
            "dusan": {
                "socket": "/tmp/dusan-doesnt-matter.sock",
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

    # Sacekaj da HTTP i socket budu spremni (max 5s)
    deadline = time.time() + 5
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.05)

    if not ready:
        proc.terminate()
        out, err = proc.communicate(timeout=3)
        raise RuntimeError(f"clade serve nije starovao za 5s. stdout={out!r} stderr={err!r}")

    yield {"port": port, "sock_path": sock_path, "audit_db": audit_db, "proc": proc}

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.asyncio
async def test_serve_health_endpoint(serve_proc):
    r = httpx.get(f"http://127.0.0.1:{serve_proc['port']}/health", timeout=2)
    assert r.status_code == 200
    data = r.json()
    assert data["peer"] == "katana"
    assert data["protocol_version"] == "2.0.0"
    assert data["inbox_processed_total"] == 0
    assert data["thread_cache_size"] == 0
    assert "uptime_s" in data
    # PR#3: outbox metrike u /health
    assert data["outbox_pending"] == 0
    assert data["outbox_dead"] == 0


@pytest.mark.asyncio
async def test_serve_accepts_send_envelope(serve_proc):
    """Posalji 'send' envelope kroz UnixSocketTransport.deliver() i proveri
    da je audit DB primio + health refleksuje povecan inbox_processed_total."""
    client = UnixSocketTransport()
    env = Envelope.new(
        from_agent="dusan", to_agent="katana", kind="send",
        payload={"text": "test send"}, thread_id="t-integration",
    )
    res = await client.deliver(env, to_url=f"unix://{serve_proc['sock_path']}")
    assert res.ok is True
    # send → no response
    assert res.response is None

    # Mali grace period jer audit pise asinhrono iz handler-a — ali u praksi
    # je sync within handler. /health bi trebao da uoci poruku odmah.
    await asyncio.sleep(0.1)

    r = httpx.get(f"http://127.0.0.1:{serve_proc['port']}/health", timeout=2)
    data = r.json()
    assert data["inbox_processed_total"] == 1
    assert data["thread_cache_size"] == 1
    assert data["last_message_at_ms"] == env.timestamp_ms

    # Provera audit DB-a direktno
    from clade.audit import Audit  # noqa: PLC0415
    audit = Audit(serve_proc["audit_db"])
    try:
        row = audit.by_msg_id(env.msg_id)
        assert row is not None
        assert row.direction == "in"
        assert row.peer == "dusan"
        assert row.kind == "send"
        assert row.thread_id == "t-integration"
        assert row.payload == {"text": "test send"}
        assert row.status == "delivered"
    finally:
        audit.close()


@pytest.mark.asyncio
async def test_serve_ask_without_workdir_returns_error(serve_proc):
    """v2.1.0: 'ask' envelope ide kroz ask_handler.handle_ask. Ako workdir
    nije konfigurisan u peers.yaml (kao u ovom fixture-u), vraca _error
    reply umesto crash-a — graceful degradation."""
    client = UnixSocketTransport()
    env = Envelope.new(
        from_agent="dusan", to_agent="katana", kind="ask",
        payload={"question": "Sta je?"}, correlation_id="c-test",
        thread_id="t-ask",
    )
    res = await client.deliver(env, to_url=f"unix://{serve_proc['sock_path']}")
    assert res.ok is True
    assert res.response is not None
    assert res.response.kind == "reply"
    assert res.response.correlation_id == "c-test"
    assert res.response.thread_id == "t-ask"
    assert "_error" in res.response.payload
    assert "workdir" in res.response.payload["_error"]


@pytest.mark.asyncio
async def test_serve_rejects_unknown_peer(serve_proc):
    """Envelope od peer-a koji nije u allowlist-u → error reply."""
    client = UnixSocketTransport()
    env = Envelope.new(
        from_agent="impostor", to_agent="katana", kind="ask",
        payload={}, correlation_id="c-evil",
    )
    res = await client.deliver(env, to_url=f"unix://{serve_proc['sock_path']}")
    assert res.ok is True  # deliver tehnicki radi, samo je sadrzaj error
    assert res.response is not None
    assert "_error" in res.response.payload
    assert "unknown peer" in res.response.payload["_error"]


@pytest.mark.asyncio
async def test_serve_graceful_shutdown(serve_proc):
    """SIGTERM → proces se zatvara cisto, socket fajl uklonjen."""
    sock_path = serve_proc["sock_path"]
    assert sock_path.exists()

    serve_proc["proc"].terminate()
    rc = serve_proc["proc"].wait(timeout=5)
    # 0 ili -SIGTERM su prihvatljivi (zavisi od loop signal handler ponasanja)
    assert rc in (0, -15, 143)
    # Socket fajl mora biti uklonjen
    assert not sock_path.exists()

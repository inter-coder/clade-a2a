"""PR#6 testovi za clade status/logs/send CLI subkomande."""

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

from clade.audit import Audit
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
def serve_proc(tmp_path: Path):
    """Spawn clade serve sa minimal peers.yaml."""
    port = _free_port()
    cfg_path = tmp_path / "peers.yaml"
    audit_db = tmp_path / "audit.db"
    cfg_path.write_text(yaml.dump({
        "version": 2,
        "self": "katana",
        "peers": {
            "katana": {
                "transport": "unix", "role": "interactive",
                "socket": str(tmp_path / "katana.sock"),
                "http_port": port,
                "audit_db": str(audit_db),
            },
            "dusan": {
                "transport": "unix", "role": "headless",
                "socket": str(tmp_path / "dusan.sock"),
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

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        proc.terminate()
        out, err = proc.communicate(timeout=3)
        raise RuntimeError(f"clade serve nije starovao za 8s. stderr={err!r}")

    yield {"port": port, "cfg_path": cfg_path, "audit_db": audit_db, "proc": proc, "tmp": tmp_path}

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _run_clade(args: list[str], env_extra: dict | None = None,
               timeout: float = 10) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, "-m", "clade"] + args,
                          capture_output=True, text=True, env=env, timeout=timeout)


# ---- clade status ----


def test_cli_status_returns_health_table(serve_proc):
    res = _run_clade(["status", "--config", str(serve_proc["cfg_path"])])
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert "peer:" in res.stdout
    assert "katana" in res.stdout
    assert "protocol:" in res.stdout
    assert "2.0.0" in res.stdout
    assert "outbox:" in res.stdout


def test_cli_status_unreachable_peer_fails_cleanly(tmp_path: Path):
    """clade serve nije pokrenut — status vraca smisleni error, ne crash."""
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "version": 2, "self": "ghost",
        "peers": {"ghost": {"transport": "unix", "role": "interactive",
                            "socket": "/tmp/x.sock", "http_port": 1,
                            "audit_db": "/tmp/x.db"}},
    }))
    res = _run_clade(["status", "--config", str(cfg_path)])
    assert res.returncode == 2  # unreachable
    assert "unreachable" in res.stderr


# ---- clade logs ----


def test_cli_logs_empty_audit_works(serve_proc):
    """Kad nista nije primljeno, logs ispisuje '(nema audit zapisa)'."""
    res = _run_clade(["logs", "--config", str(serve_proc["cfg_path"]), "--tail", "10"])
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert "nema audit zapisa" in res.stdout


@pytest.mark.asyncio
async def test_cli_logs_shows_received_message(serve_proc):
    """Poslemo poruku, logs treba da je prikazu."""
    # Posalji envelope direktno na katana socket
    katana_sock = serve_proc["tmp"] / "katana.sock"
    client = UnixSocketTransport()
    env = Envelope.new(
        from_agent="dusan", to_agent="katana", kind="send",
        payload={"text": "hello from logs test"},
        thread_id="t-log-test",
    )
    await client.deliver(env, to_url=f"unix://{katana_sock}")
    await asyncio.sleep(0.2)  # audit write nije instant

    res = _run_clade(["logs", "--config", str(serve_proc["cfg_path"]), "--tail", "5"])
    assert res.returncode == 0
    assert "dusan" in res.stdout
    assert "hello from logs test" in res.stdout
    assert "t-log-te" in res.stdout  # truncated thread_id prefix


# ---- clade send ----


@pytest.mark.asyncio
async def test_cli_send_to_live_peer(serve_proc):
    """clade send --to dusan 'hi' kroz local clade serve → unix socket → dusan handler."""
    dusan_sock = serve_proc["tmp"] / "dusan.sock"
    server = UnixSocketTransport(socket_path=dusan_sock)
    received: list[Envelope] = []

    async def handler(env: Envelope) -> Response:
        received.append(env)
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if dusan_sock.exists():
            break
        await asyncio.sleep(0.02)

    try:
        # Subprocess call, izvan event loop-a — koristim sync run
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: _run_clade([
            "send", "--config", str(serve_proc["cfg_path"]),
            "--to", "dusan", "hi from cli",
        ]))
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert "delivered" in res.stdout

        # Daj handler trenutak
        await asyncio.sleep(0.2)
        assert len(received) == 1
        assert received[0].from_agent == "katana"
        assert received[0].payload == {"text": "hi from cli"}
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


def test_cli_send_to_unknown_peer_fails(serve_proc):
    res = _run_clade([
        "send", "--config", str(serve_proc["cfg_path"]),
        "--to", "predrag", "test",  # predrag nije u peers.yaml
    ])
    assert res.returncode == 3  # error u tool response-u
    assert "unknown peer" in res.stderr


# ---- handshake protiv live servera ----


@pytest.mark.asyncio
async def test_handshake_rejects_v1_envelope(serve_proc):
    """Slanje envelope-a sa protocol_version='1.0.0' → reply payload sa
    _error='protocol_mismatch'."""
    katana_sock = serve_proc["tmp"] / "katana.sock"
    client = UnixSocketTransport()
    env = Envelope(
        msg_id="m1", from_agent="dusan", to_agent="katana", kind="ask",
        payload={"q": "?"}, nonce="n1", timestamp_ms=1700000000000,
        correlation_id="c1", protocol_version="1.0.0",
    )
    result = await client.deliver(env, to_url=f"unix://{katana_sock}")
    assert result.ok is True  # transport-level OK
    assert result.response is not None
    payload = result.response.payload
    assert payload.get("_error") == "protocol_mismatch"
    assert payload.get("expected") == "2.x.y"
    assert payload.get("received") == "1.0.0"

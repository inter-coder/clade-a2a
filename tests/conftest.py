"""Pytest fixtures za Clade smoke tests.

Spawn-uje sopstvenu relay instancu na free port-u + sopstvene config fajlove
da ne kontaminira dev state. Svaki test file dobija fresh state.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def shared_secret() -> str:
    """Per-pair HMAC secret (alice <-> bob)."""
    return secrets.token_hex(32)


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    """Bearer token → agent_id mapping."""
    return {
        secrets.token_urlsafe(32): "alice",
        secrets.token_urlsafe(32): "bob",
    }


@pytest.fixture(scope="session")
def token_for_alice(tokens: dict[str, str]) -> str:
    return [t for t, a in tokens.items() if a == "alice"][0]


@pytest.fixture(scope="session")
def token_for_bob(tokens: dict[str, str]) -> str:
    return [t for t, a in tokens.items() if a == "bob"][0]


@pytest.fixture(scope="session")
def workdir() -> Iterator[Path]:
    """Privremeni dir sa configs + tokens.json + audit DBs."""
    with tempfile.TemporaryDirectory(prefix="clade-test-") as d:
        yield Path(d)


@pytest.fixture(scope="session")
def relay_port() -> int:
    return _free_port()


@pytest.fixture(scope="session")
def relay_url(relay_port: int) -> str:
    return f"http://127.0.0.1:{relay_port}"


@pytest.fixture(scope="session")
def relay_process(
    workdir: Path,
    tokens: dict[str, str],
    relay_port: int,
    relay_url: str,
) -> Iterator[subprocess.Popen]:
    """Spawn relay subprocess sa sopstvenim tokens.json u workdir-u."""
    # Pripremi tokens.json u workdir-u
    tokens_path = workdir / "tokens.json"
    tokens_path.write_text(json.dumps(tokens, indent=2))

    # Symlink workdir/relay → relay/relay tako da relay vidi tokens.json
    # NAJEDNOSTAVNIJE: override TOKENS_PATH preko env vara
    env = os.environ.copy()
    env["CLADE_TEST_TOKENS_PATH"] = str(tokens_path)

    proc = subprocess.Popen(
        [
            PY,
            "-c",
            f"""
import os, sys
sys.path.insert(0, '{ROOT}')
# Override TOKENS_PATH pre nego sto se main module importuje
import relay.main
import pathlib
relay.main.TOKENS_PATH = pathlib.Path(os.environ['CLADE_TEST_TOKENS_PATH'])
import uvicorn
uvicorn.run(relay.main.app, host='127.0.0.1', port={relay_port}, log_level='warning')
""",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    # Saceka da bude ready
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{relay_url}/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        out, err = proc.communicate(timeout=2)
        raise RuntimeError(f"Relay didn't start: stdout={out!r}, stderr={err!r}")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# Note: alice_config/bob_config fixture-i (v1 agent) uklonjeni u PR#5 cleanup-u.
# v2 testovi koriste sopstvene tmp_path / fastmcp.Client setup-e (vidi
# tests/test_v2_*.py); relay testovi koriste samo gornje token + relay_url fixture.

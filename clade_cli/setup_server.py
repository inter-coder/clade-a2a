"""Clade A2A — Web Setup Server (v1.4.0+).

Replaces the bash wizard with a web form. Run on the host machine that will
also host the relay; other peers `curl` their install script from this server.

Flow:
  1. `clade-setup-server` (or `python -m clade_cli.setup_server`)
  2. Browser opens http://<lan-ip>:8000/
  3. Admin fills form: project name, relay host, peers (id/name/role)
  4. Backend generates tokens.json + per-peer yaml-ovi + per-peer install URLs
  5. Optionally starts relay subprocess (all-in-one mode)
  6. Each peer receives its unique install URL out-of-band, runs:
        curl -fsSL http://<lan-ip>:8000/agent/<token>/install | bash
     Install script: installs clade-a2a if missing, downloads config + start.sh,
     prints next-step instructions.

State: in-memory dict per project. Process state is volatile — restart of
setup server invalidates all tokens. For long-running deploys, copy generated
files out of `--data-dir` and run agents manually.

All UI strings English. Logs/docstrings stay as-is per project convention.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator

LOG = logging.getLogger("clade.setup")
TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---- Models ----

class PeerInput(BaseModel):
    """One peer as submitted from the form."""
    peer_id: str = Field(..., description="Technical slug for routing (e.g. 'frontend')")
    display_name: str = Field("", description="Display name shown to other agents")
    role: str = Field("", description="Multi-line role prompt")

    @field_validator("peer_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip()
        if not v or not v[0].isalpha():
            raise ValueError(f"Invalid peer_id '{v}': must start with a letter, alnum/dash/underscore only")
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(f"Invalid peer_id '{v}': only alphanumeric, dash, underscore allowed")
        return v


class SetupForm(BaseModel):
    """Top-level form submission."""
    project_name: str = Field(..., min_length=1, max_length=64)
    relay_host: str = Field("0.0.0.0", description="Bind host for relay")
    relay_url_host: str = Field(..., description="Public hostname/IP for peers to reach relay")
    relay_port: int = Field(7777, ge=1024, le=65535)
    start_relay: bool = Field(True, description="All-in-one: spawn relay subprocess")
    peers: list[PeerInput]

    @field_validator("peers")
    @classmethod
    def _at_least_two_peers(cls, v: list[PeerInput]) -> list[PeerInput]:
        if len(v) < 2:
            raise ValueError("At least 2 peers required")
        ids = [p.peer_id for p in v]
        if len(set(ids)) != len(ids):
            raise ValueError("Peer IDs must be unique")
        return v


# ---- State ----

@dataclass
class PeerArtifact:
    """Generated artifacts for one peer (after form submit)."""
    peer_id: str
    display_name: str
    role: str
    download_token: str           # secret URL component
    bearer_token: str             # for auth to relay
    yaml_path: Path               # path on disk to the yaml file
    yaml_content: str             # cached for serving


@dataclass
class Setup:
    """One completed setup (project + all peers + optional relay subprocess)."""
    project_name: str
    project_token: str            # for result page URL
    relay_url: str                # http://host:port for peers
    relay_host_bind: str          # 0.0.0.0 or specific bind
    relay_port: int
    tokens_path: Path             # tokens.json path on disk
    peers: dict[str, PeerArtifact] = field(default_factory=dict)
    relay_subprocess: subprocess.Popen | None = None
    download_token_to_peer: dict[str, str] = field(default_factory=dict)
    # map per-peer download_token → peer_id for quick lookup


@dataclass
class AppState:
    """Server-wide state. One process can have many setups (but typically one)."""
    data_dir: Path
    public_url_base: str          # what peers see (http://<lan-ip>:8000)
    setups: dict[str, Setup] = field(default_factory=dict)
    # project_token → Setup


# ---- Helpers ----

def _detect_lan_ip() -> str:
    """Try to detect this host's LAN IP. Falls back to 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _build_yaml(peer: PeerInput, all_peers: list[PeerInput],
                bearer: str, secret_for_pair: dict[frozenset, str],
                relay_url: str, audit_dir: str = "~/.clade") -> dict:
    """Build the per-peer yaml dict (v1.3.0+ schema with PeerInfo)."""
    peers_dict: dict[str, Any] = {}
    for other in all_peers:
        if other.peer_id == peer.peer_id:
            continue
        entry: dict[str, Any] = {
            "secret": secret_for_pair[frozenset({peer.peer_id, other.peer_id})],
        }
        if other.display_name and other.display_name != other.peer_id:
            entry["name"] = other.display_name
        if other.role and other.role.strip():
            entry["role"] = other.role.strip().split("\n")[0][:200]
        peers_dict[other.peer_id] = entry

    data: dict[str, Any] = {
        "my_id": peer.peer_id,
    }
    if peer.display_name and peer.display_name != peer.peer_id:
        data["name"] = peer.display_name
    if peer.role and peer.role.strip():
        data["role"] = peer.role.strip()
    data.update({
        "relay_url": relay_url,
        "bearer_token": bearer,
        "peers": peers_dict,
        "audit_db": f"{audit_dir}/{peer.peer_id}-audit.db",
    })
    return data


def _generate_setup(form: SetupForm, data_dir: Path) -> Setup:
    """Generate tokens.json + per-peer yaml-ovi + bearer/secret keys."""
    project_token = secrets.token_urlsafe(16)
    project_dir = data_dir / project_token
    project_dir.mkdir(parents=True, exist_ok=True)

    # Per-pair HMAC secrets
    pair_secrets: dict[frozenset, str] = {}
    for a, b in combinations(form.peers, 2):
        pair_secrets[frozenset({a.peer_id, b.peer_id})] = secrets.token_hex(32)

    # Per-peer bearer token (for auth to relay)
    bearers = {p.peer_id: secrets.token_urlsafe(32) for p in form.peers}

    # Per-peer download token (for unique install URL)
    download_tokens = {p.peer_id: secrets.token_urlsafe(24) for p in form.peers}

    # tokens.json: bearer → agent_id mapping (for relay)
    tokens_map = {bearers[p.peer_id]: p.peer_id for p in form.peers}
    tokens_path = project_dir / "tokens.json"
    tokens_path.write_text(json.dumps(tokens_map, indent=2) + "\n")
    tokens_path.chmod(0o600)

    relay_url = f"http://{form.relay_url_host}:{form.relay_port}"

    setup = Setup(
        project_name=form.project_name,
        project_token=project_token,
        relay_url=relay_url,
        relay_host_bind=form.relay_host,
        relay_port=form.relay_port,
        tokens_path=tokens_path,
    )

    # Per-peer yaml
    for peer in form.peers:
        data = _build_yaml(peer, form.peers, bearers[peer.peer_id], pair_secrets, relay_url)
        yaml_content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        yaml_path = project_dir / f"{peer.peer_id}.yaml"
        yaml_path.write_text(yaml_content)
        yaml_path.chmod(0o600)

        artifact = PeerArtifact(
            peer_id=peer.peer_id,
            display_name=peer.display_name or peer.peer_id,
            role=peer.role,
            download_token=download_tokens[peer.peer_id],
            bearer_token=bearers[peer.peer_id],
            yaml_path=yaml_path,
            yaml_content=yaml_content,
        )
        setup.peers[peer.peer_id] = artifact
        setup.download_token_to_peer[download_tokens[peer.peer_id]] = peer.peer_id

    return setup


def _start_relay_subprocess(setup: Setup) -> subprocess.Popen:
    """Spawn relay subprocess that reads tokens.json from setup."""
    # Find venv python (relay needs the same install)
    venv_python = Path(sys.executable)
    repo_root = Path(__file__).resolve().parent.parent

    cmd = [
        str(venv_python), "-c",
        f"""
import sys, pathlib, uvicorn, os
sys.path.insert(0, '{repo_root}')
import relay.main
relay.main.TOKENS_PATH = pathlib.Path({str(setup.tokens_path)!r})
uvicorn.run(relay.main.app, host='{setup.relay_host_bind}', port={setup.relay_port}, log_level='info')
""",
    ]
    log_path = setup.tokens_path.parent / "relay.log"
    log_fh = open(log_path, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    LOG.info("relay subprocess started PID=%d (log: %s)", proc.pid, log_path)
    return proc


# ---- App factory ----

def create_app(state: AppState) -> FastAPI:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )

    app = FastAPI(title="Clade A2A Setup Server")

    # --- Form page ---

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        tmpl = env.get_template("index.html")
        return tmpl.render(
            detected_lan_ip=_detect_lan_ip(),
            existing_setups=list(state.setups.values()),
        )

    # --- API: submit form ---

    @app.post("/api/setup")
    async def submit(req: Request) -> RedirectResponse:
        body = await req.json()
        form = SetupForm(**body)
        setup = _generate_setup(form, state.data_dir)
        state.setups[setup.project_token] = setup
        if form.start_relay:
            setup.relay_subprocess = _start_relay_subprocess(setup)
        return RedirectResponse(f"/setup/{setup.project_token}", status_code=303)

    # --- Result page (shows per-peer URLs) ---

    @app.get("/setup/{project_token}", response_class=HTMLResponse)
    async def setup_result(project_token: str) -> str:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        tmpl = env.get_template("result.html")
        return tmpl.render(
            setup=setup,
            public_url_base=state.public_url_base,
            relay_running=setup.relay_subprocess is not None
                          and setup.relay_subprocess.poll() is None,
        )

    # --- Per-peer artifacts ---

    @app.get("/agent/{token}/install", response_class=PlainTextResponse)
    async def install_script(token: str) -> str:
        peer, setup = _find_peer_by_token(state, token)
        tmpl = env.get_template("install.sh.j2")
        return tmpl.render(
            peer=peer, setup=setup, public_url_base=state.public_url_base,
        )

    @app.get("/agent/{token}/config", response_class=PlainTextResponse)
    async def peer_yaml(token: str) -> str:
        peer, _setup = _find_peer_by_token(state, token)
        return peer.yaml_content

    @app.get("/agent/{token}/start", response_class=PlainTextResponse)
    async def start_script(token: str) -> str:
        peer, setup = _find_peer_by_token(state, token)
        tmpl = env.get_template("start.sh.j2")
        return tmpl.render(peer=peer, setup=setup)

    @app.get("/agent/{token}/chat", response_class=PlainTextResponse)
    async def chat_script(token: str) -> str:
        peer, setup = _find_peer_by_token(state, token)
        # Pick a sample "other" peer for the prompt hint
        others = [p for p in setup.peers.values() if p.peer_id != peer.peer_id]
        other_sample_name = others[0].display_name if others else "<peer>"
        tmpl = env.get_template("chat.sh.j2")
        return tmpl.render(
            peer=peer, setup=setup,
            public_url_base=state.public_url_base,
            other_sample_name=other_sample_name,
        )

    @app.get("/agent/{token}/mcp-config", response_class=PlainTextResponse)
    async def mcp_config(token: str) -> str:
        peer, _setup = _find_peer_by_token(state, token)
        tmpl = env.get_template("mcp-config.json.j2")
        return tmpl.render(
            peer=peer,
            clade_python="/opt/clade-a2a/.venv/bin/python",
            clade_repo_dir="/opt/clade-a2a",
            agent_dir="$HOME/clade-agent",
        )

    # --- Health ---

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "service": "clade-setup-server",
            "setups": len(state.setups),
            "data_dir": str(state.data_dir),
        }

    return app


def _find_peer_by_token(state: AppState, token: str) -> tuple[PeerArtifact, Setup]:
    for setup in state.setups.values():
        peer_id = setup.download_token_to_peer.get(token)
        if peer_id is None:
            continue
        return setup.peers[peer_id], setup
    raise HTTPException(404, "Token not found or expired")


# ---- Entry point ----

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clade-setup-server",
        description="Web-based setup server for Clade A2A. Run on the host that will also host the relay.",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host for the setup web server (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Bind port for the setup web server (default: 8000)")
    parser.add_argument("--data-dir", default="~/.clade/setup-server",
                        help="Where to store generated configs (default: ~/.clade/setup-server)")
    parser.add_argument("--public-url",
                        help="Public URL peers will see (default: auto-detect LAN IP + port)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    public_url_base = args.public_url
    if not public_url_base:
        lan_ip = _detect_lan_ip()
        public_url_base = f"http://{lan_ip}:{args.port}"

    state = AppState(data_dir=data_dir, public_url_base=public_url_base)
    app = create_app(state)

    print(f"\nClade A2A Setup Server")
    print(f"=======================")
    print(f"  Open in browser:  {public_url_base}/")
    print(f"  Bind:             {args.host}:{args.port}")
    print(f"  Data dir:         {data_dir}")
    print(f"")
    print(f"  Stop:             Ctrl+C")
    print(f"")

    # Graceful shutdown of relay subprocesses on signal
    def shutdown_relays():
        for setup in state.setups.values():
            if setup.relay_subprocess and setup.relay_subprocess.poll() is None:
                LOG.info("stopping relay subprocess PID=%d", setup.relay_subprocess.pid)
                setup.relay_subprocess.terminate()

    def handle_signal(sig, _frame):
        LOG.info("signal %s received, shutting down", signal.Signals(sig).name)
        shutdown_relays()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        import uvicorn  # noqa: PLC0415
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_relays()
    return 0


if __name__ == "__main__":
    sys.exit(main())

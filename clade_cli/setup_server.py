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
    extra_add_dirs: list[str] = Field(default_factory=list,
                                        description="Paths the daemon-spawned Claude can read outside its workdir")

    @field_validator("peer_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip()
        if not v or not v[0].isalpha():
            raise ValueError(f"Invalid peer_id '{v}': must start with a letter, alnum/dash/underscore only")
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(f"Invalid peer_id '{v}': only alphanumeric, dash, underscore allowed")
        return v


# v1.12.0: per-peer mgmt request bodies
class AddPeerRequest(BaseModel):
    peer_id: str
    display_name: str = ""
    role: str = ""
    teams: list[str] = Field(default_factory=list)
    extra_add_dirs: list[str] = Field(default_factory=list)


class UpdatePeerRequest(BaseModel):
    role: str | None = None
    display_name: str | None = None
    extra_add_dirs: list[str] | None = None


class UpdateTeamsRequest(BaseModel):
    teams: dict[str, list[str]]


# v1.13.0 (Phase A — AITF import): body for POST /api/setup/import-aitf
class AitfImportRequest(BaseModel):
    project_path: str = Field(..., description="Absolute path to an AITF-scaffolded project root")
    relay_host: str = Field("0.0.0.0", description="Bind host for relay")
    relay_url_host: str = Field("", description="Public hostname/IP for peers. If empty, auto-detected LAN IP.")
    relay_port: int = Field(7777, ge=1024, le=65535)
    start_relay: bool = Field(True)
    project_name_override: str = Field("", description="If empty, uses name from .ai-team-config.yml")


class SetupForm(BaseModel):
    """Top-level form submission."""
    project_name: str = Field(..., min_length=1, max_length=64)
    relay_host: str = Field("0.0.0.0", description="Bind host for relay")
    relay_url_host: str = Field(..., description="Public hostname/IP for peers to reach relay")
    relay_port: int = Field(7777, ge=1024, le=65535)
    start_relay: bool = Field(True, description="All-in-one: spawn relay subprocess")
    peers: list[PeerInput]
    # v1.9.0: teams — team_name → lista peer_id-eva. Optional (postoji 0+ teams).
    teams: dict[str, list[str]] = Field(default_factory=dict)

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
                relay_url: str, audit_dir: str = "~/.clade",
                teams: dict[str, list[str]] | None = None) -> dict:
    """Build the per-peer yaml dict (v1.3.0+ schema with PeerInfo; v1.9.0 teams)."""
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
        # v1.10.2: paths the daemon-spawned Claude can read outside its workdir.
        # Editable from the web result page (PATCH /api/setup/{token}/peers/{id})
        # or directly in this yaml; the daemon reads it at startup.
        "extra_add_dirs": list(peer.extra_add_dirs or []),
    })
    # v1.9.0: teams su shared kod sve peer-ove — CEO i zaposleni svi imaju isti
    # list "engineering = [alice, bob]" pa svako moze da broadcast-uje grupi.
    # Filtriramo praznihm/invalid (zaposleni koji ne postoje).
    if teams:
        all_ids = {p.peer_id for p in all_peers}
        valid_teams: dict[str, list[str]] = {}
        for tname, members in teams.items():
            valid = [m for m in members if m in all_ids]
            if valid:
                valid_teams[tname] = valid
        if valid_teams:
            data["teams"] = valid_teams
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
        data = _build_yaml(peer, form.peers, bearers[peer.peer_id], pair_secrets, relay_url,
                            teams=form.teams)
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
    """Spawn relay subprocess that reads tokens.json from setup.

    start_new_session=True izoluje subprocess od process grupe setup-server-a.
    Razlog: relay zivi i preko restart-a setup-server-a — peer install-i ostaju
    validni (vidi `main()` i `_load_setups`)."""
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
    # v1.4.5: pisi PID file da bi start-setup-server.sh reset blok mogao pouzdano
    # da ubije relay-e (pkill -f "import relay.main" promasuje jer je pattern u
    # stringu Python -c argumenta — pgrep ne hvata sve verzije pouzdano).
    pid_file = setup.tokens_path.parent / "relay.pid"
    try:
        pid_file.write_text(str(proc.pid))
    except OSError as e:
        LOG.warning("nije moglo da se napise %s: %s", pid_file, e)
    LOG.info("relay subprocess started PID=%d (log: %s, pid file: %s)",
             proc.pid, log_path, pid_file)
    return proc


def _is_relay_already_up(relay_url: str, timeout_s: float = 1.5) -> bool:
    """Health check na relay_url — True ako vec neko opsluzuje. Koristi se pri
    load-u setup-ova da ne bi dva relay-a probala da bind-uju isti port."""
    import httpx as _httpx  # noqa: PLC0415
    try:
        with _httpx.Client(timeout=timeout_s) as c:
            r = c.get(f"{relay_url.rstrip('/')}/health")
            return r.status_code == 200
    except Exception:
        return False


def _save_setup(setup: Setup, data_dir: Path) -> None:
    """Persist setup state u data_dir/<project_token>/setup.json (v1.4.2+).

    Razlog: bez ovoga restart setup-server-a unisti in-memory tokens mapping,
    a relay subprocess (start_new_session=True) i dalje tece sa starim
    tokens.json — peer install-i izgledaju validni ali daemon dobija 401
    jer setup-server vise ne zna o tom download_token-u i ne moze poslati
    isti yaml. Subprocess handle se NE serijalizuje (PID je transient)."""
    setup_dir = data_dir / setup.project_token
    setup_dir.mkdir(parents=True, exist_ok=True)
    state_file = setup_dir / "setup.json"

    data = {
        "version": 1,
        "project_name": setup.project_name,
        "project_token": setup.project_token,
        "relay_url": setup.relay_url,
        "relay_host_bind": setup.relay_host_bind,
        "relay_port": setup.relay_port,
        "tokens_path": str(setup.tokens_path),
        "relay_was_started": setup.relay_subprocess is not None,
        "peers": [
            {
                "peer_id": p.peer_id,
                "display_name": p.display_name,
                "role": p.role,
                "download_token": p.download_token,
                "bearer_token": p.bearer_token,
                "yaml_path": str(p.yaml_path),
                "yaml_content": p.yaml_content,
            }
            for p in setup.peers.values()
        ],
    }
    tmp_file = state_file.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(data, indent=2))
    tmp_file.chmod(0o600)
    tmp_file.replace(state_file)


def _load_setups(data_dir: Path) -> dict[str, Setup]:
    """Rekonstruisi setup state sa diska na startup. Za svaki sa relay-em:
    ako relay vec tece, ostavi; ako ne, respawn-uj."""
    out: dict[str, Setup] = {}
    if not data_dir.exists():
        return out

    for project_dir in sorted(data_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        state_file = project_dir / "setup.json"
        if not state_file.exists():
            continue

        try:
            data = json.loads(state_file.read_text())
        except Exception as e:
            LOG.warning("failed to load %s: %s — skipping", state_file, e)
            continue

        if data.get("version") != 1:
            LOG.warning("setup.json at %s has unknown version, skipping", state_file)
            continue

        setup = Setup(
            project_name=data["project_name"],
            project_token=data["project_token"],
            relay_url=data["relay_url"],
            relay_host_bind=data["relay_host_bind"],
            relay_port=data["relay_port"],
            tokens_path=Path(data["tokens_path"]),
        )
        for p in data.get("peers") or []:
            artifact = PeerArtifact(
                peer_id=p["peer_id"],
                display_name=p["display_name"],
                role=p["role"],
                download_token=p["download_token"],
                bearer_token=p["bearer_token"],
                yaml_path=Path(p["yaml_path"]),
                yaml_content=p["yaml_content"],
            )
            setup.peers[p["peer_id"]] = artifact
            setup.download_token_to_peer[p["download_token"]] = p["peer_id"]

        if data.get("relay_was_started"):
            if _is_relay_already_up(setup.relay_url):
                LOG.info("setup '%s': external relay still up at %s, not respawning",
                         setup.project_name, setup.relay_url)
            else:
                try:
                    setup.relay_subprocess = _start_relay_subprocess(setup)
                    LOG.info("setup '%s': respawned relay on port %d",
                             setup.project_name, setup.relay_port)
                except Exception as e:
                    LOG.error("setup '%s': failed to respawn relay: %s",
                              setup.project_name, e)

        out[setup.project_token] = setup
        LOG.info("loaded setup '%s' (%d peers) from %s",
                 setup.project_name, len(setup.peers), state_file)

    return out


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
        # v1.4.2: persistuj da restart setup-server-a ne invalidira peer install-e
        _save_setup(setup, state.data_dir)
        return RedirectResponse(f"/setup/{setup.project_token}", status_code=303)

    # v1.13.0 (Phase A): import an existing AI Team Framework project.
    # Maps PD/DD/Team/(DO) roles from .ai-team-config.yml + docs/TEAM/*.md to
    # Clade A2A peers, gives every peer extra_add_dirs=[<project_path>] so the
    # document substrate stays the source of truth.
    @app.post("/api/setup/import-aitf")
    async def import_aitf(body: AitfImportRequest) -> dict:
        from clade_cli import aitf_import as _aitf  # noqa: PLC0415

        try:
            parsed = _aitf.parse_aitf_project(Path(body.project_path))
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(400, f"AITF import failed: {e}") from e

        relay_url_host = body.relay_url_host.strip() or _detect_lan_ip()
        project_name = body.project_name_override.strip() or parsed.project_name

        peer_inputs = [
            PeerInput(
                peer_id=p.peer_id,
                display_name=p.display_name,
                role=p.role,
                extra_add_dirs=p.extra_add_dirs,
            )
            for p in parsed.peers
        ]

        form = SetupForm(
            project_name=project_name[:64],
            relay_host=body.relay_host,
            relay_url_host=relay_url_host,
            relay_port=body.relay_port,
            start_relay=body.start_relay,
            peers=peer_inputs,
            teams=parsed.teams,
        )

        setup = _generate_setup(form, state.data_dir)
        state.setups[setup.project_token] = setup
        if form.start_relay:
            setup.relay_subprocess = _start_relay_subprocess(setup)
        _save_setup(setup, state.data_dir)

        return {
            "ok": True,
            "project_token": setup.project_token,
            "redirect_url": f"/setup/{setup.project_token}",
            "imported_from": str(parsed.project_path),
            "doc_optimizer_enabled": parsed.doc_optimizer_enabled,
            "peers": [p.peer_id for p in parsed.peers],
            "teams": parsed.teams,
        }

    # --- Result page (shows per-peer URLs) ---

    @app.get("/setup/{project_token}", response_class=HTMLResponse)
    async def setup_result(project_token: str) -> str:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        # v1.12.0: parse extra_add_dirs out of each peer's yaml so the inline
        # Edit form can pre-fill it. Also extract teams (same across all yamls).
        peers_data: dict[str, dict] = {}
        teams_data: dict[str, list[str]] = {}
        pd = state.data_dir / setup.project_token
        for pid in setup.peers.keys():
            yaml_path = pd / f"{pid}.yaml"
            if yaml_path.exists():
                try:
                    d = yaml.safe_load(yaml_path.read_text()) or {}
                    peers_data[pid] = {
                        "extra_add_dirs": d.get("extra_add_dirs", []),
                    }
                    if not teams_data and d.get("teams"):
                        teams_data = d["teams"]
                except Exception:
                    peers_data[pid] = {"extra_add_dirs": []}
        tmpl = env.get_template("result.html")
        return tmpl.render(
            setup=setup,
            public_url_base=state.public_url_base,
            relay_running=setup.relay_subprocess is not None
                          and setup.relay_subprocess.poll() is None,
            peers_data=peers_data,
            teams_data=teams_data,
        )

    # v1.7.0 (C4): realtime status JSON za setup result page polling
    # v1.8.0: pridodaj live presence (online/offline po peer-u + secs_ago)
    @app.get("/setup/{project_token}/status")
    async def setup_status(project_token: str) -> dict:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        relay_alive = (setup.relay_subprocess is not None
                       and setup.relay_subprocess.poll() is None)
        status: dict = {
            "relay_url": setup.relay_url,
            "relay_alive": relay_alive,
            "relay_pid": setup.relay_subprocess.pid if relay_alive else None,
            "peers": list(setup.peers.keys()),
        }
        # Pita relay /health za live podatke + /presence za online status.
        # Koristimo prvog peer-ovog bearer da prodjemo auth na /presence
        # (relay je interni i svaki authenticated agent moze citati).
        try:
            import httpx as _httpx  # noqa: PLC0415
            first_bearer = next(iter(setup.peers.values())).bearer_token if setup.peers else None
            headers = {"Authorization": f"Bearer {first_bearer}"} if first_bearer else {}
            with _httpx.Client(timeout=2) as c:
                r = c.get(f"{setup.relay_url}/health")
                if r.status_code == 200:
                    h = r.json()
                    status["relay_known_agents"] = h.get("known_agents", [])
                    status["online_agents"] = h.get("online_agents", [])
                    status["pending_by_peer"] = h.get("pending_by_peer", {})
                    status["audit_count"] = h.get("audit_count", 0)
                    status["presence_ttl_s"] = h.get("presence_ttl_s")
                if first_bearer:
                    rp = c.get(f"{setup.relay_url}/presence", headers=headers)
                    if rp.status_code == 200:
                        status["presence"] = rp.json().get("peers", {})
        except Exception as e:
            status["relay_error"] = str(e)[:120]
        return status

    # --- Per-peer artifacts ---

    @app.get("/agent/{token}/install", response_class=PlainTextResponse)
    async def install_script(token: str) -> str:
        peer, setup = _find_peer_by_token(state, token)
        tmpl = env.get_template("install.sh.j2")
        return tmpl.render(
            peer=peer, setup=setup, public_url_base=state.public_url_base,
        )

    # v1.12.1: one-shot multi-peer installer (parity with --quickstart's local install loop)
    @app.get("/setup/{project_token}/install-all", response_class=PlainTextResponse)
    async def install_all_script(project_token: str) -> str:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        lines = [
            "#!/bin/bash",
            "# Install all peers of this setup on the current machine.",
            f"# Project: {setup.project_name}",
            f"# Setup-server: {state.public_url_base}",
            "set -e",
            "",
            "GREEN='\\033[0;32m'; CYAN='\\033[0;36m'; NC='\\033[0m'",
            "info()  { echo -e \"${CYAN}==>${NC} $*\"; }",
            "ok()    { echo -e \"${GREEN}\\u2713${NC} $*\"; }",
            "",
        ]
        for peer in setup.peers.values():
            lines.append(f"info \"installing peer '{peer.peer_id}' ({peer.display_name})...\"")
            lines.append(f'curl -fsSL "{state.public_url_base}/agent/{peer.download_token}/install" | bash')
            lines.append(f"ok \"{peer.peer_id} done\"")
            lines.append("")
        lines.append("echo ''")
        lines.append('echo "All peers installed. Start daemons (one terminal each):"')
        for peer in setup.peers.values():
            lines.append(f'echo "  $HOME/clade-agent/start-{peer.peer_id}.sh --yolo"')
        lines.append('echo ""')
        lines.append('echo "And open one CEO/coordinator chat (pick a peer to be your interactive seat):"')
        # Heuristic: if there's a 'ceo' peer, suggest it; else suggest the first peer
        ceo_id = "ceo" if "ceo" in setup.peers else next(iter(setup.peers))
        lines.append(f'echo "  $HOME/clade-agent/chat-{ceo_id}.sh"')
        return "\n".join(lines) + "\n"

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

    # --- v1.12.0: peer management endpoints ---

    def _agent_dir() -> Path:
        # Single-machine assumption: ~/clade-agent (override via env if needed)
        return Path(os.environ.get("CLADE_AGENT_DIR", str(Path.home() / "clade-agent")))

    def _project_dir_for(setup: Setup) -> Path:
        return state.data_dir / setup.project_token

    def _refresh_setup_in_memory(setup: Setup) -> None:
        """Re-read setup.json + yamls for this setup so in-memory state matches disk."""
        pd = _project_dir_for(setup)
        data = json.loads((pd / "setup.json").read_text())
        new_peers: dict[str, PeerArtifact] = {}
        new_token_map: dict[str, str] = {}
        for p in data["peers"]:
            art = PeerArtifact(
                peer_id=p["peer_id"], display_name=p["display_name"], role=p["role"],
                download_token=p["download_token"], bearer_token=p["bearer_token"],
                yaml_path=Path(p["yaml_path"]), yaml_content=p["yaml_content"],
            )
            new_peers[p["peer_id"]] = art
            new_token_map[p["download_token"]] = p["peer_id"]
        setup.peers = new_peers
        setup.download_token_to_peer = new_token_map

    @app.post("/api/setup/{project_token}/peers")
    async def add_peer(project_token: str, req: AddPeerRequest) -> dict:
        """v1.12.0: add a new peer to a running setup. Calls peer_ops + refreshes state."""
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        try:
            from clade_cli.peer_ops import add_peer_op  # noqa: PLC0415
            result = add_peer_op(
                project_dir=_project_dir_for(setup),
                agent_dir=_agent_dir(),
                peer_id=req.peer_id,
                display_name=req.display_name or req.peer_id,
                role=req.role,
                teams=req.teams,
                extra_add_dirs=req.extra_add_dirs,
                relay_url_for_reload=setup.relay_url,
                setup_server_url_for_reload=None,  # we refresh in-process below
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        _refresh_setup_in_memory(setup)
        return result

    @app.patch("/api/setup/{project_token}/peers/{peer_id}")
    async def update_peer(project_token: str, peer_id: str, req: UpdatePeerRequest) -> dict:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        try:
            from clade_cli.peer_ops import update_peer_op  # noqa: PLC0415
            result = update_peer_op(
                project_dir=_project_dir_for(setup),
                agent_dir=_agent_dir(),
                peer_id=peer_id,
                role=req.role,
                display_name=req.display_name,
                extra_add_dirs=req.extra_add_dirs,
                setup_server_url_for_reload=None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        _refresh_setup_in_memory(setup)
        return result

    @app.delete("/api/setup/{project_token}/peers/{peer_id}")
    async def delete_peer(project_token: str, peer_id: str) -> dict:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        try:
            from clade_cli.peer_ops import remove_peer_op  # noqa: PLC0415
            result = remove_peer_op(
                project_dir=_project_dir_for(setup),
                agent_dir=_agent_dir(),
                peer_id=peer_id,
                relay_url_for_reload=setup.relay_url,
                setup_server_url_for_reload=None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        _refresh_setup_in_memory(setup)
        return result

    @app.put("/api/setup/{project_token}/teams")
    async def update_teams(project_token: str, req: UpdateTeamsRequest) -> dict:
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        try:
            from clade_cli.peer_ops import update_teams_op  # noqa: PLC0415
            result = update_teams_op(
                project_dir=_project_dir_for(setup),
                agent_dir=_agent_dir(),
                teams=req.teams,
                setup_server_url_for_reload=None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Teams don't change peer state; no refresh needed
        return result

    @app.get("/api/setup/{project_token}/teams")
    async def get_teams(project_token: str) -> dict:
        """Read current teams structure from disk (any peer yaml has the same teams)."""
        setup = state.setups.get(project_token)
        if setup is None:
            raise HTTPException(404, "Setup not found")
        if not setup.peers:
            return {"teams": {}}
        # Read from first yaml on disk
        pd = state.data_dir / setup.project_token
        any_peer = next(iter(setup.peers.keys()))
        yaml_path = pd / f"{any_peer}.yaml"
        if not yaml_path.exists():
            return {"teams": {}}
        data = yaml.safe_load(yaml_path.read_text()) or {}
        return {"teams": data.get("teams", {})}

    # --- v1.11.0: reload setups from disk (used by clade-add-peer) ---

    @app.post("/admin/reload")
    async def admin_reload() -> dict:
        """Re-read every setup.json from disk into the in-memory `state.setups`
        dict. Used after out-of-band edits (e.g. clade-add-peer adds a new peer
        artifact to setup.json) so that subsequent calls to /agent/<token>/...
        endpoints see the new download_token. The relay subprocess is NOT
        respawned — only file-level state is refreshed."""
        before_setups = set(state.setups.keys())
        before_peer_counts = {tok: len(s.peers) for tok, s in state.setups.items()}
        new_setups = _load_setups(state.data_dir)
        # Preserve the live relay_subprocess handle for any setup that already
        # had one — _load_setups would respawn it, but here we want to keep it.
        for token, new_setup in new_setups.items():
            if token in state.setups and state.setups[token].relay_subprocess is not None:
                new_setup.relay_subprocess = state.setups[token].relay_subprocess
        state.setups = new_setups
        after_peer_counts = {tok: len(s.peers) for tok, s in state.setups.items()}
        added_setups = sorted(set(state.setups.keys()) - before_setups)
        removed_setups = sorted(before_setups - set(state.setups.keys()))
        peer_count_delta = {
            tok: after_peer_counts[tok] - before_peer_counts.get(tok, 0)
            for tok in state.setups.keys()
            if after_peer_counts[tok] != before_peer_counts.get(tok, 0)
        }
        return {
            "ok": True,
            "setups_loaded": len(state.setups),
            "setups_added": added_setups,
            "setups_removed": removed_setups,
            "peer_count_delta": peer_count_delta,
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

    # v1.4.2: ucitaj persistovane setup-e + smart-respawn relay-a ako treba
    state.setups = _load_setups(state.data_dir)

    app = create_app(state)

    print(f"\nClade A2A Setup Server")
    print(f"=======================")
    print(f"  Open in browser:  {public_url_base}/")
    print(f"  Bind:             {args.host}:{args.port}")
    print(f"  Data dir:         {data_dir}")
    print(f"  Loaded setups:    {len(state.setups)}")
    if state.setups:
        for s in state.setups.values():
            relay_state = "running" if (
                s.relay_subprocess is not None
                or _is_relay_already_up(s.relay_url)
            ) else "stopped"
            print(f"    - {s.project_name}: {len(s.peers)} peers, relay {relay_state}")
    print(f"")
    print(f"  Stop:             Ctrl+C (relay-i ostaju da rade preko restart-a)")
    print(f"")

    # v1.4.2: ne ubijaj relay subprocess-e na shutdown — moraju da prezive
    # restart setup-server-a da peer install-i ne pucnu. Eksplicitno kill se
    # radi preko scripts/clade-cleanup.sh ili kill -PID.
    def handle_signal(sig, _frame):
        LOG.info("signal %s received, shutting down (relay-i nastavljaju da rade)",
                 signal.Signals(sig).name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        import uvicorn  # noqa: PLC0415
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

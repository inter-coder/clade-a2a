"""Generatori za systemd unit + .mcp.json + default peers.yaml.

Svi `render_*` su pure-Python: uzmu strukturisane podatke, vrate string.
`clade init` ih onda upisuje (sa --apply flag-om) ili samo prikaze (dry-run).

systemd template iz samozapazanja §2.12. .mcp.json format iz PR#1 verifikacije
Claude Code MCP klijenta (samo `stdio`/`http`/`sse` su podrzani; mi koristimo
`http` na 127.0.0.1:PORT)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent

from clade.peers_config import PeerEntry, PeersConfig


def render_systemd_unit(peer_id: str, peers_yaml_path: str | Path,
                         clade_executable: str | None = None) -> str:
    """`~/.config/systemd/user/clade-<peer>.service` template.

    `clade_executable` defaults to `which clade` ili fallback na
    `python -m clade`. Ako nije postavljen i nista nije u PATH-u → ValueError."""
    if clade_executable is None:
        # Detektuj `clade` binary u PATH-u ili fallback na module invocation
        clade_executable = shutil.which("clade")
        if clade_executable is None:
            # Pripremiti za PR#5 kad `clade` console script bude registrovan;
            # za sad koristi `python -m clade`
            import sys  # noqa: PLC0415
            clade_executable = f"{sys.executable} -m clade"

    return dedent(f"""\
        [Unit]
        Description=Clade A2A peer ({peer_id})
        After=network.target

        [Service]
        Type=notify
        ExecStart={clade_executable} serve --peer {peer_id} --config {peers_yaml_path}
        Restart=on-failure
        RestartSec=2s
        WatchdogSec=60s
        StandardOutput=journal
        StandardError=journal
        Environment=CLADE_CONFIG={peers_yaml_path}

        [Install]
        WantedBy=default.target
        """)


def render_mcp_json(peer_entry: PeerEntry) -> str:
    """`.mcp.json` u peer workdir-u — Claude Code ce ga discover-ovati pri
    `cd <workdir> && claude`. Format po PR#1 verifikaciji: `type=http`,
    `url=http://127.0.0.1:PORT`."""
    if peer_entry.http_port is None:
        raise ValueError("peer mora imati http_port da bi mogao da generise .mcp.json")
    config = {
        "mcpServers": {
            "clade": {
                "type": "http",
                "url": f"http://127.0.0.1:{peer_entry.http_port}",
            },
        },
    }
    return json.dumps(config, indent=2) + "\n"


def render_default_peers_yaml(self_id: str, peer_ids: list[str],
                               http_port_start: int = 9001,
                               socket_dir: str | None = None,
                               audit_dir: str | None = None,
                               workdir_root: str | None = None) -> str:
    """Default peers.yaml za prvi-put init. Konvencija putanja:
    - socket: /run/user/<uid>/clade/<peer>.sock (XDG_RUNTIME_DIR)
    - audit_db: ~/.local/state/clade/<peer>/audit.db
    - workdir: ~/.local/state/clade/workdirs/<peer>
    - http_port: pocetni + index u listi

    Self peer dobija role=interactive (Claude Code se konektuje na njega),
    ostali dobijaju role=headless."""
    import os  # noqa: PLC0415
    if socket_dir is None:
        uid = os.getuid()
        socket_dir = f"/run/user/{uid}/clade"
    audit_dir = audit_dir or "~/.local/state/clade"
    workdir_root = workdir_root or "~/.local/state/clade/workdirs"

    lines = [
        "version: 2",
        f"self: {self_id}",
        "peers:",
    ]
    all_peers = sorted({*peer_ids, self_id})
    for i, peer in enumerate(all_peers):
        role = "interactive" if peer == self_id else "headless"
        port = http_port_start + i
        lines.append(f"  {peer}:")
        lines.append(f"    transport: unix")
        lines.append(f"    role: {role}")
        lines.append(f"    socket: {socket_dir}/{peer}.sock")
        if peer == self_id:
            lines.append(f"    http_port: {port}")
            lines.append(f"    audit_db: {audit_dir}/{peer}/audit.db")
            lines.append(f"    workdir: {workdir_root}/{peer}")
    return "\n".join(lines) + "\n"


def default_paths() -> dict[str, Path]:
    """Konvencionalne putanje. Vraca dict za upotrebu u init i drugde."""
    home = Path.home()
    return {
        "peers_yaml": home / ".config" / "clade" / "peers.yaml",
        "systemd_user_dir": home / ".config" / "systemd" / "user",
    }

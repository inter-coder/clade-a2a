"""PR#4 testovi za clade.init + clade.templates + peers_config v2 schema."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clade.init import InitPlan, apply, format_plan_for_display, plan
from clade.peers_config import PeerEntry, PeersConfig, load
from clade.templates import render_default_peers_yaml, render_mcp_json, render_systemd_unit


# ---- peers_config v2 schema ----


def test_v2_schema_with_all_fields(tmp_path: Path):
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "version": 2,
        "self": "katana",
        "peers": {
            "katana": {
                "transport": "unix",
                "role": "interactive",
                "socket": "/run/user/1000/clade/katana.sock",
                "http_port": 9001,
                "audit_db": "~/.local/state/clade/katana/audit.db",
                "workdir": "~/clade-projects/test/workdirs/katana",
            },
            "dusan": {
                "transport": "unix",
                "role": "headless",
                "socket": "/run/user/1000/clade/dusan.sock",
            },
        },
    }))
    cfg = load(cfg_path)
    assert cfg.version == 2
    assert cfg.me().role == "interactive"
    assert cfg.peers["dusan"].role == "headless"


def test_unsupported_version_rejected(tmp_path: Path):
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({"version": 1, "self": "x", "peers": {"x": {}}}))
    with pytest.raises(ValidationError, match="version=1 not supported"):
        load(cfg_path)


def test_transport_relay_requires_relay_url():
    """v2.2.0: schema validacija samo zahteva relay_url. secret_hex i
    bearer_token su asimetricni (self vs other), check ide na use-site."""
    with pytest.raises(ValidationError, match="relay_url"):
        PeerEntry(transport="relay", role="headless")


def test_transport_relay_no_secret_hex_is_valid():
    """v2.2.0: secret_hex nije obavezan na schema nivou — runtime check
    proverava asimetricno (self ne treba; other peer treba)."""
    # Self-style entry (sa bearer_token) — validan
    entry = PeerEntry(transport="relay", relay_url="https://r", bearer_token="tk")
    assert entry.relay_url == "https://r"
    # Other-peer-style entry (sa secret_hex) — validan
    entry = PeerEntry(transport="relay", relay_url="https://r", secret_hex="deadbeef" * 8)
    assert entry.secret_hex == "deadbeef" * 8


def test_secret_hex_env_substitution(monkeypatch):
    monkeypatch.setenv("CLADE_TEST_SECRET", "deadbeef" * 8)
    entry = PeerEntry(
        transport="relay",
        relay_url="https://r",
        secret_hex="${env:CLADE_TEST_SECRET}",
        bearer_token="tk",
    )
    assert entry.secret_hex == "deadbeef" * 8


def test_secret_hex_env_unset_keeps_literal():
    """Ako env var nije set, ostavi literalan string (ne crash) — downstream
    moze reagovati ili ga upotrebiti kao placeholder."""
    entry = PeerEntry(
        transport="relay",
        relay_url="https://r",
        secret_hex="${env:CLADE_TEST_DEFINITELY_UNSET}",
        bearer_token="tk",
    )
    assert entry.secret_hex == "${env:CLADE_TEST_DEFINITELY_UNSET}"


def test_interactive_peers_filter():
    cfg = PeersConfig(
        self="katana",
        peers={
            "katana": PeerEntry(role="interactive", socket="/tmp/k.sock"),
            "dusan": PeerEntry(role="headless", socket="/tmp/d.sock"),
            "both": PeerEntry(role="both", socket="/tmp/b.sock"),
        },
    )
    interactive = cfg.interactive_peers()
    assert set(interactive) == {"katana", "both"}


# ---- templates ----


def test_render_systemd_unit_basic():
    text = render_systemd_unit("katana", "/home/x/.config/clade/peers.yaml",
                                clade_executable="/usr/local/bin/clade")
    assert "Description=Clade A2A peer (katana)" in text
    assert "Type=notify" in text
    assert "WatchdogSec=60s" in text
    assert "ExecStart=/usr/local/bin/clade serve --peer katana" in text
    assert "Restart=on-failure" in text


def test_render_mcp_json_http_transport():
    """PR#1 verifikacija: Claude Code MCP klijent prihvata `type=http`."""
    entry = PeerEntry(role="interactive", socket="/tmp/x.sock", http_port=9001)
    import json as _json  # noqa: PLC0415
    parsed = _json.loads(render_mcp_json(entry))
    assert parsed["mcpServers"]["clade"]["type"] == "http"
    assert parsed["mcpServers"]["clade"]["url"] == "http://127.0.0.1:9001"


def test_render_mcp_json_requires_http_port():
    entry = PeerEntry(role="interactive", socket="/tmp/x.sock")
    with pytest.raises(ValueError, match="http_port"):
        render_mcp_json(entry)


def test_render_default_peers_yaml_self_interactive():
    """Generisan yaml: self peer dobija role=interactive + http_port/audit/workdir."""
    text = render_default_peers_yaml("katana", ["dusan", "predrag"], http_port_start=9001)
    data = yaml.safe_load(text)
    assert data["version"] == 2
    assert data["self"] == "katana"
    assert set(data["peers"]) == {"katana", "dusan", "predrag"}
    # Self peer dobija punu konfiguraciju
    me = data["peers"]["katana"]
    assert me["role"] == "interactive"
    assert "http_port" in me
    assert "audit_db" in me
    assert "workdir" in me
    # Drugi peer-ovi dobijaju samo socket
    other = data["peers"]["dusan"]
    assert other["role"] == "headless"
    assert other.get("http_port") is None


# ---- init.plan() ----


def test_init_plan_first_run(tmp_path: Path):
    """Prvi run bez postojeceg peers.yaml — sve treba kreirati."""
    yaml_path = tmp_path / "peers.yaml"
    sysd_dir = tmp_path / "systemd"
    workdir_root = tmp_path / "workdirs"
    p = plan("katana", ["dusan"], peers_yaml_path=yaml_path, systemd_dir=sysd_dir,
             workdir_root=str(workdir_root), audit_dir=str(tmp_path / "state"),
             socket_dir=str(tmp_path / "sockets"))

    # peers.yaml + systemd unit treba kreirati; .mcp.json za interactive peer (katana)
    created_paths = {p[0] for p in p.files_to_create}
    assert yaml_path in created_paths
    assert sysd_dir / "clade-katana.service" in created_paths
    mcp_paths = [path for path, _ in p.files_to_create if path.name == ".mcp.json"]
    assert len(mcp_paths) == 1
    assert "katana" in str(mcp_paths[0])
    assert str(workdir_root) in str(mcp_paths[0])


def test_init_plan_requires_peers_on_first_run(tmp_path: Path):
    yaml_path = tmp_path / "peers.yaml"
    with pytest.raises(ValueError, match="peer-ova"):
        plan("katana", peer_ids=None, peers_yaml_path=yaml_path)


def test_init_plan_idempotent_when_files_exist(tmp_path: Path):
    """Drugi run posle prvog: sve fajlove vidi i prijavljuje 'skipped'."""
    yaml_path = tmp_path / "peers.yaml"
    sysd_dir = tmp_path / "systemd"
    kw = dict(workdir_root=str(tmp_path / "workdirs"),
              audit_dir=str(tmp_path / "state"),
              socket_dir=str(tmp_path / "sockets"))
    p1 = plan("katana", ["dusan"], peers_yaml_path=yaml_path, systemd_dir=sysd_dir, **kw)  # type: ignore[arg-type]
    apply(p1)

    p2 = plan("katana", ["dusan"], peers_yaml_path=yaml_path, systemd_dir=sysd_dir, **kw)  # type: ignore[arg-type]
    assert p2.is_empty() or len(p2.files_to_create) == 0
    assert yaml_path in p2.files_to_skip
    assert sysd_dir / "clade-katana.service" in p2.files_to_skip


def test_init_apply_writes_with_0600_mode(tmp_path: Path):
    yaml_path = tmp_path / "peers.yaml"
    sysd_dir = tmp_path / "systemd"
    p = plan("katana", ["dusan"], peers_yaml_path=yaml_path, systemd_dir=sysd_dir,
             workdir_root=str(tmp_path / "workdirs"),
             audit_dir=str(tmp_path / "state"),
             socket_dir=str(tmp_path / "sockets"))
    count = apply(p)
    assert count > 0
    assert (yaml_path.stat().st_mode & 0o777) == 0o600


def test_init_plan_rejects_self_mismatch(tmp_path: Path):
    """Ako peers.yaml postoji sa drugacijim self, init odbije bez modifikacije."""
    yaml_path = tmp_path / "peers.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(yaml.dump({
        "version": 2, "self": "katana",
        "peers": {"katana": {"socket": "/tmp/k.sock"}},
    }))

    with pytest.raises(ValueError, match="ne odgovara"):
        plan("predrag", None, peers_yaml_path=yaml_path)


def test_format_plan_includes_next_steps(tmp_path: Path):
    yaml_path = tmp_path / "peers.yaml"
    sysd_dir = tmp_path / "systemd"
    p = plan("katana", ["dusan"], peers_yaml_path=yaml_path, systemd_dir=sysd_dir,
             workdir_root=str(tmp_path / "workdirs"),
             audit_dir=str(tmp_path / "state"),
             socket_dir=str(tmp_path / "sockets"))
    text = format_plan_for_display(p)
    assert "systemctl --user enable" in text
    assert "clade-katana" in text


# ---- CLI integration ----


def test_cli_init_dry_run(tmp_path: Path):
    """`python -m clade init --self X --peer Y` bez --apply prikaze plan."""
    yaml_path = tmp_path / "peers.yaml"
    proc = subprocess.run(
        [sys.executable, "-m", "clade", "init",
         "--self", "katana", "--peer", "dusan",
         "--config", str(yaml_path)],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "dry-run" in proc.stdout
    assert "Fajlovi koje bi kreirao" in proc.stdout
    # Sanity: nije stvarno napisao
    assert not yaml_path.exists()


def test_cli_init_apply_creates_files(tmp_path: Path):
    """`--apply` stvarno zapise fajlove."""
    yaml_path = tmp_path / "peers.yaml"
    proc = subprocess.run(
        [sys.executable, "-m", "clade", "init",
         "--self", "katana", "--peer", "dusan",
         "--config", str(yaml_path),
         "--apply"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "Applied" in proc.stdout
    assert yaml_path.exists()
    cfg = load(yaml_path)
    assert cfg.self == "katana"

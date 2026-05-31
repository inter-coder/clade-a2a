"""v1.17.0 — tests for the kill-switch + scribe-state + repo-log web endpoints
and the scribe field in PATCH /api/setup/{token}/peers/{peer_id}.

Builds a real setup via _generate_setup (no setup-server subprocess needed)
and hits the endpoints via FastAPI TestClient. Uses an isolated
CLADE_PAUSE_DIR so the test never touches the user's ~/.clade/ sentinels.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from clade_cli import aitf_import
from clade_cli.setup_server import (
    AppState, PeerInput, SetupForm, _generate_setup, _save_setup, create_app,
)


def _make_aitf_project(root: Path) -> Path:
    """Reuse the synthetic AITF builder from the import tests."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ai-team-config.yml").write_text(yaml.dump({
        "project": {"name": "kt", "owner_name": "u"},
        "doc_optimizer_enabled": True,
    }))
    team = root / "docs" / "TEAM"
    team.mkdir(parents=True, exist_ok=True)
    for f in ("PROJECT_DIRECTOR", "DEVELOPMENT_DIRECTOR", "DEVELOPMENT_TEAM", "DOC_OPTIMIZER"):
        (team / f"{f}.md").write_text(f"# {f}\nbody\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


def _client(tmp_path: Path) -> tuple[TestClient, AppState, str]:
    project = _make_aitf_project(tmp_path / "proj")
    parsed = aitf_import.parse_aitf_project(project)
    peer_inputs = [PeerInput(peer_id=p.peer_id, display_name=p.display_name,
                             role=p.role, extra_add_dirs=p.extra_add_dirs, scribe=p.scribe)
                   for p in parsed.peers]
    form = SetupForm(project_name="kt", relay_host="127.0.0.1",
                     relay_url_host="127.0.0.1", relay_port=17900, start_relay=False,
                     peers=peer_inputs, teams=parsed.teams)
    state = AppState(data_dir=tmp_path / "data", public_url_base="http://localhost")
    state.data_dir.mkdir(parents=True)
    setup = _generate_setup(form, state.data_dir)
    # Redirect every audit_db into tmp_path so tests can't pollute ~/.clade/
    # (the persisted scribe-state.json file lives next to audit_db).
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    for pid in setup.peers.keys():
        yp = state.data_dir / setup.project_token / f"{pid}.yaml"
        d = yaml.safe_load(yp.read_text())
        d["audit_db"] = str(audit_root / pid / "audit.db")
        yp.write_text(yaml.dump(d, default_flow_style=False, sort_keys=False))
    _save_setup(setup, state.data_dir)
    state.setups[setup.project_token] = setup
    return TestClient(create_app(state)), state, setup.project_token


# ---- pause/resume endpoints ----

def test_pause_global_creates_sentinel(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.post(f"/api/setup/{tok}/pause", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scope"] == "global"
    assert (pause_dir / "pause").exists()


def test_pause_per_peer_creates_sentinel(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.post(f"/api/setup/{tok}/pause", json={"peer_id": "team"})
    assert r.status_code == 200
    assert r.json()["scope"] == "peer:team"
    assert (pause_dir / "team-pause").exists()
    assert not (pause_dir / "pause").exists()


def test_pause_unknown_peer_returns_400(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.post(f"/api/setup/{tok}/pause", json={"peer_id": "ghost"})
    assert r.status_code == 400
    assert "Unknown peer_id" in r.text


def test_resume_removes_sentinel(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    client, _, tok = _client(tmp_path)
    r = client.post(f"/api/setup/{tok}/resume", json={})
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert not (pause_dir / "pause").exists()


def test_resume_idempotent_when_no_sentinel(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.post(f"/api/setup/{tok}/resume", json={"peer_id": "team"})
    assert r.status_code == 200
    assert r.json()["removed"] is False


# ---- scribe-state endpoint ----

def test_scribe_state_returns_all_peers(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.get(f"/api/setup/{tok}/scribe-state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["global_paused"] is False
    assert set(body["peers"].keys()) == {"pd", "dd", "team", "doc"}
    for pid, info in body["peers"].items():
        assert info["scribe_enabled"] is True
        assert info["interval_minutes"] in (15, 30, 60)
        assert info["paused"] is False
        assert info["rounds_today"] == 0  # no state file yet
        assert info["last_head_sha"] is None


def test_scribe_state_reflects_global_pause(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    client, _, tok = _client(tmp_path)
    body = client.get(f"/api/setup/{tok}/scribe-state").json()
    assert body["global_paused"] is True
    assert all(p["paused"] for p in body["peers"].values())
    assert all(p["paused_scope"] == "global" for p in body["peers"].values())


def test_scribe_state_reflects_per_peer_pause(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    pause_dir.mkdir()
    (pause_dir / "team-pause").touch()
    client, _, tok = _client(tmp_path)
    body = client.get(f"/api/setup/{tok}/scribe-state").json()
    assert body["global_paused"] is False
    assert body["peers"]["team"]["paused"] is True
    assert body["peers"]["team"]["paused_scope"] == "peer:team"
    assert body["peers"]["pd"]["paused"] is False


def test_scribe_state_picks_up_persisted_state_file(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, state, tok = _client(tmp_path)
    # Drop a fake state file next to the audit_db parent
    doc_yaml = yaml.safe_load((state.data_dir / tok / "doc.yaml").read_text())
    audit_parent = Path(doc_yaml["audit_db"]).expanduser().parent
    audit_parent.mkdir(parents=True, exist_ok=True)
    (audit_parent / "doc-scribe-state.json").write_text(json.dumps({
        "last_head_sha": "abcdef1234567890",
        "rounds_today": [1.0, 2.0, 3.0],
    }))
    body = client.get(f"/api/setup/{tok}/scribe-state").json()
    assert body["peers"]["doc"]["last_head_sha"] == "abcdef1234567890"
    assert body["peers"]["doc"]["rounds_today"] == 3


# ---- repo-log endpoint ----

def test_repo_log_returns_commits(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.get(f"/api/setup/{tok}/repo-log?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["repo_path"]
    assert len(body["commits"]) >= 1
    c0 = body["commits"][0]
    assert "sha" in c0 and "short" in c0 and "iso" in c0 and "author" in c0 and "subject" in c0
    assert c0["subject"] == "init"


def test_repo_log_limit_capped(tmp_path, monkeypatch):
    pause_dir = tmp_path / "pd"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    client, _, tok = _client(tmp_path)
    r = client.get(f"/api/setup/{tok}/repo-log?limit=500")
    assert r.status_code == 200


# ---- PATCH peer scribe edit ----

def test_patch_peer_writes_scribe_block(tmp_path, monkeypatch):
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(tmp_path / "pd"))
    monkeypatch.setenv("CLADE_AGENT_DIR", str(tmp_path / "agent"))
    client, state, tok = _client(tmp_path)

    # Start with team's existing scribe interval=30 → bump to 45
    r = client.patch(f"/api/setup/{tok}/peers/team", json={
        "scribe": {"interval_minutes": 45, "round_prompt": "NEW PROMPT"},
    })
    assert r.status_code == 200, r.text
    new_yaml = yaml.safe_load((state.data_dir / tok / "team.yaml").read_text())
    assert new_yaml["scribe"]["interval_minutes"] == 45
    assert new_yaml["scribe"]["round_prompt"] == "NEW PROMPT"
    # Other scribe keys (enabled, max_rounds_per_day, docs_repo_path) preserved
    assert new_yaml["scribe"]["enabled"] is True
    assert "docs_repo_path" in new_yaml["scribe"]


def test_patch_peer_empty_scribe_disables_block(tmp_path, monkeypatch):
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(tmp_path / "pd"))
    monkeypatch.setenv("CLADE_AGENT_DIR", str(tmp_path / "agent"))
    client, state, tok = _client(tmp_path)
    assert "scribe" in yaml.safe_load((state.data_dir / tok / "dd.yaml").read_text())
    r = client.patch(f"/api/setup/{tok}/peers/dd", json={"scribe": {}})
    assert r.status_code == 200
    assert "scribe" not in yaml.safe_load((state.data_dir / tok / "dd.yaml").read_text())

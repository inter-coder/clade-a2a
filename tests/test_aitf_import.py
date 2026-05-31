"""v1.13.0 (Phase A) — tests for AITF (AI Team Framework) project import.

Builds a synthetic AITF project on disk (.ai-team-config.yml + docs/TEAM/*.md),
then exercises both the pure parser (clade_cli.aitf_import) and the REST
endpoint (POST /api/setup/import-aitf) via FastAPI TestClient.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from clade_cli import aitf_import
from clade_cli.setup_server import AppState, create_app


def _make_aitf_project(root: Path, *, do_enabled: bool, project_name: str = "demo-app",
                       owner_name: str = "Alice") -> Path:
    """Synthesize a minimal AITF-scaffolded project under `root`."""
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "framework_version": "2.0.0",
        "language": "English",
        "claude_flags": "",
        "project": {
            "name": project_name,
            "description": "Test project",
            "tech_stack": "Python 3.13",
            "repository_url": "",
            "owner_name": owner_name,
        },
        "phases": [{"number": 1, "name": "MVP", "description": "First cut"}],
        "autonomy_level": "moderate",
        "review_strictness": "moderate",
        "dispatcher_control": "pd_dd",
        "test_command": "pytest",
        "dependency_policy": "conservative",
        "architecture": {"concepts": ["CLI"]},
        "code_organization": "src/ for code, tests/ for tests",
        "naming_conventions": {"files": "snake_case.py", "classes": "PascalCase",
                               "functions": "snake_case", "constants": "UPPER_SNAKE_CASE",
                               "variables": "snake_case"},
        "branch_strategy": "feature_branches",
        "commit_convention": "conventional commits",
        "special_rules": [],
        "docker_isolation": False,
        "backup_dir": "",
        "doc_optimizer_enabled": do_enabled,
    }
    (root / ".ai-team-config.yml").write_text(yaml.dump(config))
    team_dir = root / "docs" / "TEAM"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "PROJECT_DIRECTOR.md").write_text(
        f"# PD role for {project_name}\n\nYou are the Project Director.\nFollow the AITF protocol.\n"
    )
    (team_dir / "DEVELOPMENT_DIRECTOR.md").write_text(
        f"# DD role for {project_name}\n\nYou are the Development Director.\nTranslate strategy.\n"
    )
    (team_dir / "DEVELOPMENT_TEAM.md").write_text(
        f"# Team role for {project_name}\n\nYou are the Development Team.\nImplement the TODO.\n"
    )
    if do_enabled:
        (team_dir / "DOC_OPTIMIZER.md").write_text(
            f"# DO role for {project_name}\n\nYou are the Documentation Optimizer.\n"
        )
    return root


def test_detect_happy_path(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "myproj", do_enabled=True)
    ok, reason = aitf_import.detect_aitf_project(project)
    assert ok is True
    assert reason == "ok"


def test_detect_not_a_dir(tmp_path: Path):
    ok, reason = aitf_import.detect_aitf_project(tmp_path / "does-not-exist")
    assert ok is False
    assert "not a directory" in reason


def test_detect_missing_config(tmp_path: Path):
    project = tmp_path / "empty"
    project.mkdir()
    ok, reason = aitf_import.detect_aitf_project(project)
    assert ok is False
    assert ".ai-team-config.yml" in reason


def test_detect_missing_team_dir(tmp_path: Path):
    project = tmp_path / "noteam"
    project.mkdir()
    (project / ".ai-team-config.yml").write_text("framework_version: '2.0.0'\n")
    ok, reason = aitf_import.detect_aitf_project(project)
    assert ok is False
    assert "docs/TEAM" in reason


def test_parse_with_do(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "myproj", do_enabled=True,
                                 project_name="zeta", owner_name="Bob")
    out = aitf_import.parse_aitf_project(project)
    assert out.project_name == "zeta"
    assert out.owner_name == "Bob"
    assert out.doc_optimizer_enabled is True
    assert [p.peer_id for p in out.peers] == ["pd", "dd", "team", "doc"]
    # display names include project name
    assert all("zeta" in p.display_name for p in out.peers)
    # role text inlined from .md files
    pd = next(p for p in out.peers if p.peer_id == "pd")
    assert "Project Director" in pd.role
    assert "zeta" in pd.role  # came from PROJECT_DIRECTOR.md body
    # extra_add_dirs points to absolute project path
    for p in out.peers:
        assert p.extra_add_dirs == [str(project.resolve())]
    # teams: aitf_team has all, engineering has dd+team
    assert set(out.teams["aitf_team"]) == {"pd", "dd", "team", "doc"}
    assert set(out.teams["engineering"]) == {"dd", "team"}


def test_parse_without_do(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "myproj", do_enabled=False)
    out = aitf_import.parse_aitf_project(project)
    assert out.doc_optimizer_enabled is False
    assert [p.peer_id for p in out.peers] == ["pd", "dd", "team"]
    assert "doc" not in out.teams["aitf_team"]


def test_parse_missing_role_file_raises(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "myproj", do_enabled=False)
    (project / "docs" / "TEAM" / "DEVELOPMENT_TEAM.md").unlink()
    with pytest.raises(FileNotFoundError, match="DEVELOPMENT_TEAM.md"):
        aitf_import.parse_aitf_project(project)


def test_parse_empty_role_file_raises(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "myproj", do_enabled=False)
    (project / "docs" / "TEAM" / "PROJECT_DIRECTOR.md").write_text("   \n  \n")
    with pytest.raises(ValueError, match="empty"):
        aitf_import.parse_aitf_project(project)


def test_parse_falls_back_to_dir_name_if_no_project_name(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "fallback-name", do_enabled=False, project_name="")
    out = aitf_import.parse_aitf_project(project)
    assert out.project_name == "fallback-name"


def test_endpoint_imports_aitf_project(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "endpoint-proj", do_enabled=True,
                                 project_name="rocket", owner_name="Mira")
    state = AppState(data_dir=tmp_path / "setup-data",
                     public_url_base="http://localhost:8000")
    state.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(state)
    client = TestClient(app)

    resp = client.post("/api/setup/import-aitf", json={
        "project_path": str(project),
        "relay_url_host": "127.0.0.1",
        "relay_port": 17777,
        "relay_host": "127.0.0.1",
        "start_relay": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["doc_optimizer_enabled"] is True
    assert data["peers"] == ["pd", "dd", "team", "doc"]
    assert set(data["teams"]["aitf_team"]) == {"pd", "dd", "team", "doc"}
    assert set(data["teams"]["engineering"]) == {"dd", "team"}

    # Verify setup actually materialized on disk
    project_token = data["project_token"]
    assert project_token in state.setups
    setup = state.setups[project_token]
    assert set(setup.peers.keys()) == {"pd", "dd", "team", "doc"}

    # Every peer yaml has extra_add_dirs pointing at the AITF project
    abs_project = str(project.resolve())
    pd_yaml = yaml.safe_load((state.data_dir / project_token / "pd.yaml").read_text())
    assert pd_yaml["extra_add_dirs"] == [abs_project]
    assert pd_yaml["my_id"] == "pd"
    assert "Project Director" in pd_yaml["role"]
    assert "rocket" in pd_yaml["role"]
    # teams stored in yaml
    assert set(pd_yaml["teams"]["aitf_team"]) == {"pd", "dd", "team", "doc"}
    assert set(pd_yaml["teams"]["engineering"]) == {"dd", "team"}


def test_endpoint_returns_400_on_invalid_path(tmp_path: Path):
    state = AppState(data_dir=tmp_path / "setup-data",
                     public_url_base="http://localhost:8000")
    state.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(state)
    client = TestClient(app)

    resp = client.post("/api/setup/import-aitf", json={
        "project_path": str(tmp_path / "nonexistent"),
        "relay_url_host": "127.0.0.1",
        "start_relay": False,
    })
    assert resp.status_code == 400
    assert "AITF import failed" in resp.text


def test_endpoint_project_name_override(tmp_path: Path):
    project = _make_aitf_project(tmp_path / "orig", do_enabled=False, project_name="original")
    state = AppState(data_dir=tmp_path / "setup-data",
                     public_url_base="http://localhost:8000")
    state.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(state)
    client = TestClient(app)

    resp = client.post("/api/setup/import-aitf", json={
        "project_path": str(project),
        "relay_url_host": "127.0.0.1",
        "relay_port": 17778,
        "start_relay": False,
        "project_name_override": "renamed",
    })
    assert resp.status_code == 200, resp.text
    setup = state.setups[resp.json()["project_token"]]
    assert setup.project_name == "renamed"

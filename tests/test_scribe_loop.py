"""v1.14.0 (Phase B) — tests for the daemon scribe_loop and its helpers.

Exercises the cheap "anything changed?" path (no token spend when git HEAD
unchanged), state persistence across daemon restarts, and the round-trigger
branch (real git repo with a commit creates new HEAD -> _scribe_tick spawns).

Spawn is monkeypatched in tests that go through _scribe_tick — we never invoke
the real `claude --print`.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_daemon_with_config(tmp_path: Path, cfg_dict: dict):
    """Fresh import of agent.main + agent.daemon with CLADE_CONFIG pointed at
    a synthesized yaml. Tests that need an isolated cfg should call this."""
    import yaml as _yaml
    cfg_path = tmp_path / "peer.yaml"
    cfg_path.write_text(_yaml.dump(cfg_dict))
    import os
    os.environ["CLADE_CONFIG"] = str(cfg_path)
    for mod in ("agent.daemon", "agent.main"):
        sys.modules.pop(mod, None)
    import agent.daemon as daemon  # noqa: PLC0415
    return daemon


def _scribe_cfg(my_id: str, audit_db: str, *, scribe_extra: dict | None = None,
                docs_repo_path: str | None = None) -> dict:
    scribe = {"enabled": True, "interval_minutes": 60, "max_rounds_per_day": 24}
    if docs_repo_path:
        scribe["docs_repo_path"] = docs_repo_path
    if scribe_extra:
        scribe.update(scribe_extra)
    return {
        "my_id": my_id,
        "bearer_token": "tok",
        "peers": {},
        "audit_db": audit_db,
        "extra_add_dirs": [],
        "scribe": scribe,
    }


def _init_git_repo(repo: Path, initial_file: str = "README.md", initial_text: str = "initial\n") -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
    (repo / initial_file).write_text(initial_text)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    return head


def _new_commit(repo: Path, filename: str, text: str, msg: str) -> str:
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()


def test_state_path_lives_next_to_audit_db(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "doc-audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg
    p = daemon._scribe_state_path(cfg)
    assert p == tmp_path / "doc-scribe-state.json"


def test_state_round_trip(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "doc-audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg
    assert daemon._scribe_load_state(cfg) == {"last_head_sha": None, "rounds_today": []}
    daemon._scribe_save_state(cfg, {"last_head_sha": "abc123", "rounds_today": [1.0, 2.0]})
    loaded = daemon._scribe_load_state(cfg)
    assert loaded["last_head_sha"] == "abc123"
    assert loaded["rounds_today"] == [1.0, 2.0]


def test_state_load_handles_corrupt_file(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "doc-audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg
    sp = daemon._scribe_state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not json {{{")
    loaded = daemon._scribe_load_state(cfg)
    assert loaded["last_head_sha"] is None  # fresh start, not crash


def test_resolve_repo_prefers_scribe_path_over_extra_add_dirs(tmp_path: Path):
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    primary.mkdir()
    fallback.mkdir()
    cfg_dict = _scribe_cfg("doc", str(tmp_path / "audit.db"),
                            docs_repo_path=str(primary))
    cfg_dict["extra_add_dirs"] = [str(fallback)]
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg
    assert daemon._scribe_resolve_repo(cfg) == primary.resolve()


def test_resolve_repo_falls_back_to_extra_add_dirs(tmp_path: Path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    cfg_dict = _scribe_cfg("doc", str(tmp_path / "audit.db"))
    cfg_dict["extra_add_dirs"] = [str(fallback)]
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg
    assert daemon._scribe_resolve_repo(cfg) == fallback.resolve()


def test_resolve_repo_none_when_nothing_set(tmp_path: Path):
    cfg_dict = _scribe_cfg("doc", str(tmp_path / "audit.db"))
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg
    assert daemon._scribe_resolve_repo(cfg) is None


@pytest.mark.asyncio
async def test_git_head_returns_real_sha(tmp_path: Path):
    repo = tmp_path / "repo"
    expected = _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo)))
    head = await daemon._scribe_git_head(repo)
    assert head == expected


@pytest.mark.asyncio
async def test_git_head_returns_none_for_non_repo(tmp_path: Path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(not_a_repo)))
    head = await daemon._scribe_git_head(not_a_repo)
    assert head is None


@pytest.mark.asyncio
async def test_tick_skips_when_head_unchanged(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg

    called = []
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        called.append(True)
        return "should not be called"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    # Pre-fill state with current HEAD — tick should bail out cheaply
    state = {"last_head_sha": head, "rounds_today": []}
    spawned = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert spawned is False
    assert called == []
    assert state["last_head_sha"] == head  # unchanged


@pytest.mark.asyncio
async def test_tick_spawns_when_new_commits(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    sha1 = _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg

    called_with = {}
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        called_with["prompt"] = prompt
        return "ok scribe done"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    workdir = tmp_path / "wd"
    workdir.mkdir()
    state = {"last_head_sha": sha1, "rounds_today": []}

    sha2 = _new_commit(repo, "DIRECTIVES/001.md", "first directive\n", "add D001")
    spawned = await daemon._scribe_tick(cfg, workdir, False, state)

    assert spawned is True
    assert "prompt" in called_with
    assert "DIRECTIVES/001.md" in called_with["prompt"] or "add D001" in called_with["prompt"]
    assert state["last_head_sha"] == sha2
    assert len(state["rounds_today"]) == 1
    # Persisted to disk so next daemon start resumes
    persisted = json.loads((tmp_path / "doc-scribe-state.json").read_text())
    assert persisted["last_head_sha"] == sha2


@pytest.mark.asyncio
async def test_tick_respects_day_cap(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    sha1 = _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo),
        scribe_extra={"max_rounds_per_day": 2}))
    from agent.main import cfg
    import time as _t

    called = []
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        called.append(True)
        return "ok"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    # Two fresh rounds within last hour -> cap reached
    state = {"last_head_sha": sha1, "rounds_today": [_t.time() - 10, _t.time() - 5]}
    _new_commit(repo, "NEW.md", "x", "new commit")
    spawned = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert spawned is False
    assert called == []


@pytest.mark.asyncio
async def test_tick_prunes_old_round_timestamps(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo)))
    import time as _t
    state = {"last_head_sha": "x", "rounds_today": [_t.time() - 25 * 3600, _t.time() - 1]}
    daemon._scribe_prune_today(state)
    assert len(state["rounds_today"]) == 1  # 25h-old timestamp dropped
    assert state["rounds_today"][0] > _t.time() - 2


@pytest.mark.asyncio
async def test_loop_exits_immediately_when_disabled(tmp_path: Path):
    cfg_dict = _scribe_cfg("doc", str(tmp_path / "audit.db"))
    cfg_dict["scribe"]["enabled"] = False
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg
    # Loop should return without ever sleeping
    await asyncio.wait_for(daemon.scribe_loop(cfg, tmp_path / "wd", False), timeout=1)


@pytest.mark.asyncio
async def test_loop_exits_immediately_when_no_scribe_config(tmp_path: Path):
    cfg_dict = _scribe_cfg("doc", str(tmp_path / "audit.db"))
    del cfg_dict["scribe"]
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg
    assert cfg.scribe is None
    await asyncio.wait_for(daemon.scribe_loop(cfg, tmp_path / "wd", False), timeout=1)


@pytest.mark.asyncio
async def test_tick_uses_round_prompt_when_set(tmp_path: Path, monkeypatch):
    """v1.15.0 (Phase C): if scribe.round_prompt is set, _scribe_tick uses it
    instead of the default scribe documentation prompt. PD/DD/Team rely on this."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg_dict = _scribe_cfg("pd", str(tmp_path / "audit.db"), docs_repo_path=str(repo),
                            scribe_extra={"round_prompt": "MARKER_PD_PROMPT do your specific job"})
    daemon = _load_daemon_with_config(tmp_path, cfg_dict)
    from agent.main import cfg

    seen = {}
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        seen["prompt"] = prompt
        return "ok"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    state = {"last_head_sha": None, "rounds_today": []}
    spawned = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert spawned is True
    assert "MARKER_PD_PROMPT" in seen["prompt"], "round_prompt override not used"
    # Default Phase B scribe-specific phrasing must NOT appear when override is set
    assert "scribe round" not in seen["prompt"].lower() or "MARKER_PD_PROMPT" in seen["prompt"]
    # Context (repo + commits) is still injected ahead of the override
    assert "NEW COMMITS SINCE LAST ROUND" in seen["prompt"]


@pytest.mark.asyncio
async def test_tick_falls_back_to_default_prompt_when_no_round_prompt(tmp_path: Path, monkeypatch):
    """When round_prompt is None (doc peer's default), use the original Phase B prompt."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(tmp_path, _scribe_cfg(
        "doc", str(tmp_path / "audit.db"), docs_repo_path=str(repo)))
    from agent.main import cfg
    assert cfg.scribe.round_prompt is None

    seen = {}
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        seen["prompt"] = prompt
        return "ok"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    state = {"last_head_sha": None, "rounds_today": []}
    await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    # Default prompt references documentation-curator behavior
    assert "PROJECT_STATUS.md" in seen["prompt"]
    assert "OPTIMIZATION_LOG.md" in seen["prompt"]
    assert "nudge" in seen["prompt"]

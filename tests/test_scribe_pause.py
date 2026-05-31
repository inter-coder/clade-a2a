"""v1.16.0 (Phase D) — tests for the scribe kill-switch.

Covers the daemon-side `_scribe_paused` helper, the `_scribe_tick` integration
(paused tick cheap-exits without git/spawn calls), and the `clade-pause` /
`clade-resume` CLI entry points.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from clade_cli import pause as pause_mod


def _load_daemon_with_config(tmp_path: Path, cfg_dict: dict, pause_dir: Path):
    """Fresh import of agent.main + agent.daemon, with CLADE_PAUSE_DIR pointing
    at the test-specific sentinel directory."""
    import yaml as _yaml
    cfg_path = tmp_path / "peer.yaml"
    cfg_path.write_text(_yaml.dump(cfg_dict))
    os.environ["CLADE_CONFIG"] = str(cfg_path)
    os.environ["CLADE_PAUSE_DIR"] = str(pause_dir)
    for mod in ("agent.daemon", "agent.main"):
        sys.modules.pop(mod, None)
    import agent.daemon as daemon  # noqa: PLC0415
    return daemon


def _basic_cfg(my_id: str, audit_db: str, docs_repo_path: str | None = None) -> dict:
    scribe: dict = {"enabled": True, "interval_minutes": 60, "max_rounds_per_day": 24}
    if docs_repo_path:
        scribe["docs_repo_path"] = docs_repo_path
    return {
        "my_id": my_id,
        "bearer_token": "tok",
        "peers": {},
        "audit_db": audit_db,
        "extra_add_dirs": [],
        "scribe": scribe,
    }


def _init_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
    (repo / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()


# ---- _scribe_paused helper ----

def test_paused_returns_false_when_no_sentinel(tmp_path: Path):
    pause_dir = tmp_path / "clade-home"
    daemon = _load_daemon_with_config(tmp_path, _basic_cfg("doc", str(tmp_path / "a.db")), pause_dir)
    from agent.main import cfg
    paused, reason = daemon._scribe_paused(cfg)
    assert paused is False
    assert reason == ""


def test_paused_true_when_global_sentinel(tmp_path: Path):
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    daemon = _load_daemon_with_config(tmp_path, _basic_cfg("doc", str(tmp_path / "a.db")), pause_dir)
    from agent.main import cfg
    paused, reason = daemon._scribe_paused(cfg)
    assert paused is True
    assert "global sentinel" in reason
    assert str(pause_dir / "pause") in reason


def test_paused_true_when_per_peer_sentinel(tmp_path: Path):
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    (pause_dir / "doc-pause").touch()
    daemon = _load_daemon_with_config(tmp_path, _basic_cfg("doc", str(tmp_path / "a.db")), pause_dir)
    from agent.main import cfg
    paused, reason = daemon._scribe_paused(cfg)
    assert paused is True
    assert "per-peer sentinel" in reason
    assert "doc-pause" in reason


def test_paused_other_peer_sentinel_does_not_affect_me(tmp_path: Path):
    """A `pd-pause` sentinel must NOT pause the `doc` peer."""
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    (pause_dir / "pd-pause").touch()
    daemon = _load_daemon_with_config(tmp_path, _basic_cfg("doc", str(tmp_path / "a.db")), pause_dir)
    from agent.main import cfg
    paused, _ = daemon._scribe_paused(cfg)
    assert paused is False


def test_per_peer_sentinel_takes_priority_over_global(tmp_path: Path):
    """Per-peer file is reported first (specific reason). Either pauses the peer."""
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    (pause_dir / "doc-pause").touch()
    daemon = _load_daemon_with_config(tmp_path, _basic_cfg("doc", str(tmp_path / "a.db")), pause_dir)
    from agent.main import cfg
    paused, reason = daemon._scribe_paused(cfg)
    assert paused is True
    assert "doc-pause" in reason  # per-peer wins the reason


# ---- _scribe_tick integration ----

@pytest.mark.asyncio
async def test_tick_skips_when_paused_without_touching_git(tmp_path: Path, monkeypatch):
    """When paused, _scribe_tick must NOT call _scribe_git_head or spawn claude.
    Verified by replacing both with raisers that would fail loudly if called."""
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(
        tmp_path, _basic_cfg("doc", str(tmp_path / "a.db"), str(repo)), pause_dir)
    from agent.main import cfg

    async def _explode(*a, **k):
        raise AssertionError("git/claude was called while paused")
    monkeypatch.setattr(daemon, "_scribe_git_head", _explode)
    monkeypatch.setattr(daemon, "_call_claude_scribe", _explode)

    state = {"last_head_sha": None, "rounds_today": []}
    spawned = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert spawned is False
    assert state["_paused_logged"] is True  # logged the pause


@pytest.mark.asyncio
async def test_tick_resumes_after_sentinel_removed(tmp_path: Path, monkeypatch):
    """After pause then resume, the next tick should spawn normally and log resume."""
    pause_dir = tmp_path / "clade-home"
    pause_dir.mkdir()
    sentinel = pause_dir / "pause"
    sentinel.touch()

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    daemon = _load_daemon_with_config(
        tmp_path, _basic_cfg("doc", str(tmp_path / "a.db"), str(repo)), pause_dir)
    from agent.main import cfg

    spawned_calls: list[bool] = []
    async def _fake_claude(prompt, workdir, cfg_, dangerous):
        spawned_calls.append(True)
        return "ok"
    monkeypatch.setattr(daemon, "_call_claude_scribe", _fake_claude)

    state = {"last_head_sha": None, "rounds_today": []}
    # Tick #1: paused → cheap exit, no spawn
    s1 = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert s1 is False
    assert spawned_calls == []
    assert state["_paused_logged"] is True

    # Remove sentinel → tick #2 should spawn
    sentinel.unlink()
    s2 = await daemon._scribe_tick(cfg, tmp_path / "wd", False, state)
    assert s2 is True
    assert spawned_calls == [True]
    assert state["_paused_logged"] is False  # reset after resume


# ---- CLI ----

def test_cli_pause_creates_global_sentinel(tmp_path: Path, monkeypatch, capsys):
    pause_dir = tmp_path / "home"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    monkeypatch.setattr(sys, "argv", ["clade-pause"])
    rc = pause_mod.pause_main()
    assert rc == 0
    assert (pause_dir / "pause").exists()
    captured = capsys.readouterr().out
    assert "GLOBAL" in captured


def test_cli_pause_creates_per_peer_sentinel(tmp_path: Path, monkeypatch, capsys):
    pause_dir = tmp_path / "home"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    monkeypatch.setattr(sys, "argv", ["clade-pause", "--peer", "team"])
    rc = pause_mod.pause_main()
    assert rc == 0
    assert (pause_dir / "team-pause").exists()
    assert not (pause_dir / "pause").exists()
    assert "team" in capsys.readouterr().out


def test_cli_resume_removes_sentinel(tmp_path: Path, monkeypatch, capsys):
    pause_dir = tmp_path / "home"
    pause_dir.mkdir()
    (pause_dir / "pause").touch()
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    monkeypatch.setattr(sys, "argv", ["clade-resume"])
    rc = pause_mod.resume_main()
    assert rc == 0
    assert not (pause_dir / "pause").exists()
    assert "removed" in capsys.readouterr().out


def test_cli_resume_idempotent_when_no_sentinel(tmp_path: Path, monkeypatch, capsys):
    pause_dir = tmp_path / "home"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    monkeypatch.setattr(sys, "argv", ["clade-resume", "--peer", "ghost"])
    rc = pause_mod.resume_main()
    assert rc == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_cli_pause_is_idempotent(tmp_path: Path, monkeypatch):
    pause_dir = tmp_path / "home"
    monkeypatch.setenv("CLADE_PAUSE_DIR", str(pause_dir))
    monkeypatch.setattr(sys, "argv", ["clade-pause"])
    assert pause_mod.pause_main() == 0
    assert pause_mod.pause_main() == 0  # second call must not error
    assert (pause_dir / "pause").exists()

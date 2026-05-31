"""v1.16.0 (Phase D) — kill-switch CLI for the scribe self-driving loop.

`clade-pause` and `clade-resume` manage sentinel files in `$CLADE_PAUSE_DIR`
(default `~/.clade/`). Daemons check these before each scribe tick:

- `pause` (no flag) → file at `<dir>/pause` → ALL peers on this host skip scribe
- `pause --peer X` → file at `<dir>/X-pause` → only peer X skips scribe

`resume` removes the matching sentinel. Daemons log the resume on the next
tick. Idempotent — re-running `pause` or `resume` is safe.

Does NOT stop existing in-flight `claude --print` spawns — only prevents the
next tick from spawning a new one. To kill an in-flight spawn, send the
daemon SIGTERM (or use the OS to kill the claude child process).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _sentinel_dir() -> Path:
    override = os.environ.get("CLADE_PAUSE_DIR")
    return Path(override).expanduser() if override else (Path.home() / ".clade")


def _sentinel_path(peer: str | None) -> Path:
    d = _sentinel_dir()
    if peer:
        return d / f"{peer}-pause"
    return d / "pause"


def _list_existing() -> list[Path]:
    d = _sentinel_dir()
    if not d.exists():
        return []
    return sorted([p for p in d.iterdir() if p.name == "pause" or p.name.endswith("-pause")])


def _print_state() -> None:
    existing = _list_existing()
    if not existing:
        print(f"  no sentinels in {_sentinel_dir()}")
    else:
        print(f"  active sentinels in {_sentinel_dir()}:")
        for p in existing:
            scope = "GLOBAL (all peers)" if p.name == "pause" else f"peer '{p.name[:-len('-pause')]}'"
            print(f"    {p.name} → {scope}")


def _pause(peer: str | None) -> int:
    p = _sentinel_path(peer)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    scope = "GLOBAL (all peers on this host)" if peer is None else f"peer '{peer}'"
    print(f"clade-pause: sentinel created at {p} → {scope}")
    print("daemons will log the pause on the next scribe tick (within interval_minutes).")
    print()
    print("Current state:")
    _print_state()
    return 0


def _resume(peer: str | None) -> int:
    p = _sentinel_path(peer)
    if not p.exists():
        scope = "GLOBAL" if peer is None else f"peer '{peer}'"
        print(f"clade-resume: no {scope} sentinel at {p} (nothing to remove)")
    else:
        p.unlink()
        scope = "GLOBAL (all peers)" if peer is None else f"peer '{peer}'"
        print(f"clade-resume: removed {p} → {scope}")
        print("daemons will log the resume on the next scribe tick.")
    print()
    print("Current state:")
    _print_state()
    return 0


def pause_main() -> int:
    ap = argparse.ArgumentParser(
        prog="clade-pause",
        description="Pause scribe self-driving loops via sentinel file. "
                    "Without --peer, pauses ALL peers on this host.",
    )
    ap.add_argument("--peer", help="Pause only this peer_id (per-peer sentinel)")
    args = ap.parse_args()
    return _pause(args.peer)


def resume_main() -> int:
    ap = argparse.ArgumentParser(
        prog="clade-resume",
        description="Remove a scribe pause sentinel. Without --peer, removes the global one.",
    )
    ap.add_argument("--peer", help="Resume only this peer_id (per-peer sentinel)")
    args = ap.parse_args()
    return _resume(args.peer)


if __name__ == "__main__":
    # Default to pause when invoked as a module; resume needs the explicit entry point.
    sys.exit(pause_main())

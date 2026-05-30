"""clade-add-peer — thin CLI wrapper around clade_cli.peer_ops.add_peer_op.

Usage:
    clade-add-peer <peer_id> "<display_name>" "<role>" [--team NAME]...

Example:
    clade-add-peer designer "Mira - UX designer" \\
        "You are Mira, UX designer. Specialty: Figma, design systems." \\
        --team everyone --team engineering
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from clade_cli.peer_ops import _find_active_project, add_peer_op


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clade-add-peer",
        description="Add a new peer to an existing setup-server project (surgical, no daemon restarts).",
    )
    parser.add_argument("peer_id", help="Slug like 'designer'; must start with a letter, alnum/dash/underscore only")
    parser.add_argument("display_name", help="Display name shown to other agents")
    parser.add_argument("role", help="Role prompt — what this agent does")
    parser.add_argument("--team", action="append", default=[],
                        help="Add this peer to a team (can be repeated); team is auto-created if missing")
    parser.add_argument("--extra-add-dir", action="append", default=[],
                        help="Path the daemon-spawned Claude can read outside its workdir (repeatable)")
    parser.add_argument("--agent-dir", default=os.path.expanduser("~/clade-agent"),
                        help="Local dir where peer yamls + scripts live (default: ~/clade-agent)")
    parser.add_argument("--setup-data-dir",
                        default=os.environ.get("CLADE_SETUP_DATA_DIR",
                                               os.path.expanduser("~/.clade/setup-server")),
                        help="setup-server data dir (default: ~/.clade/setup-server)")
    parser.add_argument("--setup-server-url", default="http://127.0.0.1:8000",
                        help="setup-server URL for /admin/reload (default: http://127.0.0.1:8000)")
    parser.add_argument("--no-reload", action="store_true",
                        help="Skip reloading relay + setup-server (useful when they aren't running)")
    args = parser.parse_args()

    try:
        project_dir = _find_active_project(Path(args.setup_data_dir))
    except (FileNotFoundError, RuntimeError) as e:
        sys.exit(f"Error: {e}")

    agent_dir = Path(args.agent_dir).expanduser()

    try:
        import json as _json
        relay_url = _json.loads((project_dir / "setup.json").read_text()).get("relay_url")
        result = add_peer_op(
            project_dir=project_dir,
            agent_dir=agent_dir,
            peer_id=args.peer_id,
            display_name=args.display_name,
            role=args.role,
            teams=args.team,
            extra_add_dirs=args.extra_add_dir,
            relay_url_for_reload=None if args.no_reload else relay_url,
            setup_server_url_for_reload=None if args.no_reload else args.setup_server_url,
        )
    except ValueError as e:
        sys.exit(f"Error: {e}")

    print(f"Adding peer '{result['peer_id']}' to setup ({project_dir.name})")
    print(f"  download_token: {result['download_token']}")
    if result.get("added_to_teams"):
        print(f"  added to teams: {result['added_to_teams']}")
    if isinstance(result.get("relay_reload"), dict) and result["relay_reload"].get("status_code") == 200:
        print(f"  relay reloaded: {result['relay_reload']['body'].get('added')} added")
    elif result.get("relay_reload", {}).get("skipped"):
        pass
    else:
        print(f"  WARN: relay reload: {result['relay_reload']}", file=sys.stderr)
    if isinstance(result.get("setup_server_reload"), dict) and result["setup_server_reload"].get("status_code") == 200:
        print(f"  setup-server reloaded")
    elif result.get("setup_server_reload", {}).get("skipped"):
        pass

    print()
    print("==================================================")
    print(f"Peer '{result['peer_id']}' added.")
    print("==================================================")
    print()
    print(f"Start the new daemon (new terminal):")
    print(f"  {result['next_step']}")
    print()
    print("Existing daemons hot-reload the peer allowlist within ~5s (v1.7.0+ config_watcher_loop).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

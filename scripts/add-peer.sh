#!/bin/bash
# Thin wrapper around the clade-add-peer Python CLI.
#
# Usage:
#   ./scripts/add-peer.sh <peer_id> "<display_name>" "<role>" [--team NAME]...
#
# Example:
#   ./scripts/add-peer.sh designer "Mira - UX designer" \
#       "You are Mira, UX designer. Specialty: Figma, design systems." \
#       --team everyone
#
# Adds a new peer to the active setup-server project: generates HMAC secrets,
# updates every existing peer's yaml (auto-reloaded by their daemons), writes
# the new peer's yaml + scripts + workdir, and signals the relay + setup-server
# to refresh their in-memory state. Only the new peer's daemon needs to be
# started manually — existing daemons keep running.

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Error: venv not found at $ROOT/.venv" >&2
  exit 1
fi

exec "$PY" -m clade_cli.add_peer "$@"

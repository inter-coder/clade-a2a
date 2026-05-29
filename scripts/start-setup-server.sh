#!/bin/bash
# Start Clade A2A Setup Server (v1.4.0+).
#
# Opens the web form for configuring peers + (optionally) starts the relay.
# Other peers curl-pull their unique install scripts from this server.
#
# Defaults:
#   - Bind:  0.0.0.0:8000 (LAN-reachable)
#   - Data:  ~/.clade/setup-server/
#
# Override via env vars or args:
#   CLADE_SETUP_PORT=9000 ./scripts/start-setup-server.sh
#   ./scripts/start-setup-server.sh --port 9000 --data-dir /tmp/clade
#
# Stop with Ctrl+C. Generated configs persist in --data-dir.

set -e

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Error: venv not found at $ROOT/.venv" >&2
  echo "Setup first:" >&2
  echo "  cd $ROOT && uv venv && uv pip install -e ." >&2
  exit 1
fi

# Detect LAN IP for user-facing hint (best effort)
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="${CLADE_SETUP_PORT:-8000}"

echo ""
echo "Clade A2A Setup Server starting..."
echo ""
if [[ -n "$LAN_IP" ]]; then
  echo "  Open in browser:"
  echo "    http://$LAN_IP:$PORT/    (LAN — share with peer machines)"
  echo "    http://127.0.0.1:$PORT/  (this machine)"
else
  echo "  Open in browser: http://127.0.0.1:$PORT/"
fi
echo ""
echo "  Stop: Ctrl+C"
echo ""

exec "$PY" -m clade_cli.setup_server --port "$PORT" "$@"

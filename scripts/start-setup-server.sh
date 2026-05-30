#!/bin/bash
# Start Clade A2A Setup Server (v1.4.2+).
#
# Cisto pokretanje: skripta uvek prvo ugasi sve sto je ostalo od prosle sesije
# (setup-server + relay subprocess-i) i obrise data-dir, pa onda krene fresh.
# Ovo iz prosle sesije znaci da nema vise "Invalid bearer token" sranja od
# starih tokens.json fajlova koji su preziveli restart.
#
# Sta se gasi:
#   - clade_cli.setup_server (sam setup-server)
#   - relay.main / clade-relay (relay subprocess-i koje je setup-server spawn-ovao)
#
# Sta se brise:
#   - ${CLADE_SETUP_DATA_DIR:-~/.clade/setup-server}/*
#
# Defaults:
#   - Bind:  0.0.0.0:8000 (LAN-reachable)
#   - Data:  ~/.clade/setup-server/
#
# Override:
#   CLADE_SETUP_PORT=9000 ./scripts/start-setup-server.sh
#   CLADE_SETUP_DATA_DIR=/tmp/clade ./scripts/start-setup-server.sh
#   ./scripts/start-setup-server.sh --port 9000 --data-dir /tmp/clade
#
# Skip reset (drzi stari state — nema poenta osim za debugging):
#   CLADE_NO_RESET=1 ./scripts/start-setup-server.sh
#
# Stop with Ctrl+C. Generated configs ostaju u --data-dir do sledeceg pokretanja.

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

# --- Parse data-dir iz argv (ako je dat) ili koristi env/default
DATA_DIR="${CLADE_SETUP_DATA_DIR:-$HOME/.clade/setup-server}"
PORT="${CLADE_SETUP_PORT:-8000}"
prev=""
for a in "$@"; do
  if [[ "$prev" == "--data-dir" ]]; then DATA_DIR="$a"; fi
  if [[ "$prev" == "--port" ]]; then PORT="$a"; fi
  prev="$a"
done
DATA_DIR="${DATA_DIR/#\~/$HOME}"  # expand ~ rucno

# --- Reset (osim ako user eksplicitno trazi da preskoci)
if [[ -z "$CLADE_NO_RESET" ]]; then
  echo "Reset: gasim stare procese i brisem $DATA_DIR ..."

  # Pattern-i pokrivaju sve nacine na koje se setup-server ili relay startuje.
  # SIGTERM prvo, pa proverimo + SIGKILL ako neko ne ode lepo.
  KILL_PATTERNS=(
    "clade_cli.setup_server"
    "clade-setup-server"
    "import relay.main"
    "clade-relay"
    "relay.main:app"
  )

  for pat in "${KILL_PATTERNS[@]}"; do
    pkill -TERM -f "$pat" 2>/dev/null || true
  done
  sleep 1
  for pat in "${KILL_PATTERNS[@]}"; do
    if pgrep -f "$pat" > /dev/null 2>&1; then
      pkill -KILL -f "$pat" 2>/dev/null || true
    fi
  done

  # Wipe data-dir (samo sadrzaj, ne i sam direktorijum — da chmod/owner ostane)
  if [[ -d "$DATA_DIR" ]]; then
    rm -rf "$DATA_DIR"/* "$DATA_DIR"/.[!.]* 2>/dev/null || true
  fi

  echo "Reset zavrsen."
  echo ""
fi

# Detect LAN IP for user-facing hint (best effort)
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

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

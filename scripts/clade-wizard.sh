#!/bin/bash
# clade-wizard.sh — interaktivni setup wizard za novi Clade A2A projekat.
#
# Sta radi:
#   1. Pita za ime projekta + peer-ove + relay host/port
#   2. Pokrene clade-init da generise sve config-e + ključeve
#   3. Pokrene relay u pozadini (sa health check-om)
#   4. Generise start-<peer>.sh skripte za svakog peer-a — jedna komanda
#      po peer-u da otvoris Claude sa pravim config-om
#   5. Generise stop.sh / status.sh / restart.sh za upravljanje

set -e

# ---- Boja ----
BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
RESET='\033[0m'

info()  { echo -e "${CYAN}$*${RESET}"; }
ok()    { echo -e "${GREEN}✓${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*"; }
err()   { echo -e "${RED}✗${RESET} $*"; }
ask()   { read -rp "$(echo -e "${BOLD}$1${RESET} ")" "${2}"; }

# ---- Locate repo root + python ----
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  err "Python venv ne postoji u $ROOT/.venv"
  echo "  Run setup prvo:"
  echo "    cd $ROOT"
  echo "    uv venv && uv pip install -e ."
  exit 1
fi

clear
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  Clade A2A — Setup Wizard${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo "Pravimo novi A2A projekat. Pitam te par stvari, pa konfigurišem"
echo "sve sam — na kraju ostaje samo da otvoriš Claude za svakog peer-a."
echo ""

# ---- Korak 1: Ime projekta ----
DEFAULT_PROJECT_NAME="my-clade-$(date +%Y%m%d-%H%M)"
ask "Ime projekta [${DEFAULT_PROJECT_NAME}]:" PROJECT_NAME
PROJECT_NAME=${PROJECT_NAME:-$DEFAULT_PROJECT_NAME}

DEFAULT_PROJECT_DIR="$HOME/clade-projects/$PROJECT_NAME"
ask "Gde da snimim configs [${DEFAULT_PROJECT_DIR}]:" PROJECT_DIR
PROJECT_DIR=${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}

if [[ -d "$PROJECT_DIR" ]] && [[ "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]]; then
  warn "$PROJECT_DIR vec postoji i nije prazan."
  ask "Obrisati i krenuti ispocetka? [N/y]:" OVERWRITE
  if [[ "${OVERWRITE,,}" == "y" ]]; then
    rm -rf "$PROJECT_DIR"
  else
    err "Prekidam — biraj drugu lokaciju."
    exit 1
  fi
fi
mkdir -p "$PROJECT_DIR"

# ---- Korak 2: Peer-ovi ----
echo ""
info "Peer agenti — koji ce komunicirati medjusobno."
echo "Tipicna imena: frontend/backend, alice/bob, dusan/predrag, sds/katana itd."
echo ""

ask "Koliko peer agenata? [2]:" N_PEERS
N_PEERS=${N_PEERS:-2}

if ! [[ "$N_PEERS" =~ ^[2-9]$|^1[0-9]$ ]]; then
  err "Mora biti broj izmedju 2 i 19."
  exit 1
fi

PEERS=()
SUGGESTIONS=("frontend" "katana" "alice" "bob" "carol" "dave" "eve" "frank" "grace" "henry")
for ((i=0; i<N_PEERS; i++)); do
  DEFAULT="${SUGGESTIONS[$i]:-peer$((i+1))}"
  ask "  Peer $((i+1)) ime [${DEFAULT}]:" PEER_NAME
  PEER_NAME=${PEER_NAME:-$DEFAULT}

  # Validacija: alfanumerik + dash/underscore, prvo slovo
  if ! [[ "$PEER_NAME" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]]; then
    err "Ime '$PEER_NAME' nije validno. Mora poceti slovom, ostalo alfanumerik/dash/underscore."
    exit 1
  fi

  # Duplikat check
  for existing in "${PEERS[@]}"; do
    if [[ "$existing" == "$PEER_NAME" ]]; then
      err "Ime '$PEER_NAME' vec dodato. Mora biti unique."
      exit 1
    fi
  done

  PEERS+=("$PEER_NAME")
done

# ---- Korak 3: Relay endpoint ----
echo ""
info "Relay konfiguracija."
echo "  127.0.0.1 = samo lokalna masina"
echo "  0.0.0.0   = svi interface-i (LAN/VPN dostupno)"
echo "  <ip>      = specifican interface (npr. 10.0.0.5)"
echo ""

DEFAULT_HOST="127.0.0.1"
ask "Relay listen host [${DEFAULT_HOST}]:" RELAY_HOST
RELAY_HOST=${RELAY_HOST:-$DEFAULT_HOST}

DEFAULT_PORT="7777"
ask "Relay port [${DEFAULT_PORT}]:" RELAY_PORT
RELAY_PORT=${RELAY_PORT:-$DEFAULT_PORT}

# Compute URL koji ide u config-e peer-ova
if [[ "$RELAY_HOST" == "0.0.0.0" ]]; then
  RELAY_URL_HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [[ -z "$RELAY_URL_HOST" ]]; then
    warn "Ne mogu da detektujem LAN IP — koristim 127.0.0.1 u config-ima."
    RELAY_URL_HOST="127.0.0.1"
  else
    info "Slušam na svim interface-ima. Peer-ovi ce koristiti: ${RELAY_URL_HOST}:${RELAY_PORT}"
  fi
elif [[ "$RELAY_HOST" == "127.0.0.1" ]] || [[ "$RELAY_HOST" == "localhost" ]]; then
  RELAY_URL_HOST="127.0.0.1"
else
  RELAY_URL_HOST="$RELAY_HOST"
fi
RELAY_URL="http://${RELAY_URL_HOST}:${RELAY_PORT}"

# ---- Korak 4: Confirm ----
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Sumarno:${RESET}"
echo "  Projekat:    $PROJECT_NAME"
echo "  Lokacija:    $PROJECT_DIR"
echo "  Peers:       ${PEERS[*]}"
echo "  Relay:       $RELAY_HOST:$RELAY_PORT  (URL: $RELAY_URL)"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
ask "Krećemo? [Y/n]:" CONFIRM
if [[ "${CONFIRM,,}" == "n" ]]; then
  echo "Prekidam."
  exit 0
fi

# ---- Korak 5: Bootstrap kroz clade-init ----
echo ""
info "Generišem config-e..."
"$PY" -m clade_cli.init --peers "${PEERS[@]}" --output "$PROJECT_DIR" --relay-url "$RELAY_URL" --agent-python "$PY"

# ---- Korak 6: Pokreni relay ----
echo ""
info "Pokrećem relay..."

LOG="$PROJECT_DIR/relay.log"
PIDFILE="$PROJECT_DIR/relay.pid"

# Kill bilo koji vec pokrenut relay na tom portu
if curl -sf --max-time 1 "http://127.0.0.1:${RELAY_PORT}/health" >/dev/null 2>&1; then
  warn "Vec postoji relay na ${RELAY_PORT}. Gasim ga..."
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
  fi
  pkill -f "uvicorn.*${RELAY_PORT}" 2>/dev/null || true
  sleep 2
fi

setsid nohup "$PY" -c "
import sys, pathlib, uvicorn
sys.path.insert(0, '$ROOT')
import relay.main
relay.main.TOKENS_PATH = pathlib.Path('$PROJECT_DIR/tokens.json')
uvicorn.run(relay.main.app, host='$RELAY_HOST', port=$RELAY_PORT, log_level='info')
" > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
disown

# Wait za health
DEADLINE=$((SECONDS + 10))
while (( SECONDS < DEADLINE )); do
  if curl -sf --max-time 1 "http://127.0.0.1:${RELAY_PORT}/health" >/dev/null 2>&1; then
    ok "Relay tece — PID $(cat "$PIDFILE")"
    break
  fi
  sleep 0.5
done

if ! curl -sf --max-time 1 "http://127.0.0.1:${RELAY_PORT}/health" >/dev/null 2>&1; then
  err "Relay nije starovao posle 10s. Log:"
  cat "$LOG"
  exit 1
fi

# ---- Korak 7: Generiši per-peer start skripte + helpere ----

# start-<peer>.sh — otvara Claude za peer-a u svom radnom dir-u
for peer in "${PEERS[@]}"; do
  PEER_WORKDIR="$PROJECT_DIR/workdirs/$peer"
  mkdir -p "$PEER_WORKDIR"
  cp "$PROJECT_DIR/mcp-config-$peer.json" "$PEER_WORKDIR/.mcp.json"
  cp "$PROJECT_DIR/CLAUDE.md" "$PEER_WORKDIR/CLAUDE.md"

  cat > "$PROJECT_DIR/start-$peer.sh" <<EOF
#!/bin/bash
# Otvara Claude Code za peer "$peer" u njegovom workdir-u.
# Generisano sa clade-wizard.sh, $(date)
exec env CLADE_CONFIG="$PROJECT_DIR/$peer.yaml" claude --add-dir "$PEER_WORKDIR"
EOF
  chmod +x "$PROJECT_DIR/start-$peer.sh"
done

# stop.sh — gasi relay
cat > "$PROJECT_DIR/stop.sh" <<EOF
#!/bin/bash
# Gasi Clade relay za projekat $PROJECT_NAME.
if [[ -f "$PIDFILE" ]]; then
  PID=\$(cat "$PIDFILE")
  if kill "\$PID" 2>/dev/null; then
    echo "Relay PID \$PID ugašen."
  else
    echo "Relay PID \$PID nije aktivan (mozda vec ugasen)."
  fi
  rm -f "$PIDFILE"
else
  echo "Nema PID fajla — relay verovatno nije pokrenut kroz wizard."
fi
EOF
chmod +x "$PROJECT_DIR/stop.sh"

# status.sh — health + audit summary
cat > "$PROJECT_DIR/status.sh" <<EOF
#!/bin/bash
# Status check za Clade relay projekta $PROJECT_NAME.
echo "=== Relay health ==="
if curl -sf --max-time 2 "$RELAY_URL/health" 2>/dev/null | $PY -m json.tool; then
  echo ""
  echo "Audit UI: $RELAY_URL/ui/audit"
else
  echo "Relay NE radi na $RELAY_URL"
  if [[ -f "$PIDFILE" ]]; then
    echo "Stari PID: \$(cat "$PIDFILE")"
  fi
  echo "Pokreni: $PROJECT_DIR/start-relay.sh"
fi
EOF
chmod +x "$PROJECT_DIR/status.sh"

# start-relay.sh — za ponovno pokretanje posle stop-a
cat > "$PROJECT_DIR/start-relay.sh" <<EOF
#!/bin/bash
# Pokrece relay za projekat $PROJECT_NAME.
LOG="$LOG"
PIDFILE="$PIDFILE"
if [[ -f "\$PIDFILE" ]] && kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null; then
  echo "Relay vec radi (PID \$(cat "\$PIDFILE"))."
  exit 0
fi
setsid nohup "$PY" -c "
import sys, pathlib, uvicorn
sys.path.insert(0, '$ROOT')
import relay.main
relay.main.TOKENS_PATH = pathlib.Path('$PROJECT_DIR/tokens.json')
uvicorn.run(relay.main.app, host='$RELAY_HOST', port=$RELAY_PORT, log_level='info')
" > "\$LOG" 2>&1 < /dev/null &
echo \$! > "\$PIDFILE"
disown
sleep 2
if curl -sf --max-time 2 "http://127.0.0.1:${RELAY_PORT}/health" >/dev/null; then
  echo "Relay pokrenut (PID \$(cat "\$PIDFILE"))."
else
  echo "Nije moglo da pokrene. Log:"
  cat "\$LOG"
fi
EOF
chmod +x "$PROJECT_DIR/start-relay.sh"

# ---- Korak 8: Finalni izlaz ----
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  SETUP GOTOV${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "${BOLD}Sve je u: ${CYAN}$PROJECT_DIR${RESET}"
echo ""
echo -e "${BOLD}Sledeci koraci:${RESET}"
echo ""
echo -e "  ${CYAN}1.${RESET} Otvori N terminala (po jedan po peer-u) i pokreni:"
for peer in "${PEERS[@]}"; do
  echo -e "       ${YELLOW}$PROJECT_DIR/start-$peer.sh${RESET}"
done
echo ""
echo -e "  ${CYAN}2.${RESET} U svakoj Claude sesiji moze odmah da pita drugog peer-a:"
echo -e "       ${YELLOW}\"Pitaj <peer> koliko je 7×8 preko clade_ask, timeout 90s.\"${RESET}"
echo ""
echo -e "  ${CYAN}3.${RESET} Audit log:  ${YELLOW}${RELAY_URL}/ui/audit${RESET}"
echo "       (paste bilo koji bearer token iz $PROJECT_DIR/tokens.json)"
echo ""
echo -e "${BOLD}Upravljanje:${RESET}"
echo "  Status:   $PROJECT_DIR/status.sh"
echo "  Stop:     $PROJECT_DIR/stop.sh"
echo "  Restart:  $PROJECT_DIR/stop.sh && $PROJECT_DIR/start-relay.sh"
echo "  Logs:     tail -f $LOG"
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"

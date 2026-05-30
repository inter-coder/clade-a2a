#!/bin/bash
# Start Clade A2A Setup Server (v1.4.2+, full reset v1.10.0+).
#
# Cisto pokretanje: skripta uvek prvo ugasi SVE sto je ostalo od prethodnih
# sesija pa onda krene fresh. Cilj je da posle pokretanja ne moze biti
# konflikta sa zaostalim procesima ili fajlovima.
#
# Sta se gasi (procesi):
#   - clade_cli.setup_server (sam setup-server)
#   - relay.main / clade-relay (relay subprocess-i)
#   - agent.daemon (peer daemon-i ako su pokrenuti na ovoj masini)
#
# Sta se brise (fajlovi):
#   - ${CLADE_SETUP_DATA_DIR:-~/.clade/setup-server}/*  (stari setup-i + tokens)
#   - ~/.clade/*-daemon.lock                            (stale lock fajlovi)
#   - ~/.clade/wd-*                                     (orphan daemon workdir-i)
#
# Sa --deep / CLADE_DEEP_RESET=1 dodatno:
#   - ~/.clade/*-audit.db    (audit + tasks + thread_history — gubi se istorija)
#   - ~/clade-agent/         (peer install dir: yaml-ovi, start.sh, chat.sh)
#
# Sa --quickstart auto-bootstrap virtuelne firme:
#   1. Pokrene setup-server (BG)
#   2. POST virtual-company template (CEO + frontend + backend + qa + engineering)
#   3. Lokalno install svakog peer-a (curl /install | bash)
#   4. Ispise sta dalje da uradis (start-<peer>.sh, chat-ceo.sh)
# Setup-server tece u foreground-u — Ctrl+C gasi sve. Daemon-e user pokrece
# zasebno (po dizajnu, da se ne mesa state izmedju peer-ova).
#
# Defaults:
#   - Bind:  0.0.0.0:8000 (LAN-reachable)
#   - Data:  ~/.clade/setup-server/
#
# Override:
#   CLADE_SETUP_PORT=9000 ./scripts/start-setup-server.sh
#   CLADE_SETUP_DATA_DIR=/tmp/clade ./scripts/start-setup-server.sh
#   ./scripts/start-setup-server.sh --port 9000 --data-dir /tmp/clade
#   ./scripts/start-setup-server.sh --deep            # extra cleanup audit+peers
#   ./scripts/start-setup-server.sh --quickstart      # auto VC bootstrap
#   ./scripts/start-setup-server.sh --quickstart --deep   # full reset + bootstrap
#
# Skip reset (drzi stari state — debug):
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

# --- Parse data-dir, port, --deep, --quickstart iz argv ili env/default
DATA_DIR="${CLADE_SETUP_DATA_DIR:-$HOME/.clade/setup-server}"
PORT="${CLADE_SETUP_PORT:-8000}"
DEEP_RESET="${CLADE_DEEP_RESET:-}"
QUICKSTART="${CLADE_QUICKSTART:-}"
# Posto --deep i --quickstart nisu setup-server options, izbacujemo ih iz argv
FILTERED_ARGS=()
prev=""
for a in "$@"; do
  if [[ "$prev" == "--data-dir" ]]; then DATA_DIR="$a"; fi
  if [[ "$prev" == "--port" ]]; then PORT="$a"; fi
  if [[ "$a" == "--deep" ]]; then
    DEEP_RESET=1
  elif [[ "$a" == "--quickstart" ]]; then
    QUICKSTART=1
  else
    FILTERED_ARGS+=("$a")
  fi
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

  # v1.4.5: prvo ubij relay-e preko PID fajlova (pouzdano, ne zavisi od pgrep
  # match-a Python -c argumenta). Tek onda fallback pkill -f patterns za
  # eventualne orphan-e.
  if [[ -d "$DATA_DIR" ]]; then
    for pf in "$DATA_DIR"/*/relay.pid; do
      [[ -f "$pf" ]] || continue
      rpid=$(cat "$pf" 2>/dev/null || echo "")
      if [[ -n "$rpid" ]] && kill -0 "$rpid" 2>/dev/null; then
        kill -TERM "$rpid" 2>/dev/null || true
      fi
    done
    sleep 1
    for pf in "$DATA_DIR"/*/relay.pid; do
      [[ -f "$pf" ]] || continue
      rpid=$(cat "$pf" 2>/dev/null || echo "")
      if [[ -n "$rpid" ]] && kill -0 "$rpid" 2>/dev/null; then
        kill -KILL "$rpid" 2>/dev/null || true
      fi
    done
  fi

  for pat in "${KILL_PATTERNS[@]}"; do
    pkill -TERM -f "$pat" 2>/dev/null || true
  done
  sleep 1
  for pat in "${KILL_PATTERNS[@]}"; do
    if pgrep -f "$pat" > /dev/null 2>&1; then
      pkill -KILL -f "$pat" 2>/dev/null || true
    fi
  done

  # v1.10.0: cisti i peer-side state na ovoj masini.
  # Bez ovog, stari daemon procesi nastavljaju da rade sa starim yaml-om i
  # daju "Invalid bearer token" greske kad setup-server izda nove tokene; ili
  # nov daemon ne moze da starta jer stari lock fajl jos drzi PID koji je sad
  # zauzet od drugog procesa (collision).
  # Daemon-e ubijamo BEZ obzira na DEEP_RESET — single-machine setup ih
  # uvek koristi, multi-machine setup ih na ovoj masini takodje pokrece za
  # CEO peer-a.
  DAEMON_PIDS=$(pgrep -f "agent\.daemon" 2>/dev/null | while read -r pid; do
    [[ -z "$pid" ]] && continue
    comm=$(ps -p "$pid" -o comm= 2>/dev/null || true)
    [[ "$comm" =~ ^python ]] && echo "$pid"
  done)
  if [[ -n "$DAEMON_PIDS" ]]; then
    echo "  ubijam agent.daemon procese: $DAEMON_PIDS"
    echo "$DAEMON_PIDS" | xargs -r kill -TERM 2>/dev/null || true
    sleep 1
    echo "$DAEMON_PIDS" | xargs -r kill -KILL 2>/dev/null || true
  fi

  # Lock + workdir orphan-i u ~/.clade/
  if [[ -d "$HOME/.clade" ]]; then
    locks=$(ls "$HOME/.clade"/*-daemon.lock 2>/dev/null || true)
    if [[ -n "$locks" ]]; then
      echo "  brisem lock fajlove: $(echo $locks | tr '\n' ' ')"
      rm -f "$HOME/.clade"/*-daemon.lock 2>/dev/null || true
    fi
    wds=$(ls -d "$HOME/.clade"/wd-* 2>/dev/null || true)
    if [[ -n "$wds" ]]; then
      echo "  brisem orphan workdir-e: $(echo $wds | tr '\n' ' ' | head -c 200)"
      rm -rf "$HOME/.clade"/wd-* 2>/dev/null || true
    fi
  fi

  # Wipe setup-server data-dir (samo sadrzaj, ne i sam direktorijum)
  if [[ -d "$DATA_DIR" ]]; then
    rm -rf "$DATA_DIR"/* "$DATA_DIR"/.[!.]* 2>/dev/null || true
  fi

  # v1.10.0: deep reset — dodatno brise audit DB-jeve i peer install dir.
  # Audit DB sadrzi istoriju, task-ove, thread_history — gubi se za reset OK,
  # ali korisnik mora eksplicitno da zatrazi.
  if [[ -n "$DEEP_RESET" ]]; then
    echo "  DEEP RESET: brisem audit DB-jeve + ~/clade-agent/"
    rm -f "$HOME/.clade"/*-audit.db 2>/dev/null || true
    rm -rf "$HOME/clade-agent" 2>/dev/null || true
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

# --- Spawn setup-server ---
# Ako --quickstart, treba nam BG da bismo posle radili instalaciju + onda
# fg/wait za server. Bez --quickstart, exec je dovoljan (zameni shell).
if [[ -z "$QUICKSTART" ]]; then
  exec "$PY" -m clade_cli.setup_server --port "$PORT" "${FILTERED_ARGS[@]}"
fi

# Quickstart path: BG server, sacekaj health, POST template, install + print
"$PY" -m clade_cli.setup_server --port "$PORT" "${FILTERED_ARGS[@]}" &
SS_PID=$!
# Ctrl+C ubija sve sto je quickstart spawnovao (setup-server + relay-e)
trap 'echo ""; echo "Stopping setup-server (PID $SS_PID)..."; kill -TERM $SS_PID 2>/dev/null; wait $SS_PID 2>/dev/null; exit 0' INT TERM

# Wait for health
SS_URL="http://127.0.0.1:$PORT"
echo ""
echo "Cekam setup-server da bude reachable na $SS_URL ..."
for i in $(seq 1 15); do
  if curl -sf --max-time 1 "$SS_URL/health" > /dev/null 2>&1; then
    echo "  setup-server up"
    break
  fi
  sleep 1
done

# POST virtual-company template
echo ""
echo "POST virtual-company template (CEO + frontend + backend + qa)..."
QS_RELAY_HOST="${CLADE_QS_RELAY_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
QS_RELAY_HOST="${QS_RELAY_HOST:-127.0.0.1}"
QS_RELAY_PORT="${CLADE_QS_RELAY_PORT:-7777}"
RESP=$(curl -sS -X POST "$SS_URL/api/setup" \
  -H 'content-type: application/json' \
  -d '{
    "project_name": "virtual-company",
    "relay_host": "0.0.0.0",
    "relay_url_host": "'"$QS_RELAY_HOST"'",
    "relay_port": '"$QS_RELAY_PORT"',
    "start_relay": true,
    "peers": [
      {"peer_id": "ceo",      "display_name": "CEO",                "role": "You are the CEO of the virtual company. You coordinate, delegate, broadcast. Before delegating, check who is online (clade_peers). Style: concise, decisive, prioritize. You do not implement directly — delegate via clade_task to specialists."},
      {"peer_id": "frontend", "display_name": "Ana — Frontend dev", "role": "You are Ana, frontend developer. Specialty: React 18, TypeScript, Tailwind. Style: short concrete answers, code snippet when relevant."},
      {"peer_id": "backend",  "display_name": "Bob — Backend dev",  "role": "You are Bob, backend developer. Specialty: Python, FastAPI, Postgres, Redis. Style: think about data flow + API contract before code."},
      {"peer_id": "qa",       "display_name": "Cveta — QA",         "role": "You are Cveta, QA engineer. Specialty: pytest, Playwright, manual testing, bug triaging. Style: think about edge cases dev did not cover."}
    ],
    "teams": {
      "engineering": ["frontend", "backend", "qa"],
      "everyone":    ["frontend", "backend", "qa"]
    }
  }' \
  -w "\n__HTTP=%{http_code}\n__LOC=%{redirect_url}\n")
PROJECT_TOKEN=$(echo "$RESP" | grep -oP 'setup/\K[A-Za-z0-9_-]+' | head -1)
if [[ -z "$PROJECT_TOKEN" ]]; then
  echo "ERROR: nisam dobio project_token. Server odgovor:"
  echo "$RESP" | tail -5
  kill -TERM $SS_PID 2>/dev/null
  exit 1
fi
echo "  generisan setup: $SS_URL/setup/$PROJECT_TOKEN"
sleep 2  # daj relay subprocess-u da bind-uje port

# Install svaki peer lokalno
echo ""
echo "Lokalni install za 4 peer-a..."
for peer in ceo frontend backend qa; do
  TOK=$(python3 -c "
import json
d = json.load(open('$DATA_DIR/$PROJECT_TOKEN/setup.json'))
for p in d['peers']:
    if p['peer_id'] == '$peer':
        print(p['download_token']); break
" 2>/dev/null)
  if [[ -z "$TOK" ]]; then
    echo "  WARN: nema download_token za $peer, preskacem"
    continue
  fi
  echo "  install $peer ..."
  if ! curl -fsSL "$SS_URL/agent/$TOK/install" 2>/dev/null | bash > "/tmp/clade-install-$peer.log" 2>&1; then
    echo "    FAILED — vidi /tmp/clade-install-$peer.log"
  else
    echo "    OK"
  fi
done

# Print sledeci korak
echo ""
echo "=================================================="
echo "Quickstart gotovo. Virtual company spremno."
echo "=================================================="
echo ""
echo "Pokreni daemon-e (3 employee terminala):"
echo "  $HOME/clade-agent/start-frontend.sh --yolo"
echo "  $HOME/clade-agent/start-backend.sh  --yolo"
echo "  $HOME/clade-agent/start-qa.sh       --yolo"
echo ""
echo "Otvori CEO interactive sesiju (cetvrti terminal):"
echo "  $HOME/clade-agent/chat-ceo.sh"
echo ""
echo "Browser dashboard (live presence):"
echo "  $SS_URL/setup/$PROJECT_TOKEN"
echo ""
echo "Setup-server tece u foreground-u. Ctrl+C gasi sve (server + relay subprocess)."
echo ""

# Drzimo foreground na setup-serveru. Trap iznad handle Ctrl+C.
wait $SS_PID

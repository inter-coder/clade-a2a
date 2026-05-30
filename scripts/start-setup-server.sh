#!/bin/bash
# Start Clade A2A Setup Server (v1.4.2+, full reset v1.10.0+).
#
# Clean launch: this script always first kills EVERYTHING left from previous
# sessions, then starts fresh. The goal is that after this script runs there
# can be no conflict with stale processes or files.
#
# What gets killed (processes):
#   - clade_cli.setup_server (the setup-server itself)
#   - relay.main / clade-relay (relay subprocesses)
#   - agent.daemon (peer daemons if running on this machine)
#
# What gets deleted (files):
#   - ${CLADE_SETUP_DATA_DIR:-~/.clade/setup-server}/*  (old setups + tokens)
#   - ~/.clade/*-daemon.lock                            (stale lock files)
#   - ~/.clade/wd-*                                     (orphan daemon workdirs)
#
# With --deep / CLADE_DEEP_RESET=1 additionally:
#   - ~/.clade/*-audit.db    (audit + tasks + thread_history — history is lost)
#   - ~/clade-agent/         (peer install dir: yamls, start.sh, chat.sh)
#
# With --quickstart auto-bootstrap a virtual company:
#   1. Start setup-server (BG)
#   2. POST virtual-company template (CEO + frontend + backend + qa + engineering)
#   3. Local install for each peer (curl /install | bash)
#   4. Print what to do next (start-<peer>.sh, chat-ceo.sh)
# Setup-server stays in the foreground — Ctrl+C kills everything. Daemons
# are started by the user manually (by design, to keep per-peer state clean).
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
# Skip reset (keep old state — debug only):
#   CLADE_NO_RESET=1 ./scripts/start-setup-server.sh
#
# Stop with Ctrl+C. Generated configs stay in --data-dir until the next launch.

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

# --- Parse data-dir, port, --deep, --quickstart from argv or env/defaults
DATA_DIR="${CLADE_SETUP_DATA_DIR:-$HOME/.clade/setup-server}"
PORT="${CLADE_SETUP_PORT:-8000}"
DEEP_RESET="${CLADE_DEEP_RESET:-}"
QUICKSTART="${CLADE_QUICKSTART:-}"
# --deep and --quickstart are not setup-server options, so strip them from argv
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
DATA_DIR="${DATA_DIR/#\~/$HOME}"  # expand ~ manually

# --- Reset (unless the user explicitly asks to skip)
if [[ -z "$CLADE_NO_RESET" ]]; then
  echo "Reset: killing old processes and wiping $DATA_DIR ..."

  # Patterns cover every way setup-server or relay can be started.
  # SIGTERM first, then check and SIGKILL anything that doesn't go gracefully.
  KILL_PATTERNS=(
    "clade_cli.setup_server"
    "clade-setup-server"
    "import relay.main"
    "clade-relay"
    "relay.main:app"
  )

  # v1.4.5: kill relays via PID files first (reliable, doesn't depend on pgrep
  # matching the Python -c argument). Then fall back to pkill -f patterns for
  # any leftover orphans.
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

  # v1.10.0: also clean peer-side state on this machine.
  # Without this, old daemon processes keep running with the old yaml and
  # give "Invalid bearer token" errors when setup-server issues new tokens;
  # or a new daemon can't start because the old lock file still holds a PID
  # that has been reused by another process (collision).
  # Daemons get killed regardless of DEEP_RESET — single-machine setups always
  # use them, multi-machine setups still run the CEO peer daemon here.
  DAEMON_PIDS=$(pgrep -f "agent\.daemon" 2>/dev/null | while read -r pid; do
    [[ -z "$pid" ]] && continue
    comm=$(ps -p "$pid" -o comm= 2>/dev/null || true)
    [[ "$comm" =~ ^python ]] && echo "$pid"
  done)
  if [[ -n "$DAEMON_PIDS" ]]; then
    echo "  killing agent.daemon processes: $DAEMON_PIDS"
    echo "$DAEMON_PIDS" | xargs -r kill -TERM 2>/dev/null || true
    sleep 1
    echo "$DAEMON_PIDS" | xargs -r kill -KILL 2>/dev/null || true
  fi

  # Lock + workdir orphans in ~/.clade/
  if [[ -d "$HOME/.clade" ]]; then
    locks=$(ls "$HOME/.clade"/*-daemon.lock 2>/dev/null || true)
    if [[ -n "$locks" ]]; then
      echo "  removing lock files: $(echo $locks | tr '\n' ' ')"
      rm -f "$HOME/.clade"/*-daemon.lock 2>/dev/null || true
    fi
    wds=$(ls -d "$HOME/.clade"/wd-* 2>/dev/null || true)
    if [[ -n "$wds" ]]; then
      echo "  removing orphan workdirs: $(echo $wds | tr '\n' ' ' | head -c 200)"
      rm -rf "$HOME/.clade"/wd-* 2>/dev/null || true
    fi
  fi

  # Wipe setup-server data-dir (contents only, keep the directory for chmod/owner)
  if [[ -d "$DATA_DIR" ]]; then
    rm -rf "$DATA_DIR"/* "$DATA_DIR"/.[!.]* 2>/dev/null || true
  fi

  # v1.10.0: deep reset — also wipe audit DBs and the peer install dir.
  # The audit DB holds history, tasks, thread_history — losing it is OK for
  # a reset, but the user must explicitly ask for it.
  if [[ -n "$DEEP_RESET" ]]; then
    echo "  DEEP RESET: removing audit DBs + ~/clade-agent/"
    rm -f "$HOME/.clade"/*-audit.db 2>/dev/null || true
    rm -rf "$HOME/clade-agent" 2>/dev/null || true
  fi

  echo "Reset done."
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
# With --quickstart we need a BG server so we can run the installs afterwards
# and then fg/wait on the server. Without --quickstart, exec is enough (replaces
# the shell).
if [[ -z "$QUICKSTART" ]]; then
  exec "$PY" -m clade_cli.setup_server --port "$PORT" "${FILTERED_ARGS[@]}"
fi

# Quickstart implies wiping ~/clade-agent/. Reason: without it, old yamls with
# old bearer tokens stick around, and if a daemon from the previous session is
# still running, it has the old bearer cached in memory (config is loaded only
# at startup). The result is a flood of 401 "Invalid bearer token" errors that
# are INVISIBLE in the quickstart log because the daemon is in another terminal.
# Plus an explicit warning at the end that daemons from the previous session
# must be stopped or restarted.
if [[ -d "$HOME/clade-agent" ]]; then
  echo "Quickstart: wiping ~/clade-agent (fresh yaml = fresh bearer = no 401 from cached state)"
  rm -rf "$HOME/clade-agent"
fi

# Quickstart path: BG server, wait for health, POST template, install + print
"$PY" -m clade_cli.setup_server --port "$PORT" "${FILTERED_ARGS[@]}" &
SS_PID=$!
# Ctrl+C kills everything quickstart spawned (setup-server + relays)
trap 'echo ""; echo "Stopping setup-server (PID $SS_PID)..."; kill -TERM $SS_PID 2>/dev/null; wait $SS_PID 2>/dev/null; exit 0' INT TERM

# Wait for health
SS_URL="http://127.0.0.1:$PORT"
echo ""
echo "Waiting for setup-server to be reachable at $SS_URL ..."
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
  echo "ERROR: no project_token returned. Server response:"
  echo "$RESP" | tail -5
  kill -TERM $SS_PID 2>/dev/null
  exit 1
fi
echo "  generated setup: $SS_URL/setup/$PROJECT_TOKEN"
sleep 2  # give the relay subprocess time to bind the port

# Install each peer locally
echo ""
echo "Local install for 4 peers..."
for peer in ceo frontend backend qa; do
  TOK=$(python3 -c "
import json
d = json.load(open('$DATA_DIR/$PROJECT_TOKEN/setup.json'))
for p in d['peers']:
    if p['peer_id'] == '$peer':
        print(p['download_token']); break
" 2>/dev/null)
  if [[ -z "$TOK" ]]; then
    echo "  WARN: no download_token for $peer, skipping"
    continue
  fi
  echo "  install $peer ..."
  if ! curl -fsSL "$SS_URL/agent/$TOK/install" 2>/dev/null | bash > "/tmp/clade-install-$peer.log" 2>&1; then
    echo "    FAILED — see /tmp/clade-install-$peer.log"
  else
    echo "    OK"
  fi
done

# Print next steps
echo ""
echo "=================================================="
echo "Quickstart done. Virtual company ready."
echo "=================================================="
echo ""
echo "IMPORTANT: if other terminals still have daemons running from a previous"
echo "session, they have a CACHED OLD BEARER → every request returns 401."
echo "Quickstart already killed all agent.daemon processes during reset, so any"
echo "of your daemons must be STOPPED or RESTARTED now. If you see 401 spam in"
echo "any terminal, that's the cause — Ctrl+C that daemon and start it again."
echo ""
echo "─────────────────────────────────────────────────────────────────"
echo "BEFORE starting daemons — optional: edit yamls if you need to:"
echo "─────────────────────────────────────────────────────────────────"
echo ""
echo "  ${EDITOR:-nano} $HOME/clade-agent/ceo.yaml          # role, name"
echo "  ${EDITOR:-nano} $HOME/clade-agent/frontend.yaml     # role, extra_add_dirs"
echo ""
echo "Most-requested field: extra_add_dirs (v1.10.2) — paths the daemon-spawned"
echo "Claude is allowed to read outside its workdir (e.g. project root, /var/www, ...)"
echo ""
echo "  # add to <peer>.yaml (top or bottom):"
echo "  extra_add_dirs:"
echo "    - $HOME/projects/foo"
echo "    - /var/www/myapp"
echo ""
echo "The daemon reads the yaml at startup, so edits take effect on the next start-<peer>.sh."
echo "─────────────────────────────────────────────────────────────────"
echo ""
echo "Start daemons (3 employee terminals — one per peer):"
echo "  $HOME/clade-agent/start-frontend.sh --yolo"
echo "  $HOME/clade-agent/start-backend.sh  --yolo"
echo "  $HOME/clade-agent/start-qa.sh       --yolo"
echo ""
echo "Open the CEO interactive session (fourth terminal):"
echo "  $HOME/clade-agent/chat-ceo.sh"
echo ""
echo "Browser dashboard (live presence):"
echo "  $SS_URL/setup/$PROJECT_TOKEN  (this machine)"
if [[ -n "$LAN_IP" ]] && [[ "$LAN_IP" != "127.0.0.1" ]]; then
  echo "  http://$LAN_IP:$PORT/setup/$PROJECT_TOKEN  (LAN — share with other machines)"
fi
echo ""
echo "Setup-server stays in the foreground. Ctrl+C kills everything (server + relay subprocess)."
echo ""

# Keep setup-server in the foreground. The trap above handles Ctrl+C.
wait $SS_PID

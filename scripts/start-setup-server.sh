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
# Defaults:
#   - Bind:  0.0.0.0:8000 (LAN-reachable)
#   - Data:  ~/.clade/setup-server/
#
# Override:
#   CLADE_SETUP_PORT=9000 ./scripts/start-setup-server.sh
#   CLADE_SETUP_DATA_DIR=/tmp/clade ./scripts/start-setup-server.sh
#   ./scripts/start-setup-server.sh --port 9000 --data-dir /tmp/clade
#   ./scripts/start-setup-server.sh --deep         # extra cleanup audit+peers
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

# --- Parse data-dir, port, --deep iz argv ili koristi env/default
DATA_DIR="${CLADE_SETUP_DATA_DIR:-$HOME/.clade/setup-server}"
PORT="${CLADE_SETUP_PORT:-8000}"
DEEP_RESET="${CLADE_DEEP_RESET:-}"
# Posto --deep nije setup-server option, izbacujemo ga iz argv pre exec-a
FILTERED_ARGS=()
prev=""
for a in "$@"; do
  if [[ "$prev" == "--data-dir" ]]; then DATA_DIR="$a"; fi
  if [[ "$prev" == "--port" ]]; then PORT="$a"; fi
  if [[ "$a" == "--deep" ]]; then
    DEEP_RESET=1
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

exec "$PY" -m clade_cli.setup_server --port "$PORT" "${FILTERED_ARGS[@]}"

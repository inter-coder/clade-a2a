#!/bin/bash
# clade-wizard.sh — v2 interactive setup wizard za LOKALNI test.
#
# Sta radi:
#   1. Pita za ime projekta + peer-ove + slobodan HTTP port
#   2. Generise peers.yaml + mock peer handler + start/stop/status skripte
#   3. Pokrene clade serve + mock peer u pozadini
#   4. Stampa cheat sheet HOW_TO_USE.txt sa primer komandama
#
# Za PRODUKCIJU (systemd integracija) koristi `clade init --apply` umesto
# ovog skripta — `clade init` generise sistemd unit u ~/.config/systemd/user/,
# wizard ne dira sistemd (sve u izolovanom dir-u).
#
# Uses: clade-a2a paket (v2.0.1+) editable installed u $ROOT/.venv. clade
# CLI ide kroz `python -m clade`.

set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

info() { echo -e "${CYAN}$*${RESET}"; }
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
warn() { echo -e "${YELLOW}⚠${RESET} $*"; }
err()  { echo -e "${RED}✗${RESET} $*"; }
ask()  { read -rp "$(echo -e "${BOLD}$1${RESET} ")" "${2}"; }

# ---- Locate repo + python ----
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  err "Python venv ne postoji u $ROOT/.venv"
  echo "  Run setup prvo:"
  echo "    cd $ROOT"
  echo "    uv venv && uv pip install -e ."
  exit 1
fi

# ---- Header ----
clear
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  Clade A2A v2 — Lokalni Setup Wizard${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo "Pravimo izolovan test setup u jednom direktorijumu:"
echo "  - peers.yaml + clade serve startup skript"
echo "  - mock peer handler (custom Python proces koji odgovara na poruke)"
echo "  - start.sh / stop.sh / status.sh / HOW_TO_USE.txt"
echo ""
echo "Wizard NE dira ~/.config/clade/ ni systemd — sve u jednom dir-u."
echo "Za produkcijski setup sa systemd-om koristi: clade init --apply"
echo ""

# ---- 1. Project name + output dir ----
DEFAULT_NAME="clade-test-$(date +%Y%m%d-%H%M)"
ask "Ime test projekta [${DEFAULT_NAME}]:" PROJECT
PROJECT=${PROJECT:-$DEFAULT_NAME}

DEFAULT_DIR="$HOME/clade-projects/$PROJECT"
ask "Output direktorijum [${DEFAULT_DIR}]:" OUT
OUT=${OUT:-$DEFAULT_DIR}

if [[ -d "$OUT" ]] && [[ "$(ls -A "$OUT" 2>/dev/null)" ]]; then
  warn "$OUT vec postoji i nije prazan."
  ask "Obrisati i krenuti ispocetka? [N/y]:" Y
  [[ "${Y,,}" == "y" ]] || { err "Prekidam."; exit 1; }
  rm -rf "$OUT"
fi
mkdir -p "$OUT"

# ---- 2. Self peer ----
echo ""
info "Self peer (TI na ovoj masini, interactive role)."
SUGGESTIONS_SELF=("katana" "alice" "frontend")
DEFAULT_SELF="${SUGGESTIONS_SELF[$((RANDOM % ${#SUGGESTIONS_SELF[@]}))]}"
ask "  Self peer ime [${DEFAULT_SELF}]:" SELF
SELF=${SELF:-$DEFAULT_SELF}

if ! [[ "$SELF" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]]; then
  err "Ime '$SELF' nije validno (mora poceti slovom, alfanumerik/dash/underscore)."
  exit 1
fi

# ---- 3. Mock peer ----
echo ""
info "Mock peer (drugi proces koji simulira drugog peer-a — echo handler)."
SUGGESTIONS_PEER=("dusan" "bob" "katana-server")
DEFAULT_PEER="${SUGGESTIONS_PEER[$((RANDOM % ${#SUGGESTIONS_PEER[@]}))]}"
[[ "$DEFAULT_PEER" == "$SELF" ]] && DEFAULT_PEER="${SUGGESTIONS_PEER[0]}"
[[ "$DEFAULT_PEER" == "$SELF" ]] && DEFAULT_PEER="bob"
ask "  Mock peer ime [${DEFAULT_PEER}]:" PEER
PEER=${PEER:-$DEFAULT_PEER}

if ! [[ "$PEER" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]]; then
  err "Ime '$PEER' nije validno."
  exit 1
fi
if [[ "$PEER" == "$SELF" ]]; then
  err "Mock peer ne sme biti isti kao self."
  exit 1
fi

# ---- 4. HTTP port ----
echo ""
info "HTTP port za MCP klijent (Claude Code) + /health endpoint."
DEFAULT_PORT=9100
# Brzi prazan-port check
while ss -tln 2>/dev/null | grep -q ":${DEFAULT_PORT} " && [[ $DEFAULT_PORT -lt 9200 ]]; do
  DEFAULT_PORT=$((DEFAULT_PORT + 1))
done
ask "  HTTP port [${DEFAULT_PORT}]:" PORT
PORT=${PORT:-$DEFAULT_PORT}

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [[ $PORT -lt 1024 ]] || [[ $PORT -gt 65535 ]]; then
  err "Port mora biti broj 1024-65535."
  exit 1
fi

# ---- 5. Confirm ----
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Sumarno:${RESET}"
echo "  Projekat:    $PROJECT"
echo "  Lokacija:    $OUT"
echo "  Self:        $SELF  (interactive, HTTP port $PORT)"
echo "  Mock peer:   $PEER  (echo handler, unix socket)"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
ask "Krećemo? [Y/n]:" C
[[ "${C,,}" == "n" ]] && { echo "Prekidam."; exit 0; }

# ---- 6. Generate peers.yaml ----
cat > "$OUT/peers.yaml" <<EOF
# v2 peers.yaml — generisan clade-wizard.sh za lokalni test
# Self peer ($SELF) je interactive (Claude Code se konektuje preko MCP HTTP).
# Mock peer ($PEER) je headless — mock_${PEER}_handler.py echo-uje sve sto stigne.

version: 2
self: $SELF
peers:
  $SELF:
    transport: unix
    role: interactive
    socket: $OUT/$SELF.sock
    http_port: $PORT
    audit_db: $OUT/$SELF-audit.db
    workdir: $OUT/workdirs/$SELF
  $PEER:
    transport: unix
    role: headless
    socket: $OUT/$PEER.sock
EOF
chmod 600 "$OUT/peers.yaml"
ok "peers.yaml generisan"

# ---- 7. Generate mock peer handler ----
cat > "$OUT/mock_${PEER}_handler.py" <<EOF
#!/usr/bin/env python3
"""Mock peer handler za '$PEER' — echo-uje sve sto stigne.

Slusa na /unix:/$OUT/$PEER.sock. Za 'send' poruke samo log-uje;
za 'ask' poruke vraca reply sa {"answer": "echo: <original>"}.

Sluzi za smoke test bez pravog drugog clade serve proceca."""

import asyncio
import sys
sys.path.insert(0, "$ROOT")

from clade.envelope import Envelope
from clade.transport import UnixSocketTransport
from clade.transport.types import Response


async def handler(env: Envelope) -> Response:
    print(f"[$PEER] ← {env.kind} from {env.from_agent}: {env.payload}", flush=True)
    if env.kind == "ask":
        q = env.payload.get("question", env.payload.get("text", "?"))
        reply = Envelope.new(
            from_agent="$PEER",
            to_agent=env.from_agent,
            kind="reply",
            payload={"answer": f"echo: {q}"},
            correlation_id=env.correlation_id,
            thread_id=env.thread_id,
        )
        print(f"[$PEER] → reply: {reply.payload}", flush=True)
        return Response(envelope=reply)
    return Response(envelope=None)


async def main() -> None:
    sock = "$OUT/$PEER.sock"
    print(f"[$PEER] mock handler ready on unix://{sock}", flush=True)
    t = UnixSocketTransport(socket_path=sock)
    try:
        await t.serve(handler)
    finally:
        await t.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"[$PEER] stopped.", flush=True)
EOF
chmod +x "$OUT/mock_${PEER}_handler.py"
ok "mock_${PEER}_handler.py generisan (echo handler)"

# ---- 8. Generate start.sh ----
cat > "$OUT/start.sh" <<EOF
#!/bin/bash
# Pokrece clade serve ($SELF) + mock $PEER handler u pozadini.
# Logovi: serve.log, mock.log.

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

# Sanity: vec ne tece?
if [[ -f serve.pid ]] && kill -0 \$(cat serve.pid 2>/dev/null) 2>/dev/null; then
  echo "clade serve vec tece (PID \$(cat serve.pid))."
  exit 0
fi

# Cleanup stari socket fajlovi i pid fajlovi
rm -f $SELF.sock $PEER.sock
rm -f serve.pid mock.pid

# Start clade serve
nohup $PY -m clade serve --config "\$HERE/peers.yaml" --log-level INFO \\
  > serve.log 2>&1 < /dev/null &
SERVE_PID=\$!
echo \$SERVE_PID > serve.pid
echo "clade serve PID=\$SERVE_PID — log: \$HERE/serve.log"

# Wait za HTTP ready
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.5
  if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    echo "✓ /health ready"
    break
  fi
done

if ! curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
  echo "ERROR: clade serve nije starovao za 5s. Log:"
  tail -20 serve.log
  exit 1
fi

# Start mock peer handler
nohup $PY "\$HERE/mock_${PEER}_handler.py" \\
  > mock.log 2>&1 < /dev/null &
MOCK_PID=\$!
echo \$MOCK_PID > mock.pid
echo "mock $PEER PID=\$MOCK_PID — log: \$HERE/mock.log"

sleep 1
if ! kill -0 \$MOCK_PID 2>/dev/null; then
  echo "ERROR: mock handler crashed. Log:"
  tail -20 mock.log
  exit 1
fi

echo ""
echo "Spreman! Probaj:"
echo "  $PY -m clade status --config \$HERE/peers.yaml"
echo "  $PY -m clade send --config \$HERE/peers.yaml --to $PEER 'hello'"
echo "  $PY -m clade send --config \$HERE/peers.yaml --to $PEER 'Q?' --expect-reply --thread t-1"
echo "  $PY -m clade logs --config \$HERE/peers.yaml --tail 10"
echo ""
echo "Stop sa: \$HERE/stop.sh"
EOF
chmod +x "$OUT/start.sh"

# ---- 9. Generate stop.sh ----
cat > "$OUT/stop.sh" <<EOF
#!/bin/bash
# Gasi clade serve + mock $PEER handler.

cd "\$(dirname "\$0")"

stop_pid() {
  local label="\$1"
  local pidfile="\$2"
  if [[ -f "\$pidfile" ]]; then
    PID=\$(cat "\$pidfile")
    if kill -TERM "\$PID" 2>/dev/null; then
      echo "✓ \$label (PID \$PID) SIGTERM..."
      for i in 1 2 3 4 5; do
        sleep 0.5
        kill -0 "\$PID" 2>/dev/null || { echo "  exited cleanly"; break; }
      done
      if kill -0 "\$PID" 2>/dev/null; then
        kill -KILL "\$PID" 2>/dev/null && echo "  SIGKILL force"
      fi
    else
      echo "  \$label (PID \$PID) nije aktivan"
    fi
    rm -f "\$pidfile"
  else
    echo "  \$label nije pokrenut (nema \$pidfile)"
  fi
}

stop_pid "clade serve" serve.pid
stop_pid "mock $PEER" mock.pid

# Cleanup socket fajlovi koji bi inace ostavili stale state
rm -f $SELF.sock $PEER.sock

echo "Cleanup gotov."
EOF
chmod +x "$OUT/stop.sh"

# ---- 10. Generate status.sh ----
cat > "$OUT/status.sh" <<EOF
#!/bin/bash
# Quick status check: clade serve + mock peer + /health endpoint.

cd "\$(dirname "\$0")"

echo "=== Procesi ==="
for label in "clade serve:serve.pid" "mock $PEER:mock.pid"; do
  NAME=\${label%%:*}
  PIDF=\${label##*:}
  if [[ -f "\$PIDF" ]] && kill -0 \$(cat "\$PIDF") 2>/dev/null; then
    echo "✓ \$NAME tece (PID \$(cat "\$PIDF"))"
  else
    echo "✗ \$NAME NE tece"
  fi
done

echo ""
echo "=== clade status (kroz CLI) ==="
$PY -m clade status --config "\$(pwd)/peers.yaml" 2>&1
EOF
chmod +x "$OUT/status.sh"

# ---- 11. Generate cheat sheet ----
cat > "$OUT/HOW_TO_USE.txt" <<EOF
═══════════════════════════════════════════════════════════════
Clade A2A v2 — lokalni test setup "$PROJECT"
═══════════════════════════════════════════════════════════════

Sve je u: $OUT

Self peer:   $SELF  (interactive role, MCP HTTP na portu $PORT)
Mock peer:   $PEER  (echo handler — vraca {"answer": "echo: <original>"})

───────────────────────────────────────────────────────────────
UPRAVLJANJE
───────────────────────────────────────────────────────────────

  $OUT/start.sh       # pokrene clade serve + mock peer
  $OUT/status.sh      # provera stanja
  $OUT/stop.sh        # gase oba procesa + socket cleanup

  tail -f $OUT/serve.log    # clade serve log
  tail -f $OUT/mock.log     # mock peer log

───────────────────────────────────────────────────────────────
PRIMER KOMANDI (kad sve tece)
───────────────────────────────────────────────────────────────

  # Health table:
  $PY -m clade status --config $OUT/peers.yaml

  # Fire-and-forget send:
  $PY -m clade send --config $OUT/peers.yaml --to $PEER "hello"
  # → "delivered: msg_id=..."

  # Ask sa echo handlerom (mock vraca "echo: <pitanje>"):
  $PY -m clade send --config $OUT/peers.yaml --to $PEER "Sta je 7*6?" \\
    --expect-reply --thread t-test
  # → "reply: {\"answer\": \"echo: Sta je 7*6?\"}"

  # Audit tail:
  $PY -m clade logs --config $OUT/peers.yaml --tail 10

  # Outbox test (zaustavi mock, posalji, vidi pending):
  $OUT/stop.sh
  $OUT/start.sh   # ali NE radi stop mock-a — ili rucno: kill \$(cat $OUT/mock.pid)
  # Onda send → queued, cekaj ~30s outbox retry loop

───────────────────────────────────────────────────────────────
INTERACTIVE CLAUDE CODE INTEGRACIJA (OPCIONO)
───────────────────────────────────────────────────────────────

Da otvoris pravu \`claude\` sesiju koja ce videti clade_message tool:

  mkdir -p $OUT/workdirs/$SELF
  cat > $OUT/workdirs/$SELF/.mcp.json <<MCP
  {
    "mcpServers": {
      "clade": {
        "type": "http",
        "url": "http://127.0.0.1:$PORT"
      }
    }
  }
  MCP

  cd $OUT/workdirs/$SELF && claude
  # U Claude prompt: "Pitaj $PEER koliko je 7*8 preko clade_message"

VAZNO: mock $PEER handler vraca echo, NE pravi LLM odgovor. Za pravi
LLM-driven odgovor treba \`clade serve\` da ima ask handler koji spawn-uje
claude --print — to je v2.1.0 TODO (vidi a2a-protocol.md §7.1).

───────────────────────────────────────────────────────────────
DEBUG
───────────────────────────────────────────────────────────────

  curl http://127.0.0.1:$PORT/health | python3 -m json.tool
  sqlite3 $OUT/$SELF-audit.db "SELECT * FROM audit ORDER BY ts_ms DESC LIMIT 10"
  ls -la $OUT/*.sock                    # vidi socket fajlovi
  pgrep -af "python.*clade serve"       # procesi
EOF

ok "start.sh / stop.sh / status.sh / HOW_TO_USE.txt generisani"

# ---- 12. Auto-start ----
echo ""
ask "Pokrenuti odmah (start.sh)? [Y/n]:" RUN
if [[ "${RUN,,}" != "n" ]]; then
  echo ""
  bash "$OUT/start.sh"
fi

echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  SETUP GOTOV${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "Cheat sheet:  ${YELLOW}cat $OUT/HOW_TO_USE.txt${RESET}"
echo ""
echo -e "${BOLD}Upravljanje:${RESET}"
echo -e "  Status:  ${YELLOW}$OUT/status.sh${RESET}"
echo -e "  Stop:    ${YELLOW}$OUT/stop.sh${RESET}"
echo ""
echo -e "${BOLD}Brzi smoke test:${RESET}"
echo -e "  ${YELLOW}$PY -m clade send --config $OUT/peers.yaml --to $PEER 'hi' --expect-reply${RESET}"
echo ""

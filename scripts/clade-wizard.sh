#!/bin/bash
# clade-wizard.sh — v2.1.0 jedan-shot setup za lokalni multi-agent test.
#
# Generise N pravih `clade serve` procesa (ne mock!) sa kompletnim peers.yaml,
# .mcp.json u svakom workdir-u, start/stop/status/chat skripte. Auto-startuje
# sve + opciono otvara `claude` u tvojoj workdir-u — par enter-a i pricas
# sa peer-om kroz pravi LLM odgovor.
#
# Zahteva: v2.1.0+ (clade/ask_handler.py). Provera se na startu.

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
  echo "  Run setup prvo:  cd $ROOT && uv venv && uv pip install -e ."
  exit 1
fi

# v2.1.0+ check
if ! "$PY" -c "from clade.ask_handler import handle_ask" 2>/dev/null; then
  err "clade.ask_handler nije nadjen — treba clade-a2a v2.1.0+."
  echo "  Update: cd $ROOT && git pull && $PY -m pip install -e ."
  exit 1
fi

# ---- Header ----
clear
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  Clade A2A v2.1 — Multi-Agent Setup Wizard${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo "Pravimo N agenata (pravi clade serve procesi sa LLM ask handler-om)"
echo "u jednom izolovanom direktorijumu. Posle wizard-a:"
echo "  - sve agente pokrenute u pozadini"
echo "  - claude prompt otvoren u tvojoj workdir-u"
echo "  - pricas prirodnim jezikom: 'Pitaj boba sta...'"
echo ""

# ---- 1. Project name + output dir ----
DEFAULT_NAME="agents-$(date +%H%M)"
ask "Ime test setup-a [${DEFAULT_NAME}]:" PROJECT
PROJECT=${PROJECT:-$DEFAULT_NAME}

DEFAULT_DIR="$HOME/clade-projects/$PROJECT"
ask "Direktorijum [${DEFAULT_DIR}]:" OUT
OUT=${OUT:-$DEFAULT_DIR}

if [[ -d "$OUT" ]] && [[ "$(ls -A "$OUT" 2>/dev/null)" ]]; then
  warn "$OUT vec postoji i nije prazan."
  ask "Obrisati i krenuti ispocetka? [N/y]:" Y
  [[ "${Y,,}" == "y" ]] || { err "Prekidam."; exit 1; }
  # Ako ima ranijih clade serve procesa za ovaj dir, pokupi ih
  if [[ -f "$OUT/stop.sh" ]]; then
    bash "$OUT/stop.sh" 2>/dev/null || true
  fi
  rm -rf "$OUT"
fi
mkdir -p "$OUT"

# ---- 2. Number of agents + names ----
echo ""
info "Agenti (svaki tece kao sopstveni clade serve proces)."
echo ""

ask "Koliko agenata? [2]:" N_PEERS
N_PEERS=${N_PEERS:-2}
if ! [[ "$N_PEERS" =~ ^[2-9]$ ]]; then
  err "Mora biti broj 2-9. Za vise — produkcija + systemd preko 'clade init'."
  exit 1
fi

PEERS=()
SUGGESTIONS=("alice" "bob" "carol" "dave" "eve" "frank" "grace" "henry" "ivy")
for ((i=0; i<N_PEERS; i++)); do
  DEFAULT="${SUGGESTIONS[$i]}"
  ask "  Agent $((i+1)) ime [${DEFAULT}]:" P
  P=${P:-$DEFAULT}
  [[ "$P" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]] || { err "Ime '$P' nije validno."; exit 1; }
  for e in "${PEERS[@]}"; do [[ "$e" == "$P" ]] && { err "Duplikat '$P'."; exit 1; }; done
  PEERS+=("$P")
done

# ---- 3. Self peer (ko si TI u prompt sesiji) ----
echo ""
info "Self — agent sa kojim ce 'claude' interactive sesija raditi"
echo "(ti otvaras prompt u njegovom workdir-u, on salje peer-ovima)."
ask "  Self peer [${PEERS[0]}]:" SELF
SELF=${SELF:-${PEERS[0]}}
SELF_FOUND=0
for p in "${PEERS[@]}"; do [[ "$p" == "$SELF" ]] && SELF_FOUND=1; done
if [[ $SELF_FOUND -eq 0 ]]; then
  err "Self '$SELF' nije u listi agenata: ${PEERS[*]}"
  exit 1
fi

# ---- 4. HTTP port range ----
echo ""
info "HTTP portovi za MCP klijent endpoint (po jedan po agent-u)."
DEFAULT_PORT_START=9100
while ss -tln 2>/dev/null | grep -q ":${DEFAULT_PORT_START} " && [[ $DEFAULT_PORT_START -lt 9200 ]]; do
  DEFAULT_PORT_START=$((DEFAULT_PORT_START + 1))
done
ask "  Start port [${DEFAULT_PORT_START}]:" PORT_START
PORT_START=${PORT_START:-$DEFAULT_PORT_START}

# ---- 5. Confirm ----
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Sumarno:${RESET}"
echo "  Setup:       $PROJECT"
echo "  Lokacija:    $OUT"
echo "  Agenti:      ${PEERS[*]}"
echo "  Self:        $SELF  (ti pricas u njegovom workdir-u)"
echo "  Portovi:     $PORT_START–$((PORT_START + N_PEERS - 1))"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
ask "Krećemo? [Y/n]:" C
[[ "${C,,}" == "n" ]] && { echo "Prekidam."; exit 0; }

# ---- 6. Generate peers.yaml ----
{
  echo "# v2.1.0 peers.yaml — generisan clade-wizard.sh"
  echo "# Svi agenti su role: interactive (i salju i odgovaraju kroz claude --print)."
  echo ""
  echo "version: 2"
  echo "self: $SELF"
  echo "peers:"
  for i in "${!PEERS[@]}"; do
    p="${PEERS[$i]}"
    port=$((PORT_START + i))
    echo "  $p:"
    echo "    transport: unix"
    echo "    role: interactive"
    echo "    socket: $OUT/$p.sock"
    echo "    http_port: $port"
    echo "    audit_db: $OUT/$p-audit.db"
    echo "    workdir: $OUT/workdirs/$p"
  done
} > "$OUT/peers.yaml"
chmod 600 "$OUT/peers.yaml"
ok "peers.yaml ($N_PEERS agenata)"

# ---- 7. Workdirs + .mcp.json za svaki ----
for i in "${!PEERS[@]}"; do
  p="${PEERS[$i]}"
  port=$((PORT_START + i))
  mkdir -p "$OUT/workdirs/$p"
  cat > "$OUT/workdirs/$p/.mcp.json" <<EOF
{
  "mcpServers": {
    "clade": {
      "type": "http",
      "url": "http://127.0.0.1:$port"
    }
  }
}
EOF
done
ok "workdirs + .mcp.json za sve"

# ---- 8. Per-agent log fajlovi se kreiraju runtime ----

# ---- 9. start.sh — pokrene SVE agente ----
cat > "$OUT/start.sh" <<EOF
#!/bin/bash
# Pokrene sve clade serve agente u pozadini.
set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

# Sanity: vec tece?
ANY_RUNNING=0
for p in ${PEERS[@]}; do
  pidfile="\$HERE/\$p.pid"
  if [[ -f "\$pidfile" ]] && kill -0 \$(cat "\$pidfile" 2>/dev/null) 2>/dev/null; then
    echo "✓ \$p vec tece (PID \$(cat "\$pidfile"))"
    ANY_RUNNING=1
  fi
done
[[ \$ANY_RUNNING -eq $N_PEERS ]] && { echo "Svi agenti vec tece. Status: ./status.sh"; exit 0; }

# Cleanup stari soketi (graceful shutdown ih brise, ali ako je crash-ovao...)
rm -f *.sock

for p in ${PEERS[@]}; do
  pidfile="\$HERE/\$p.pid"
  if [[ -f "\$pidfile" ]] && kill -0 \$(cat "\$pidfile" 2>/dev/null) 2>/dev/null; then
    continue  # vec tece
  fi
  rm -f "\$pidfile"
  nohup $PY -m clade serve --config "\$HERE/peers.yaml" --peer "\$p" --log-level INFO \\
    > "\$HERE/\$p.log" 2>&1 < /dev/null &
  echo \$! > "\$pidfile"
  echo "▸ \$p PID=\$(cat "\$pidfile") (log: \$HERE/\$p.log)"
done

# Wait HTTP ready za svakog
sleep 1
for i in 1 2 3 4 5 6 7 8 9 10; do
  ALL_UP=1
EOF

# Add port readiness checks (one per peer)
for i in "${!PEERS[@]}"; do
  p="${PEERS[$i]}"
  port=$((PORT_START + i))
  echo "  curl -sf http://127.0.0.1:$port/health > /dev/null 2>&1 || ALL_UP=0" >> "$OUT/start.sh"
done

cat >> "$OUT/start.sh" <<EOF
  [[ \$ALL_UP -eq 1 ]] && break
  sleep 0.5
done

# Verifikuj
ALL_UP=1
EOF

for i in "${!PEERS[@]}"; do
  p="${PEERS[$i]}"
  port=$((PORT_START + i))
  cat >> "$OUT/start.sh" <<EOF
if curl -sf http://127.0.0.1:$port/health > /dev/null 2>&1; then
  echo "✓ $p ready na :$port"
else
  echo "✗ $p NIJE startovao. Log: \$HERE/$p.log"
  tail -10 \$HERE/$p.log
  ALL_UP=0
fi
EOF
done

cat >> "$OUT/start.sh" <<EOF

if [[ \$ALL_UP -ne 1 ]]; then
  echo ""
  echo "Neki agenti nisu starovali. Stop sve: \$HERE/stop.sh"
  exit 1
fi

echo ""
echo "Sve spremno. Pokreni chat:"
echo "  \$HERE/chat.sh"
echo "ili rucno:  cd \$HERE/workdirs/$SELF && claude"
EOF
chmod +x "$OUT/start.sh"

# ---- 10. stop.sh — gase sve ----
cat > "$OUT/stop.sh" <<EOF
#!/bin/bash
# Gasi sve clade serve agente (SIGTERM + 5s wait + SIGKILL fallback).
cd "\$(dirname "\$0")"

for p in ${PEERS[@]}; do
  pidfile="./\$p.pid"
  if [[ ! -f "\$pidfile" ]]; then
    echo "  \$p — nema pid fajla"
    continue
  fi
  PID=\$(cat "\$pidfile")
  if kill -TERM "\$PID" 2>/dev/null; then
    echo "▸ \$p (PID \$PID) SIGTERM..."
    for i in 1 2 3 4 5; do
      sleep 0.5
      kill -0 "\$PID" 2>/dev/null || { echo "  exited cleanly"; break; }
    done
    if kill -0 "\$PID" 2>/dev/null; then
      kill -KILL "\$PID" 2>/dev/null && echo "  SIGKILL force"
    fi
  else
    echo "  \$p (PID \$PID) nije aktivan"
  fi
  rm -f "\$pidfile"
done

# Cleanup stale soketa (graceful release_lock bi ih obrisao, ali insurance)
rm -f *.sock

echo "Cleanup gotov."
EOF
chmod +x "$OUT/stop.sh"

# ---- 11. status.sh — health check svih ----
cat > "$OUT/status.sh" <<EOF
#!/bin/bash
cd "\$(dirname "\$0")"

echo "=== Procesi + /health ==="
EOF
for i in "${!PEERS[@]}"; do
  p="${PEERS[$i]}"
  port=$((PORT_START + i))
  cat >> "$OUT/status.sh" <<EOF
if [[ -f $p.pid ]] && kill -0 \$(cat $p.pid) 2>/dev/null; then
  PID=\$(cat $p.pid)
  STATS=\$(curl -sf http://127.0.0.1:$port/health 2>/dev/null | $PY -c "import sys,json; d=json.load(sys.stdin); print(f'uptime={d[\"uptime_s\"]}s inbox={d[\"inbox_processed_total\"]} outbox={d[\"outbox_pending\"]}p/{d[\"outbox_dead\"]}d')")
  echo "✓ $p (PID \$PID, port $port)  \$STATS"
else
  echo "✗ $p NE TECE"
fi
EOF
done
chmod +x "$OUT/status.sh"

# ---- 12. chat.sh — otvori claude u self workdir-u ----
cat > "$OUT/chat.sh" <<EOF
#!/bin/bash
# Otvara interactive Claude u workdir-u self peer-a ($SELF). Claude
# auto-discover-uje .mcp.json → vidi clade_message tool.
#
# U prompt-u prirodno: "Pitaj <peer> sta..." → real LLM odgovor.

cd "\$(dirname "\$0")"

# Sanity: agenti tece?
ANY_DOWN=0
EOF
for i in "${!PEERS[@]}"; do
  p="${PEERS[$i]}"
  port=$((PORT_START + i))
  cat >> "$OUT/chat.sh" <<EOF
curl -sf http://127.0.0.1:$port/health > /dev/null 2>&1 || { echo "✗ $p ne tece — pokreni: ./start.sh"; ANY_DOWN=1; }
EOF
done

OTHERS=()
for p in "${PEERS[@]}"; do [[ "$p" != "$SELF" ]] && OTHERS+=("$p"); done
EXAMPLE_PEER="${OTHERS[0]}"

cat >> "$OUT/chat.sh" <<EOF
[[ \$ANY_DOWN -eq 1 ]] && exit 1

cd workdirs/$SELF
echo "Otvaram Claude u workdir-u za '$SELF'."
echo ""
echo "Primer prompt-ova:"
echo "  > Pitaj $EXAMPLE_PEER koliko je 7 puta 8"
echo "  > Saznaj od $EXAMPLE_PEER glavni grad Francuske"
echo "  > Pitam $EXAMPLE_PEER da li radi (kroz clade_message)"
echo ""
echo "Exit: Ctrl+D u Claude prompt. Agenti ostaju da tece — stop sa ./stop.sh."
echo ""
exec claude
EOF
chmod +x "$OUT/chat.sh"

# ---- 13. HOW_TO_USE.txt ----
cat > "$OUT/HOW_TO_USE.txt" <<EOF
═══════════════════════════════════════════════════════════════
Clade A2A v2.1 — multi-agent test "$PROJECT"
═══════════════════════════════════════════════════════════════

Agenti:    ${PEERS[*]}
Self:      $SELF  (tvoj prompt sa Claude-om u njegovom workdir-u)
Portovi:   $PORT_START–$((PORT_START + N_PEERS - 1))
Lokacija:  $OUT

───────────────────────────────────────────────────────────────
JEDAN-KOMAND FLOW
───────────────────────────────────────────────────────────────

  $OUT/start.sh    # pokrene sve agente
  $OUT/chat.sh     # otvara Claude u $SELF workdir-u
  $OUT/stop.sh     # gase sve (radi cisto)

───────────────────────────────────────────────────────────────
KAKO ZNAS DA RADI
───────────────────────────────────────────────────────────────

U Claude prompt-u (posle chat.sh):

  > Pitaj $EXAMPLE_PEER koliko je 7 puta 8

Sta se desi pod haubom:
  1. Tvoj Claude pozove clade_message(to="$EXAMPLE_PEER", expect_reply=True)
  2. $SELF clade serve posalje envelope na $EXAMPLE_PEER socket
  3. $EXAMPLE_PEER clade serve spawn-uje 'claude --print'
  4. Pravi LLM izracuna "56" → vraca reply
  5. Tvoj Claude prikaze: "Bob kaze: 56"

Latencija: 3-5s po pozivu (claude --print + LLM inference).

───────────────────────────────────────────────────────────────
DEBUG
───────────────────────────────────────────────────────────────

  $OUT/status.sh                              # health svih
  tail -f $OUT/$SELF.log                      # tvoj serve log
EOF
for p in "${PEERS[@]}"; do
  [[ "$p" != "$SELF" ]] && echo "  tail -f $OUT/$p.log                      # $p serve log" >> "$OUT/HOW_TO_USE.txt"
done
cat >> "$OUT/HOW_TO_USE.txt" <<EOF

  $PY -m clade status --config $OUT/peers.yaml --peer $SELF
  $PY -m clade logs --config $OUT/peers.yaml --peer $SELF --tail 10

CLI send bez Claude sesije (debug):
  $PY -m clade send --config $OUT/peers.yaml --to $EXAMPLE_PEER "hi" --expect-reply
EOF

ok "start.sh / stop.sh / status.sh / chat.sh / HOW_TO_USE.txt"

# ---- 14. Auto-start ----
echo ""
ask "Pokrenuti sve agente sad? [Y/n]:" RUN_START
if [[ "${RUN_START,,}" != "n" ]]; then
  echo ""
  bash "$OUT/start.sh"
fi

# ---- 15. Auto-claude ----
if [[ "${RUN_START,,}" != "n" ]]; then
  echo ""
  ask "Otvoriti claude prompt u workdir-u za '$SELF' sad? [Y/n]:" RUN_CHAT
  if [[ "${RUN_CHAT,,}" != "n" ]]; then
    echo ""
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${GREEN}  Otvaram Claude. U prompt:  'Pitaj $EXAMPLE_PEER ...'${RESET}"
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
    echo ""
    sleep 1
    exec bash "$OUT/chat.sh"
  fi
fi

# ---- 16. Final summary (only reached ako user kaze N na chat) ----
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  SETUP GOTOV${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "${BOLD}Sledeci korak:${RESET}"
echo -e "  ${YELLOW}$OUT/chat.sh${RESET}    # otvara claude u $SELF workdir-u"
echo ""
echo -e "${BOLD}Upravljanje:${RESET}"
echo -e "  Status:  ${YELLOW}$OUT/status.sh${RESET}"
echo -e "  Stop:    ${YELLOW}$OUT/stop.sh${RESET}"
echo -e "  Cheat:   ${YELLOW}cat $OUT/HOW_TO_USE.txt${RESET}"
echo ""

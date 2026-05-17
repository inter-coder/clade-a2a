#!/bin/bash
# clade-deploy.sh — generator za multi-masinski A2A deploy.
#
# Razlika u odnosu na clade-wizard.sh (koji je za lokalno testiranje):
#   - Wizard: sve tece na jednoj masini (3 terminala)
#   - Deploy: generise 3+ bundle-a koji se SCP-uju na razlicite masine
#
# Output struktura:
#   ~/clade-projects/<name>/
#   ├── INSTRUCTIONS.txt        — sta dalje
#   ├── server-bundle/          — scp to relay masinu
#   │   ├── start.sh
#   │   ├── tokens.json
#   │   └── README.md
#   └── agent-<peer1>-bundle/   — scp to peer1 masinu
#       ├── start.sh
#       ├── <peer1>.yaml
#       ├── .mcp.json
#       ├── CLAUDE.md
#       └── README.md

set -e

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

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ ! -x "$PY" ]] && { err "Treba .venv u $ROOT. Run: cd $ROOT && uv venv && uv pip install -e ."; exit 1; }

clear
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  Clade A2A — Multi-Machine Deploy${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo "Generisemo zasebne bundle-ove za:"
echo "  - 1 relay masinu (server)"
echo "  - N agent masina (po peer-u)"
echo ""
echo "Posle ce ti samo da:"
echo "  scp -r server-bundle/  user@relay-machine:~/clade-server/"
echo "  scp -r agent-<X>-bundle/ user@peerX-machine:~/clade-agent/"
echo "  ssh na svaku → ./start.sh"
echo ""

# ---- Korak 1: Projekat ----
DEFAULT_NAME="clade-$(date +%Y%m%d-%H%M)"
ask "Ime deploy projekta [${DEFAULT_NAME}]:" PROJECT
PROJECT=${PROJECT:-$DEFAULT_NAME}

DEFAULT_OUT="$HOME/clade-projects/$PROJECT"
ask "Gde da snimim bundle-ove [${DEFAULT_OUT}]:" OUT
OUT=${OUT:-$DEFAULT_OUT}

if [[ -d "$OUT" ]] && [[ "$(ls -A "$OUT" 2>/dev/null)" ]]; then
  warn "$OUT vec postoji i nije prazan."
  ask "Obrisati? [N/y]:" Y
  [[ "${Y,,}" == "y" ]] || { err "Prekidam."; exit 1; }
  rm -rf "$OUT"
fi
mkdir -p "$OUT"

# ---- Korak 2: Relay masina ----
echo ""
info "RELAY MASINA — gde ce relay servis da tece."
echo "Mora biti dostupna sa SVIH agent masina (LAN IP ili public IP)."
echo "Ne moze biti 127.0.0.1 ako agenti nisu na istoj masini!"
echo ""

ask "IP/hostname relay masine (npr. 10.0.0.5):" RELAY_IP
[[ -z "$RELAY_IP" ]] && { err "Relay IP je obavezan."; exit 1; }

DEFAULT_PORT="7777"
ask "Relay port [${DEFAULT_PORT}]:" RELAY_PORT
RELAY_PORT=${RELAY_PORT:-$DEFAULT_PORT}

RELAY_URL="http://${RELAY_IP}:${RELAY_PORT}"

# ---- Korak 3: Peer-ovi ----
echo ""
info "Agent peer-ovi (svaki tece na svojoj masini)."
echo ""

ask "Koliko peer-ova? [2]:" N_PEERS
N_PEERS=${N_PEERS:-2}

if ! [[ "$N_PEERS" =~ ^[2-9]$|^1[0-9]$ ]]; then
  err "Mora biti broj 2-19."
  exit 1
fi

PEERS=()
SUGGESTIONS=("frontend" "katana" "alice" "bob" "carol")
for ((i=0; i<N_PEERS; i++)); do
  DEFAULT="${SUGGESTIONS[$i]:-peer$((i+1))}"
  ask "  Peer $((i+1)) ime [${DEFAULT}]:" P
  P=${P:-$DEFAULT}
  [[ "$P" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]] || { err "Ime '$P' nije validno."; exit 1; }
  for e in "${PEERS[@]}"; do [[ "$e" == "$P" ]] && { err "Duplikat '$P'."; exit 1; }; done
  PEERS+=("$P")
done

# ---- Korak 4: Putanja do clade-a2a na targetnoj masini ----
echo ""
info "Gde ce clade-a2a biti instaliran na targetnim masinama?"
echo "Mora biti isto na server-u i SVIM agent masinama."
echo "Najlakse: 'git clone https://github.com/inter-coder/clade-a2a.git /opt/clade'"
echo ""

DEFAULT_TARGET_PATH="/opt/clade-a2a"
ask "Putanja [${DEFAULT_TARGET_PATH}]:" TARGET_PATH
TARGET_PATH=${TARGET_PATH:-$DEFAULT_TARGET_PATH}

# Python interpreter na targetu (predpostavka: ima .venv u repo dir-u)
TARGET_PYTHON="$TARGET_PATH/.venv/bin/python"

# ---- Korak 5: Confirm ----
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Sumarno:${RESET}"
echo "  Projekat:      $PROJECT"
echo "  Lokacija:      $OUT"
echo "  Relay:         $RELAY_IP:$RELAY_PORT  (URL: $RELAY_URL)"
echo "  Peers:         ${PEERS[*]}"
echo "  Target path:   $TARGET_PATH  (na SVAKOJ masini)"
echo "  Target Python: $TARGET_PYTHON"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
echo ""
ask "Krećemo? [Y/n]:" C
[[ "${C,,}" == "n" ]] && { echo "Prekidam."; exit 0; }

# ---- Korak 6: Generiši temp bootstrap ----
TMP_BOOTSTRAP="$OUT/.tmp-bootstrap"
mkdir -p "$TMP_BOOTSTRAP"
"$PY" -m clade_cli.init --peers "${PEERS[@]}" --output "$TMP_BOOTSTRAP" \
  --relay-url "$RELAY_URL" --agent-python "$TARGET_PYTHON" > /dev/null

# ---- Korak 7: SERVER BUNDLE ----
SERVER_DIR="$OUT/server-bundle"
mkdir -p "$SERVER_DIR"
cp "$TMP_BOOTSTRAP/tokens.json" "$SERVER_DIR/"
chmod 600 "$SERVER_DIR/tokens.json"

cat > "$SERVER_DIR/start.sh" <<EOF
#!/bin/bash
# Pokrene Clade relay na ovoj masini.
# Preduslov: clade-a2a instaliran u $TARGET_PATH (vidi README.md).

set -e
cd "\$(dirname "\$0")"

if [[ ! -x "$TARGET_PYTHON" ]]; then
  echo "ERROR: clade-a2a nije instaliran u $TARGET_PATH"
  echo "Install: git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH"
  echo "         cd $TARGET_PATH && uv venv && uv pip install -e ."
  exit 1
fi

echo "Pokrecem relay na 0.0.0.0:$RELAY_PORT (svi interface-i)..."
echo "Stop sa Ctrl+C"
echo ""
exec "$TARGET_PYTHON" -c "
import sys, pathlib, uvicorn, os
sys.path.insert(0, '$TARGET_PATH')
import relay.main
relay.main.TOKENS_PATH = pathlib.Path(os.path.abspath('./tokens.json'))
uvicorn.run(relay.main.app, host='0.0.0.0', port=$RELAY_PORT, log_level='info')
"
EOF
chmod +x "$SERVER_DIR/start.sh"

cat > "$SERVER_DIR/README.md" <<EOF
# Clade Relay — server-bundle za "$PROJECT"

Pokreni Clade relay na ovoj masini.

## Preduslov (jednom)

Instaliraj clade-a2a u istom path-u koji je generator koristio:

\`\`\`bash
sudo mkdir -p $TARGET_PATH
sudo chown \$USER $TARGET_PATH
git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH
cd $TARGET_PATH
# Instaliraj uv ako ga nemas: curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv venv
~/.local/bin/uv pip install -e .
\`\`\`

## Pokretanje

\`\`\`bash
./start.sh
\`\`\`

Slusa na **0.0.0.0:$RELAY_PORT** (svi interface-i). Drugi peer-ovi se konektuju na ovaj IP.

## Firewall

Otvori port $RELAY_PORT za LAN/VPN:

\`\`\`bash
sudo ufw allow $RELAY_PORT/tcp
\`\`\`

## Health check

\`\`\`bash
curl http://localhost:$RELAY_PORT/health
\`\`\`

## Audit log UI

U browser-u: http://$RELAY_IP:$RELAY_PORT/ui/audit
(paste bilo koji bearer token iz tokens.json za login)

## SECURITY

- \`tokens.json\` sadrzi bearer tokene SVIH peer-ova. **NIKAD u git, NIKAD u Slack/mejl.**
- Distribuiraj agent bundle-ove peer-ovima preko sigurnih kanala (Signal, USB, scp direktno).
- Permissions 0600 (vec setovano).
EOF

ok "server-bundle/ generisan"

# ---- Korak 8: AGENT BUNDLES ----
for peer in "${PEERS[@]}"; do
  AGENT_DIR="$OUT/agent-$peer-bundle"
  mkdir -p "$AGENT_DIR"

  # Config — ali sa apsolutnom putanjom (target lokacija na agent masini)
  # Mora se reaza relay_url da ostane $RELAY_URL (sa pravim IP-em, ne localhost)
  cp "$TMP_BOOTSTRAP/$peer.yaml" "$AGENT_DIR/"
  # Override audit_db da bude relativan na agent dir
  sed -i "s|audit_db: .*|audit_db: ./audit.db|" "$AGENT_DIR/$peer.yaml"
  chmod 600 "$AGENT_DIR/$peer.yaml"

  # .mcp.json — pokazuje na agent/main.py u $TARGET_PATH + ovaj $peer.yaml
  cat > "$AGENT_DIR/.mcp.json" <<EOF
{
  "mcpServers": {
    "clade": {
      "command": "$TARGET_PYTHON",
      "args": ["$TARGET_PATH/agent/main.py"],
      "env": {
        "CLADE_CONFIG": "%CONFIG_PATH%"
      }
    }
  }
}
EOF
  # Note: %CONFIG_PATH% ce biti rewrite-ovan na startup-u (jer u tom trenutku znamo
  # pwd na agent masini) — to radi start.sh

  cp "$TMP_BOOTSTRAP/CLAUDE.md" "$AGENT_DIR/"

  # Sacuvaj .mcp.json kao template (sed-uje se runtime-om u claude.sh)
  mv "$AGENT_DIR/.mcp.json" "$AGENT_DIR/.mcp.json.template"

  # start.sh — pokrece DAEMON (uvek slusa, auto-odgovara na asks)
  cat > "$AGENT_DIR/start.sh" <<EOF
#!/bin/bash
# Pokrene Clade Agent DAEMON za peer "$peer".
# Daemon polluje relay svake 2s, auto-odgovara na ask poruke kroz
# 'claude --print' subprocess.
#
# Use:
#   ./start.sh             - safe mode (Claude moze tražiti permission)
#   ./start.sh --yolo      - YOLO mode (--dangerously-skip-permissions)
#
# Stop:
#   Ctrl+C u istom terminalu, ILI: ./stop.sh

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

if [[ ! -x "$TARGET_PYTHON" ]]; then
  echo "ERROR: clade-a2a nije instaliran u $TARGET_PATH"
  echo "Install: git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH"
  echo "         cd $TARGET_PATH && uv venv && uv pip install -e ."
  exit 1
fi

# Health check relay-a
if ! curl -sf --max-time 5 "$RELAY_URL/health" > /dev/null; then
  echo "⚠ Relay nedostupan na $RELAY_URL"
  echo "  Daemon ce probati anyway (retry-uje), ali ne moze raditi posao."
  read -rp "Nastaviti? [y/N] " Y
  [[ "\${Y,,}" == "y" ]] || exit 1
fi

# Sacuvaj PID za stop.sh
PIDFILE="\$HERE/daemon.pid"
echo \$\$ > "\$PIDFILE"
trap "rm -f \$PIDFILE" EXIT

export CLADE_CONFIG="\$HERE/$peer.yaml"
exec "$TARGET_PYTHON" -m agent.daemon "\$@"
EOF
  chmod +x "$AGENT_DIR/start.sh"

  # stop.sh — kill daemon
  cat > "$AGENT_DIR/stop.sh" <<EOF
#!/bin/bash
# Gasi Clade daemon za peer "$peer".
cd "\$(dirname "\$0")"
PIDFILE="./daemon.pid"
if [[ ! -f "\$PIDFILE" ]]; then
  echo "Daemon nije pokrenut (nema daemon.pid)."
  # Pokusaj svakako da nadjes proces (ako je startovan ne kroz start.sh):
  PIDS=\$(pgrep -f "agent.daemon.*CLADE_CONFIG=.*$peer.yaml" 2>/dev/null || true)
  if [[ -n "\$PIDS" ]]; then
    echo "Nasao orphan daemon proces(e): \$PIDS"
    read -rp "Gasiti ih? [y/N] " Y
    [[ "\${Y,,}" == "y" ]] && kill \$PIDS && echo "Ugasen."
  fi
  exit 0
fi
PID=\$(cat "\$PIDFILE")
if kill "\$PID" 2>/dev/null; then
  echo "Daemon (PID \$PID) ugasen."
else
  echo "Daemon (PID \$PID) nije aktivan."
fi
rm -f "\$PIDFILE"
EOF
  chmod +x "$AGENT_DIR/stop.sh"

  # status.sh — quick health
  cat > "$AGENT_DIR/status.sh" <<EOF
#!/bin/bash
# Status Clade daemon-a + agent-a za peer "$peer".
cd "\$(dirname "\$0")"
echo "=== Daemon ==="
if [[ -f daemon.pid ]] && kill -0 \$(cat daemon.pid) 2>/dev/null; then
  echo "✓ Daemon tece (PID \$(cat daemon.pid))"
else
  echo "✗ Daemon NE tece (nema daemon.pid ili process mrtav)"
fi

echo ""
echo "=== Relay ==="
if curl -sf --max-time 3 "$RELAY_URL/health" 2>/dev/null | "$TARGET_PYTHON" -m json.tool; then
  :
else
  echo "✗ Relay nedostupan na $RELAY_URL"
fi

echo ""
echo "=== Inbox size (server side) ==="
TOKEN=\$(grep '^bearer_token:' $peer.yaml | awk '{print \$2}')
curl -sf "$RELAY_URL/health" -H "Authorization: Bearer \$TOKEN" 2>/dev/null | \\
  "$TARGET_PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d.get('store',{}).get('inbox_sizes',{}))"
EOF
  chmod +x "$AGENT_DIR/status.sh"

  # claude.sh — opens interactive Claude (separate terminal usage)
  cat > "$AGENT_DIR/claude.sh" <<EOF
#!/bin/bash
# Otvara INTERACTIVE Claude sesiju za peer "$peer".
# Ovo koristis kad zelis da TI inicijaras komunikaciju ("pitaj X o Y").
# Daemon u drugom terminalu vec auto-odgovara na incoming poruke.

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

if [[ ! -x "$TARGET_PYTHON" ]]; then
  echo "ERROR: clade-a2a nije instaliran u $TARGET_PATH"
  exit 1
fi

# Rewrite .mcp.json sa pravim apsolutnim putanjama
sed "s|%CONFIG_PATH%|\$HERE/$peer.yaml|" .mcp.json.template > .mcp.json

# Daemon health warning
if [[ ! -f daemon.pid ]] || ! kill -0 \$(cat daemon.pid 2>/dev/null) 2>/dev/null; then
  echo "⚠ Daemon NE tece — incoming asks od drugih peer-ova nece dobiti odgovor."
  echo "  Pokreni: ./start.sh (u drugom terminalu)"
  echo ""
fi

echo "Otvaram interactive Claude — koristi prirodan jezik:"
echo "  'Pitaj <peer> koliko je 7*8'"
echo "  'Javi <peer> da je deploy gotov'"
echo ""
exec claude
EOF
  chmod +x "$AGENT_DIR/claude.sh"

  # Per-agent README
  OTHER_PEERS=()
  for p in "${PEERS[@]}"; do [[ "$p" != "$peer" ]] && OTHER_PEERS+=("$p"); done
  SAMPLE_PEER="${OTHER_PEERS[0]}"

  cat > "$AGENT_DIR/README.md" <<EOF
# Clade Agent "$peer" — bundle za "$PROJECT"

Ovo je sve sto treba za peer **$peer** na njegovoj masini.

## Sta sadrzi

\`\`\`
start.sh           ← pokrene DAEMON (uvek slusa, auto-odgovara) — TERMINAL 1
claude.sh          ← otvara INTERACTIVE Claude (da posaljes nesto) — TERMINAL 2
stop.sh            ← gasi daemon
status.sh          ← daemon + relay zdravlje
$peer.yaml         ← config (bearer token + HMAC secrets) — SECRET, 0600
.mcp.json.template ← template za Claude MCP integraciju
CLAUDE.md          ← instrukcije za interactive Claude
\`\`\`

## Preduslov (jednom po masini)

Instaliraj clade-a2a + Claude Code CLI:

\`\`\`bash
sudo mkdir -p $TARGET_PATH && sudo chown \$USER $TARGET_PATH
git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH
cd $TARGET_PATH
~/.local/bin/uv venv && ~/.local/bin/uv pip install -e .
# (ako nemas uv: curl -LsSf https://astral.sh/uv/install.sh | sh)
\`\`\`

I Claude Code: vidi https://docs.claude.com/claude-code

## Korak 1 — Pokreni DAEMON (uvek tece, terminal 1)

\`\`\`bash
./start.sh           # safe mode
# ili
./start.sh --yolo    # YOLO mode (--dangerously-skip-permissions)
\`\`\`

Daemon ce:
- Polovati relay svake 2s za nove poruke
- Auto-odgovarati na ask poruke kroz \`claude --print\` u pozadini
- Logovati incoming send poruke (info, ne odgovara na njih)

Stop: \`Ctrl+C\` ili \`./stop.sh\` iz drugog terminala.

## Korak 2 — Otvori INTERACTIVE Claude (po potrebi, terminal 2)

Kad zelis TI da posaljes nesto peer-u:

\`\`\`bash
./claude.sh
\`\`\`

Sad u Claude prompt:

\`\`\`
Pitaj $SAMPLE_PEER koliko je 7 puta 8
\`\`\`

Claude automatski razume da treba \`clade_ask(to="$SAMPLE_PEER", ...)\`. Ne moras
ti da kazes "preko clade_ask" — CLAUDE.md ga je vec naucio.

Drugi primeri:
\`\`\`
Javi $SAMPLE_PEER da je deploy gotov
Pitam $SAMPLE_PEER za status migracije
Saznaj od $SAMPLE_PEER koliko je danas zahteva
\`\`\`

## Sta peer dobija

Na drugoj strani, $SAMPLE_PEER-ov daemon polluje inbox. Kad vidi tvoj ask,
spawn-uje \`claude --print\` da izracuna odgovor i salje ga nazad. Tvoja
interactive sesija dobija odgovor i prikazuje ga.

Latencija: ~5-30 sekundi (zavisi od kompleksnosti pitanja i toga da li
peer koristi YOLO mode).

## Operativno

\`\`\`bash
./status.sh        # daemon + relay zdravlje
./stop.sh          # gasi daemon
./start.sh         # ponovo pokreni daemon
\`\`\`

Audit UI: $RELAY_URL/ui/audit (paste bearer token iz $peer.yaml)

## SECURITY

- \`$peer.yaml\` sadrzi tvoj bearer token + HMAC secret(e) sa drugim peer-ovima.
- Permissions 0600. **NIKAD u git, NIKAD u Slack/mejl.**
- Ako shared secret procuri, drugi peer-ovi mogu da impersoniraju te.
  Rotacija: generator pokrene \`clade-deploy.sh\` ponovo i distribuira nove bundle-ove.

## YOLO mode — kad da koristim?

Default je SAFE: \`claude --print\` ce traziti permission za tool koriscenje,
ali kao headless poziv to znaci timeout (nema covek-u-petlji da approvuje).

**YOLO** (\`--yolo\`) prosledjuje \`--dangerously-skip-permissions\`. Koristi ga
kad ocekujes da peer poziva tool-ove da odgovori (npr. DB query). Trade-off:
ako peer (ili compromised relay) posalje malicious prompt, Claude moze da
izvrsi proizvoljne komande BEZ tvog approval-a.

Granica: koristi YOLO samo kad VERUJES svim peer-ovima u svoj allowlist-u.

## Troubleshooting

**Daemon log pokazuje "claude exited 1"** — proveri da je \`claude\` u PATH-u
i da si authentifikovan: \`claude --version\`. Ako trazi login: \`claude /login\`.

**Daemon log pokazuje "relay unreachable"** — proveri sa terminal-a:
\`\`\`bash
curl $RELAY_URL/health
\`\`\`
Ako pukne: VPN/firewall problem ili je server-bundle ugasen.

**Claude (interactive) ne vidi clade_* tool-ove** — proveri da \`./claude.sh\`
generisao \`.mcp.json\` (ne template). \`cat .mcp.json\` — putanje moraju biti
realne, ne \`%CONFIG_PATH%\`.

**Inbox raste, daemon ne odgovara** — proveri daemon je up: \`./status.sh\`.
Ako jeste up i ne hvata: vidi log u njegovom terminalu, mozda HMAC mismatch
(secret kod tebe se razlikuje od secret kod peer-a).
EOF

  ok "agent-$peer-bundle/ generisan"
done

# ---- Korak 9: Top-level INSTRUCTIONS ----
PEER_LIST=$(printf "  - %s\n" "${PEERS[@]}")
cat > "$OUT/INSTRUCTIONS.txt" <<EOF
═══════════════════════════════════════════════════════════════
Clade A2A Multi-Machine Deploy — "$PROJECT"
═══════════════════════════════════════════════════════════════

GENERISANO:
  $OUT/server-bundle/         (1 dir, scp na relay masinu)
$(for p in "${PEERS[@]}"; do echo "  $OUT/agent-$p-bundle/    (1 dir, scp na $p masinu)"; done)

═══════════════════════════════════════════════════════════════
SLEDECI KORACI:
═══════════════════════════════════════════════════════════════

1) Na svakoj ciljnoj masini (server + N peer-ova) instaliraj clade-a2a:

   sudo mkdir -p $TARGET_PATH
   sudo chown \$USER $TARGET_PATH
   git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH
   cd $TARGET_PATH
   ~/.local/bin/uv venv  # ili instaliraj uv prvo
   ~/.local/bin/uv pip install -e .

2) SCP bundle na svaku masinu:

   # Na relay masinu:
   scp -r $OUT/server-bundle/ user@$RELAY_IP:~/clade-server/

$(for p in "${PEERS[@]}"; do echo "   # Na $p masinu (zameni <ip> sa pravim):"; echo "   scp -r $OUT/agent-$p-bundle/ user@<ip>:~/clade-agent/"; echo ""; done)

3) Na RELAY masini (terminal 1):

   ssh user@$RELAY_IP
   cd ~/clade-server
   ./start.sh
   # → relay tece na 0.0.0.0:$RELAY_PORT, log na stdout

4) Na svakoj agent masini, pokreni DAEMON (terminal 1):

$(for p in "${PEERS[@]}"; do echo "   ssh user@<$p-ip>"; echo "   cd ~/clade-agent"; echo "   ./start.sh             # safe mode"; echo "   # ili: ./start.sh --yolo  # YOLO mode (--dangerously-skip-permissions)"; echo "   # → daemon polluje relay, auto-odgovara na asks koje stignu"; echo ""; done)

5) Kad zelis TI da posaljes nesto, na bilo kojoj agent masini (terminal 2):

   cd ~/clade-agent
   ./claude.sh
   # → otvori interactive Claude
   # Pa u prompt: "Pitaj <drugi-peer> koliko je 7 puta 8"
   # → Claude automatski poziva clade_ask, daemon na drugoj strani odgovara,
   #   ti dobijes odgovor

═══════════════════════════════════════════════════════════════
MCP INTEGRACIJA OBJASNJENA:
═══════════════════════════════════════════════════════════════

Svaki agent bundle sadrzi .mcp.json fajl. Kad pokrenes \`claude\` u tom
dir-u, Claude Code automatski:
  - cita .mcp.json
  - spawn-uje navedeni Python proces (clade-a2a agent) kao stdio child
  - registruje 5 clade_* tool-ova koje Claude moze da poziva

Drugim recima: MCP integracija je "drop .mcp.json u dir + cd tamo + claude".
Ne treba nista da konfigurises u Claude UI niti settings.

═══════════════════════════════════════════════════════════════
AUDIT MONITORING:
═══════════════════════════════════════════════════════════════

Otvori http://$RELAY_IP:$RELAY_PORT/ui/audit u browser-u.
Paste bilo koji bearer token iz $OUT/server-bundle/tokens.json za login.

═══════════════════════════════════════════════════════════════
SECURITY:
═══════════════════════════════════════════════════════════════

- tokens.json (server) + <peer>.yaml (agent) sadrze SECRETS
- Permissions 0600 (vec setovano)
- NIKAD ih ne commit-uj u git
- Distribuiraj samo preko sigurnih kanala (scp direktno, Signal, USB)
- Rotacija: pokreni clade-deploy.sh ponovo i distribuiraj nove bundle-ove

═══════════════════════════════════════════════════════════════
EOF

# Cleanup
rm -rf "$TMP_BOOTSTRAP"

echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  DEPLOY BUNDLES GENERISANI${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "Sve je u: ${CYAN}$OUT${RESET}"
echo ""
echo -e "Otvori sledece sa instrukcijama: ${YELLOW}$OUT/INSTRUCTIONS.txt${RESET}"
echo ""
echo -e "Brza sumarno:"
echo -e "  ${CYAN}1.${RESET} scp -r $OUT/server-bundle/ user@$RELAY_IP:~/clade-server/"
for p in "${PEERS[@]}"; do
  echo -e "  ${CYAN}.${RESET} scp -r $OUT/agent-$p-bundle/ user@<$p-ip>:~/clade-agent/"
done
echo -e "  ${CYAN}2.${RESET} Na svakoj masini: cd ~/clade-{server|agent} && ./start.sh"
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"

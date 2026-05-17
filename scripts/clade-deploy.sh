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

  # start.sh — pokrece Claude sa pravim configom
  cat > "$AGENT_DIR/start.sh" <<EOF
#!/bin/bash
# Pokrene Claude Code za peer "$peer" sa Clade A2A integracijom.

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

if [[ ! -x "$TARGET_PYTHON" ]]; then
  echo "ERROR: clade-a2a nije instaliran u $TARGET_PATH"
  echo "Install: git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH"
  echo "         cd $TARGET_PATH && uv venv && uv pip install -e ."
  exit 1
fi

# Rewrite .mcp.json sa pravim apsolutnim putanjama (cwd-dependent)
sed "s|%CONFIG_PATH%|\$HERE/$peer.yaml|" .mcp.json.template > .mcp.json 2>/dev/null || true

# Health check relay-a
echo "Test relay konekcije..."
if curl -sf --max-time 5 "$RELAY_URL/health" > /dev/null; then
  echo "✓ Relay OK ($RELAY_URL)"
else
  echo "⚠ Relay nedostupan na $RELAY_URL"
  echo "  Proveri: je li server-bundle pokrenut na ciljnoj masini?"
  echo "  Proveri: VPN/firewall otvoren za port?"
  read -rp "Nastaviti svakako? [y/N] " Y
  [[ "\${Y,,}" == "y" ]] || exit 1
fi

# Sad pokreni Claude u ovom dir-u — .mcp.json + CLAUDE.md su pored
echo ""
echo "Pokrecem Claude Code u: \$HERE"
echo "Claude ce automatski naci .mcp.json i ucitati clade tool-ove."
echo ""
exec claude
EOF
  chmod +x "$AGENT_DIR/start.sh"

  # Sacuvaj .mcp.json kao template (jer start.sh sed-uje na pravi path runtime-om)
  mv "$AGENT_DIR/.mcp.json" "$AGENT_DIR/.mcp.json.template"

  # Per-agent README
  OTHER_PEERS=()
  for p in "${PEERS[@]}"; do [[ "$p" != "$peer" ]] && OTHER_PEERS+=("$p"); done
  SAMPLE_PEER="${OTHER_PEERS[0]}"

  cat > "$AGENT_DIR/README.md" <<EOF
# Clade Agent "$peer" — bundle za "$PROJECT"

Ovo je sve sto treba za peer **$peer** na njegovoj masini.

## Preduslov (jednom)

Instaliraj clade-a2a:

\`\`\`bash
sudo mkdir -p $TARGET_PATH
sudo chown \$USER $TARGET_PATH
git clone https://github.com/inter-coder/clade-a2a.git $TARGET_PATH
cd $TARGET_PATH
~/.local/bin/uv venv  # ili: curl -LsSf https://astral.sh/uv/install.sh | sh && ~/.local/bin/uv venv
~/.local/bin/uv pip install -e .
\`\`\`

Mora i Claude Code:

\`\`\`bash
# Vidi https://docs.claude.com/claude-code za instalaciju
\`\`\`

## Pokretanje

\`\`\`bash
./start.sh
\`\`\`

Sta start.sh radi:
1. Proveri da je clade-a2a instaliran u $TARGET_PATH
2. Test konekciju ka relay-u na $RELAY_URL
3. Generise .mcp.json sa pravom putanjom do tvog config-a
4. Pokrene \`claude\` u ovom dir-u — Claude ce automatski naci .mcp.json
   i ucitati 5 clade_* tool-ova

## Sta da kucnes u Claude

Cim je Claude pokrenut, mozes:

\`\`\`
Pitaj $SAMPLE_PEER koliko je 7 puta 8 preko clade_ask sa timeout 90s.
\`\`\`

Ili samo:

\`\`\`
Pogledaj inbox.
\`\`\`

Claude ce pollovati \`clade_inbox\` (CLAUDE.md u ovom dir-u to zahteva)
i odgovoriti na ask poruke automatski.

## Dostupni tool-ovi (vidi i CLAUDE.md)

- \`clade_send(to, payload)\` — fire-and-forget peer-u
- \`clade_ask(to, payload, timeout_s)\` — sinhroni upit, blokira do odgovora
- \`clade_inbox(max_items)\` — drenira sopstveni inbox
- \`clade_reply(correlation_id, response, to)\` — odgovor na ask
- \`clade_outbox_status()\` — debug stanje outbox-a

## SECURITY

- \`$peer.yaml\` sadrzi tvoj bearer token + HMAC secret(e) sa drugim peer-ovima.
- Permissions 0600. **NIKAD u git, NIKAD u Slack/mejl.**
- Ako shared secret procuri, drugi peer-ovi mogu da impersoniraju te kod jednih
  i drugih. Rotiraj cim posumnjam (generator masina: \`clade-deploy.sh\` ponovo).

## Troubleshooting

**Claude ne vidi clade_* tool-ove** — proveri:
\`\`\`bash
cat .mcp.json
cat .mcp.json.template
\`\`\`
.mcp.json mora postojati i imati realne path-ove (ne %CONFIG_PATH%).
Ako fali, ponovo pokreni \`./start.sh\`.

**"Relay nedostupan"** — proveri sa relay masine:
\`\`\`bash
curl http://$RELAY_IP:$RELAY_PORT/health
\`\`\`

**Inbox prazan iako sam siguran da je peer poslao** — peer-ov Claude jos uvek
razmislja, nije efektivno pozvao tool. Sacekaj 5-10s, pa probaj opet.
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

3) Na RELAY masini:

   ssh user@$RELAY_IP
   cd ~/clade-server
   ./start.sh
   # → relay tece na 0.0.0.0:$RELAY_PORT, log na stdout

4) Na svakoj agent masini (u zasebnom SSH-u):

$(for p in "${PEERS[@]}"; do echo "   ssh user@<$p-ip>"; echo "   cd ~/clade-agent"; echo "   ./start.sh"; echo "   # → Claude Code se otvori sa ucitanim clade_* tool-ovima"; echo ""; done)

5) Test u Claude (bilo kom peer-u):

   "Pitaj <drugi-peer> koliko je 7 puta 8 preko clade_ask, timeout 90s."

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

#!/bin/bash
# clade-wizard-multi.sh — multi-machine bundle generator (v2.2.0+).
#
# Pozvan iz clade-wizard.sh kad korisnik izabere mode=multi. Generise:
#
#   $OUT/
#   ├── INSTRUCTIONS.md         scp + setup walkthrough
#   ├── relay-bundle/           za scp na host koji ce vrteti clade-relay
#   │   ├── tokens.json         bearer → agent_id mapping (svi peer-ovi)
#   │   ├── start.sh            pokrene clade-relay
#   │   └── README.md
#   └── <peer>-bundle/          za scp na svaku peer masinu (N kom.)
#       ├── peers.yaml          self=<peer>, ostali sa transport=relay
#       ├── start.sh            pokrene clade serve --peer <peer>
#       ├── stop.sh / status.sh / chat.sh
#       ├── workdirs/<peer>/.mcp.json
#       └── README.md
#
# Argumenti (iz clade-wizard.sh): PROJECT OUT SELF PORT_START RELAY_URL PEERS...

set -e

PROJECT="$1"; shift
OUT="$1"; shift
SELF="$1"; shift
PORT_START="$1"; shift
RELAY_URL="$1"; shift
PEERS=("$@")
N_PEERS=${#PEERS[@]}

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${CYAN}$*${RESET}"; }
warn() { echo -e "${YELLOW}⚠${RESET} $*"; }

# Repo + python (treba i ovaj wizard isto Python za hex token generator)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then echo "missing $ROOT/.venv"; exit 1; fi

# ---- 1. Generate keys (pair-wise HMAC secrets + per-peer bearer tokens) ----
info "Generišem ključeve (bearer tokeni + pairwise HMAC secrets)..."

declare -A BEARER       # BEARER[peer] = "bearer_token_string"
declare -A PAIR_SECRET  # PAIR_SECRET["alice|bob"] = "hex" (sorted pair → secret)

for p in "${PEERS[@]}"; do
  BEARER[$p]=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
done

for ((i=0; i<N_PEERS; i++)); do
  for ((j=i+1; j<N_PEERS; j++)); do
    pa="${PEERS[$i]}"
    pb="${PEERS[$j]}"
    # Sortirani par za deterministicki kljuc
    if [[ "$pa" < "$pb" ]]; then PAIR="$pa|$pb"; else PAIR="$pb|$pa"; fi
    PAIR_SECRET[$PAIR]=$("$PY" -c "import secrets; print(secrets.token_hex(32))")
  done
done

ok "$N_PEERS bearer tokena + $(( N_PEERS * (N_PEERS-1) / 2 )) pairwise HMAC secrets"

# Helper: vrati secret za par (alice, bob)
pair_secret() {
  local pa="$1" pb="$2"
  if [[ "$pa" < "$pb" ]]; then echo "${PAIR_SECRET[$pa|$pb]}"; else echo "${PAIR_SECRET[$pb|$pa]}"; fi
}

# ---- 2. Relay bundle ----
RELAY_DIR="$OUT/relay-bundle"
mkdir -p "$RELAY_DIR"

# tokens.json: bearer → agent_id mapping
{
  echo "{"
  FIRST=1
  for p in "${PEERS[@]}"; do
    [[ $FIRST -eq 1 ]] || echo ","
    printf '  "%s": "%s"' "${BEARER[$p]}" "$p"
    FIRST=0
  done
  echo ""
  echo "}"
} > "$RELAY_DIR/tokens.json"
chmod 600 "$RELAY_DIR/tokens.json"

# Extract relay port iz URL-a
RELAY_PORT=$(echo "$RELAY_URL" | sed -E 's|.*:([0-9]+).*|\1|')
[[ "$RELAY_PORT" =~ ^[0-9]+$ ]] || RELAY_PORT=7777

cat > "$RELAY_DIR/start.sh" <<EOF
#!/bin/bash
# Pokrene clade-relay sa tokens.json iz ovog dir-a.
# Slusa na 0.0.0.0:$RELAY_PORT (sve interface-i — pristup ide kroz firewall).

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

if ! command -v clade-relay > /dev/null && [[ ! -x "$PY" ]]; then
  echo "ERROR: clade-relay nije nadjen u PATH-u niti python venv na $PY"
  echo "Install: cd $ROOT && uv venv && uv pip install -e ."
  exit 1
fi

# Sanity: tokens.json present
if [[ ! -f tokens.json ]]; then
  echo "ERROR: tokens.json fali. Setup je los."
  exit 1
fi

# Pokreni relay
exec "$PY" -m relay.main --tokens "\$HERE/tokens.json" --host 0.0.0.0 --port $RELAY_PORT
EOF
chmod +x "$RELAY_DIR/start.sh"

cat > "$RELAY_DIR/README.md" <<EOF
# Relay bundle za $PROJECT

Sluzi kao centralni dispatcher za peer-to-peer poruke (transport=relay).
Slusa na port $RELAY_PORT.

## Instalacija

Na host masini (npr. VPS sa javnim IP-em):

\`\`\`bash
# Klon repo + install
git clone https://github.com/inter-coder/clade-a2a.git ~/clade-a2a
cd ~/clade-a2a && uv venv && uv pip install -e .

# Kopiraj ovaj bundle (rsync ili scp):
scp -r relay-bundle/ user@<host>:~/clade-relay/
ssh user@<host> 'cd ~/clade-relay && ./start.sh'
\`\`\`

## Firewall

Otvori port $RELAY_PORT na host masini:

\`\`\`bash
sudo ufw allow $RELAY_PORT/tcp
\`\`\`

## Provera

\`\`\`bash
curl $RELAY_URL/health
\`\`\`

Trebao bi videti \`{"ok": true, "known_agents": [...]}\`.

## SECURITY

- \`tokens.json\` sadrzi sve bearer tokene peer-ova. **NIKAD u git.**
- Permissions 0600 (vec setovano).
- Distribuiraj peer bundle-ove peer-ovima preko sigurnih kanala (Signal, scp direkt).
EOF

ok "relay-bundle/ (start.sh + tokens.json + README.md)"

# ---- 3. Per-peer bundle ----
for ME in "${PEERS[@]}"; do
  ME_DIR="$OUT/$ME-bundle"
  mkdir -p "$ME_DIR/workdirs/$ME"

  # Per-peer port (zelimo razlicite portove ako su mozda na istoj masini — za smoke)
  ME_INDEX=-1
  for ((i=0; i<N_PEERS; i++)); do
    [[ "${PEERS[$i]}" == "$ME" ]] && ME_INDEX=$i
  done
  ME_PORT=$((PORT_START + ME_INDEX))

  # peers.yaml: self=$ME (transport=relay sa sopstvenim bearer), ostali = relay + pairwise secret
  {
    echo "# v2.2.0 cross-host peers.yaml — generisan clade-wizard-multi.sh"
    echo "# Self peer ($ME) ima bearer_token za auth ka relay-u."
    echo "# Ostali peer-ovi imaju secret_hex (pairwise HMAC, simetrican)."
    echo ""
    echo "version: 2"
    echo "self: $ME"
    echo "peers:"
    echo "  $ME:"
    echo "    transport: relay"
    echo "    role: interactive"
    echo "    relay_url: $RELAY_URL"
    echo "    bearer_token: ${BEARER[$ME]}"
    echo "    http_port: $ME_PORT"
    echo "    audit_db: ./audit.db"
    echo "    workdir: ./workdirs/$ME"
    for OTHER in "${PEERS[@]}"; do
      [[ "$OTHER" == "$ME" ]] && continue
      SECRET=$(pair_secret "$ME" "$OTHER")
      echo "  $OTHER:"
      echo "    transport: relay"
      echo "    role: interactive"
      echo "    relay_url: $RELAY_URL"
      echo "    secret_hex: $SECRET"
    done
  } > "$ME_DIR/peers.yaml"
  chmod 600 "$ME_DIR/peers.yaml"

  # .mcp.json (za interactive Claude na peer masini, ako ce se otvarati prompt)
  cat > "$ME_DIR/workdirs/$ME/.mcp.json" <<EOF
{
  "mcpServers": {
    "clade": {
      "type": "http",
      "url": "http://127.0.0.1:$ME_PORT"
    }
  }
}
EOF

  # start.sh
  cat > "$ME_DIR/start.sh" <<EOF
#!/bin/bash
# Pokrene clade serve za peer "$ME" na ovoj masini.
# peers.yaml ocekuje da clade-relay tece na $RELAY_URL.

set -e
cd "\$(dirname "\$0")"
HERE="\$(pwd)"

if [[ -f $ME.pid ]] && kill -0 \$(cat $ME.pid 2>/dev/null) 2>/dev/null; then
  echo "✓ $ME vec tece (PID \$(cat $ME.pid))"
  exit 0
fi
rm -f $ME.pid

# Provera: relay dostupan?
if ! curl -sf --max-time 3 "$RELAY_URL/health" > /dev/null 2>&1; then
  echo "⚠ Relay nije dostupan na $RELAY_URL"
  echo "  Provera: curl $RELAY_URL/health"
  echo "  Pokreni clade-relay na host masini (relay-bundle/start.sh)"
  read -rp "Nastaviti svejedno? Relay polling ce retry-ovati. [y/N] " Y
  [[ "\${Y,,}" == "y" ]] || exit 1
fi

nohup "$PY" -m clade serve --config "\$HERE/peers.yaml" --log-level INFO \\
  > $ME.log 2>&1 < /dev/null &
echo \$! > $ME.pid
sleep 2

if curl -sf "http://127.0.0.1:$ME_PORT/health" > /dev/null 2>&1; then
  echo "✓ $ME ready na :$ME_PORT (PID \$(cat $ME.pid))"
  echo ""
  echo "Otvori interactive Claude:"
  echo "  cd \$HERE/workdirs/$ME && claude"
  echo "Ili: \$HERE/chat.sh"
else
  echo "✗ $ME NIJE starovao. Log:"
  tail -15 $ME.log
  rm -f $ME.pid
  exit 1
fi
EOF
  chmod +x "$ME_DIR/start.sh"

  # stop.sh
  cat > "$ME_DIR/stop.sh" <<EOF
#!/bin/bash
cd "\$(dirname "\$0")"
if [[ -f $ME.pid ]]; then
  PID=\$(cat $ME.pid)
  if kill -TERM "\$PID" 2>/dev/null; then
    echo "✓ $ME (PID \$PID) SIGTERM..."
    for i in 1 2 3 4 5; do
      sleep 0.5
      kill -0 "\$PID" 2>/dev/null || { echo "  exited cleanly"; break; }
    done
    kill -0 "\$PID" 2>/dev/null && kill -KILL "\$PID" 2>/dev/null && echo "  SIGKILL force"
  fi
  rm -f $ME.pid
fi
EOF
  chmod +x "$ME_DIR/stop.sh"

  # status.sh
  cat > "$ME_DIR/status.sh" <<EOF
#!/bin/bash
cd "\$(dirname "\$0")"
if [[ -f $ME.pid ]] && kill -0 \$(cat $ME.pid) 2>/dev/null; then
  curl -sf http://127.0.0.1:$ME_PORT/health | "$PY" -m json.tool
else
  echo "$ME NE TECE"
fi
EOF
  chmod +x "$ME_DIR/status.sh"

  # chat.sh
  OTHERS=()
  for p in "${PEERS[@]}"; do [[ "$p" != "$ME" ]] && OTHERS+=("$p"); done
  EXAMPLE_PEER="${OTHERS[0]}"
  cat > "$ME_DIR/chat.sh" <<EOF
#!/bin/bash
cd "\$(dirname "\$0")"
if [[ ! -f $ME.pid ]] || ! kill -0 \$(cat $ME.pid 2>/dev/null) 2>/dev/null; then
  echo "$ME ne tece — pokreni ./start.sh prvo"
  exit 1
fi
cd workdirs/$ME
echo "U Claude prompt:"
echo "  > Pitaj $EXAMPLE_PEER koliko je 7 puta 8"
echo "  > Saznaj od $EXAMPLE_PEER glavni grad Francuske"
echo ""
exec claude
EOF
  chmod +x "$ME_DIR/chat.sh"

  # README.md
  cat > "$ME_DIR/README.md" <<EOF
# $ME-bundle za projekat $PROJECT

clade serve za peer "$ME" sa transport=relay (cross-host preko $RELAY_URL).

## Preduslov

1. clade-a2a instaliran na ovoj masini:
   \`\`\`bash
   git clone https://github.com/inter-coder/clade-a2a.git ~/clade-a2a
   cd ~/clade-a2a && uv venv && uv pip install -e .
   \`\`\`

2. \`clade-relay\` tece na $RELAY_URL (vidi relay-bundle/README.md).

3. \`claude\` (Claude Code CLI) instaliran + autentifikovan (samo ako ces otvarati chat.sh).

## Pokretanje

\`\`\`bash
./start.sh         # pokrene clade serve u pozadini, polluje relay /inbox/$ME
./status.sh        # health check
./chat.sh          # otvara claude u workdirs/$ME/
./stop.sh          # SIGTERM clean exit
\`\`\`

## Sta peer.yaml sadrzi

- Self ($ME): \`transport: relay\` + bearer_token za auth ka relay-u.
- Ostali peer-ovi: \`transport: relay\` + secret_hex (pairwise HMAC, simetrican
  sa identican u njihovim bundle-ima).

## SECURITY

- \`peers.yaml\` sadrzi sve secret_hex sa svim drugim peer-ovima + tvoj bearer token. **0600.**
- NIKAD u git, NIKAD u Slack/mejl. Distribucija samo preko sigurnih kanala.

## Debug

\`\`\`bash
tail -f $ME.log
curl http://127.0.0.1:$ME_PORT/health
"$PY" -m clade logs --config \$(pwd)/peers.yaml --tail 20
\`\`\`
EOF

  ok "$ME-bundle/"
done

# ---- 4. Top-level INSTRUCTIONS.md ----
PEER_BUNDLES=""
for p in "${PEERS[@]}"; do
  PEER_BUNDLES+="
  $OUT/$p-bundle/  →  scp na $p masinu (npr. ~/clade-agent/)"
done

cat > "$OUT/INSTRUCTIONS.md" <<EOF
# Clade A2A v2.2 Multi-Machine Deploy — "$PROJECT"

Generisano $(date '+%Y-%m-%d %H:%M').

## Sta je u $OUT/

\`\`\`
relay-bundle/         → scp na masinu koja ce vrteti clade-relay
                        (mora biti dostupna SVIM peer masinama na $RELAY_URL)
$PEER_BUNDLES
\`\`\`

## Koraci

### 1. Setup relay masina

\`\`\`bash
# Na masini koja ce biti relay (npr. VPS):
git clone https://github.com/inter-coder/clade-a2a.git ~/clade-a2a
cd ~/clade-a2a && uv venv && uv pip install -e .

# scp relay-bundle:
scp -r $OUT/relay-bundle/ user@<relay-host>:~/clade-relay/

# Pokreni relay:
ssh user@<relay-host> 'cd ~/clade-relay && ./start.sh'
\`\`\`

Provera: \`curl $RELAY_URL/health\` → \`{"ok": true, "known_agents": [${PEERS[*]}]}\`.

### 2. Setup svake peer masine

Ponovi za svakog peer-a (alice, bob, ...):

\`\`\`bash
# Na masini za <peer>:
git clone https://github.com/inter-coder/clade-a2a.git ~/clade-a2a
cd ~/clade-a2a && uv venv && uv pip install -e .

# scp <peer>-bundle:
scp -r $OUT/<peer>-bundle/ user@<peer-host>:~/clade-agent/

# Pokreni:
ssh user@<peer-host> 'cd ~/clade-agent && ./start.sh'
\`\`\`

### 3. Otvori chat

Na masini self peer-a ($SELF):

\`\`\`bash
cd ~/clade-agent && ./chat.sh
# > Pitaj <drugi-peer> koliko je 7 puta 8
\`\`\`

Tvoj claude → MCP HTTP → clade serve → POST relay/ask → drugi clade serve
poll-uje relay → spawn-uje claude --print → reply nazad kroz relay.

## Lokalni smoke test (sve na ovoj masini)

Ako hoces da testiras BEZ druge masine:

\`\`\`bash
# Terminal 1 — relay:
cd $OUT/relay-bundle && ./start.sh

# Terminal 2..N — po jedan terminal per peer:
EOF
for p in "${PEERS[@]}"; do
  echo "cd $OUT/$p-bundle && ./start.sh" >> "$OUT/INSTRUCTIONS.md"
done
cat >> "$OUT/INSTRUCTIONS.md" <<EOF

# Terminal $((N_PEERS + 2)) — chat:
cd $OUT/$SELF-bundle && ./chat.sh
\`\`\`

(Ovo je legit smoke jer transport=relay kroz lokalni clade-relay svejedno
testira cross-host putanje — samo network je localhost.)

## Cleanup
EOF
for p in "${PEERS[@]}"; do
  echo "  $OUT/$p-bundle/stop.sh" >> "$OUT/INSTRUCTIONS.md"
done
cat >> "$OUT/INSTRUCTIONS.md" <<EOF
  # Relay na drugoj masini: ssh user@<relay-host> 'pkill -TERM -f relay.main'

## Versionisanje

Generisano sa clade-a2a v2.2.0+ (relay polling u clade serve).
Vidi a2a-protocol.md za detalje protokola.
EOF

ok "INSTRUCTIONS.md (top-level walkthrough)"

# ---- 5. Final summary ----
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  MULTI-MACHINE SETUP GENERISAN${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "Sve u: ${CYAN}$OUT${RESET}"
echo ""
echo -e "${BOLD}Sledece:${RESET}"
echo -e "  cat ${YELLOW}$OUT/INSTRUCTIONS.md${RESET}    # pun walkthrough"
echo ""
echo -e "${BOLD}Brzi lokalni smoke (jednoj masini, simulira cross-host):${RESET}"
echo -e "  Terminal A: ${YELLOW}$OUT/relay-bundle/start.sh${RESET}"
for p in "${PEERS[@]}"; do
  echo -e "  Terminal:   ${YELLOW}$OUT/$p-bundle/start.sh${RESET}"
done
echo -e "  Chat:       ${YELLOW}$OUT/$SELF-bundle/chat.sh${RESET}"
echo ""

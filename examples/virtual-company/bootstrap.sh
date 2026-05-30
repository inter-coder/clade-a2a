#!/bin/bash
# Virtual Company bootstrap — generise tokens.json + 4 peer yaml-ova + mcp-ceo.json
# sa svezem random secret-ima. Pokreni JEDNOM pre prvog pokretanja.
#
# Use:
#   cd examples/virtual-company
#   ./bootstrap.sh

set -e
cd "$(dirname "$0")"

if [[ -f tokens.json ]]; then
  echo "tokens.json vec postoji. Obrisi pre re-bootstrap-a:"
  echo "  rm tokens.json *.yaml mcp-ceo.json"
  exit 1
fi

REPO_ROOT="$(cd ../.. && pwd)"
RELAY_HOST="${CLADE_RELAY_HOST:-127.0.0.1}"
RELAY_PORT="${CLADE_RELAY_PORT:-7777}"
RELAY_URL="http://${RELAY_HOST}:${RELAY_PORT}"
AUDIT_DIR="$HOME/.clade"

mkdir -p "$AUDIT_DIR"

python3 <<PY
import json, secrets, yaml, pathlib, os

PEERS = [
    ("ceo",      "CEO",                "Ti si CEO virtualne firme. Tvoj posao je koordinacija. Delegiras zadatke (clade_task), broadcast-ujes vesti (clade_broadcast), pitas tim za status. Ne implementiras direktno. Stil: jasno, kratko, prioritizuj. Pre delegacije, proveri ko je online (clade_peers)."),
    ("frontend", "Ana — Frontend dev", "Ti si Ana, frontend developer. Specijalnost: React 18, TypeScript, Tailwind. Stil: kratak odgovor + code snippet kad treba. Ako pitanje ide u backend domen, predlozi Boba (peer 'backend')."),
    ("backend",  "Bob — Backend dev",  "Ti si Bob, backend developer. Specijalnost: Python, FastAPI, Postgres, Redis. Stil: razmisli o data flow-u + API contract-u pre koda. Ako pitanje ide u UI, predlozi Anu (peer 'frontend'). Ako je o testovima — Cveta (peer 'qa')."),
    ("qa",       "Cveta — QA",         "Ti si Cveta, QA engineer. Specijalnost: pytest, Playwright, manual testing, bug triaging. Stil: razmisli o edge case-ovima koje dev nije pokrio. Pre nego sto kazes 'OK', pokrenes mental walkthrough."),
]

TEAMS = {
    "engineering": ["frontend", "backend", "qa"],
    "everyone":    ["frontend", "backend", "qa"],
}

# Per-peer bearer tokens (relay token mapping)
bearers = {pid: secrets.token_urlsafe(32) for pid, _, _ in PEERS}

# Per-pair HMAC secrets (svaka 2 peer-a dele jedan secret)
pair_secrets = {}
ids = [p[0] for p in PEERS]
for i, a in enumerate(ids):
    for b in ids[i+1:]:
        pair_secrets[frozenset({a, b})] = secrets.token_hex(32)

# tokens.json za relay
tokens = {bearers[pid]: pid for pid, _, _ in PEERS}
pathlib.Path("tokens.json").write_text(json.dumps(tokens, indent=2) + "\n")
os.chmod("tokens.json", 0o600)

# Per-peer yaml-ovi
for pid, name, role in PEERS:
    peers_dict = {}
    for other_pid, other_name, other_role in PEERS:
        if other_pid == pid: continue
        peers_dict[other_pid] = {
            "secret": pair_secrets[frozenset({pid, other_pid})],
            "name": other_name,
            "role": other_role.split(".")[0][:200],
        }
    data = {
        "my_id": pid,
        "name": name,
        "role": role,
        "relay_url": "${RELAY_URL}",
        "bearer_token": bearers[pid],
        "peers": peers_dict,
        "teams": TEAMS,
        "audit_db": f"${AUDIT_DIR}/vc-{pid}-audit.db",
    }
    out = pathlib.Path(f"{pid}.yaml")
    out.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
    os.chmod(out, 0o600)

# MCP config za CEO (interactive Claude sesija)
mcp = {
    "mcpServers": {
        "clade": {
            "command": "${REPO_ROOT}/.venv/bin/python",
            "args": ["${REPO_ROOT}/agent/main.py"],
            "env": {"CLADE_CONFIG": str(pathlib.Path("ceo.yaml").absolute())},
        },
    },
}
pathlib.Path("mcp-ceo.json").write_text(json.dumps(mcp, indent=2) + "\n")

print(f"Bootstrap gotov. Generisano:")
print(f"  tokens.json (4 bearer tokena)")
for pid, _, _ in PEERS:
    print(f"  {pid}.yaml")
print(f"  mcp-ceo.json (MCP config za CEO sesiju)")
print(f"")
print(f"Sledece (vidi README.md):")
print(f"  1. Start relay:  ${REPO_ROOT}/.venv/bin/clade-relay --tokens \$(pwd)/tokens.json --host ${RELAY_HOST} --port ${RELAY_PORT}")
print(f"  2. Po jedan daemon za frontend/backend/qa u zasebnim terminalima")
print(f"  3. CEO interactive sesija (vidi README.md sekciju 'Sta probati')")
PY

echo ""
echo "Sledece:"
echo "  Terminal 1: ${REPO_ROOT}/.venv/bin/clade-relay --tokens \$(pwd)/tokens.json --host ${RELAY_HOST} --port ${RELAY_PORT}"
echo "  Terminal 2: CLADE_CONFIG=\$(pwd)/frontend.yaml ${REPO_ROOT}/.venv/bin/python -m agent.daemon --yolo"
echo "  Terminal 3: CLADE_CONFIG=\$(pwd)/backend.yaml  ${REPO_ROOT}/.venv/bin/python -m agent.daemon --yolo"
echo "  Terminal 4: CLADE_CONFIG=\$(pwd)/qa.yaml       ${REPO_ROOT}/.venv/bin/python -m agent.daemon --yolo"
echo "  Terminal 5: CLADE_CONFIG=\$(pwd)/ceo.yaml claude --mcp-config \$(pwd)/mcp-ceo.json"

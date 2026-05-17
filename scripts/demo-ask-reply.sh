#!/bin/bash
# End-to-end demo: Alice salje ask Bob-u, Bob odgovara, Alice dobija response.
# Headless mode (claude --print + --dangerously-skip-permissions).
# Pretpostavlja da relay vec radi na localhost:7777.

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ALICE_DIR="${TMPDIR:-/tmp}/clade-alice-demo"
BOB_DIR="${TMPDIR:-/tmp}/clade-bob-demo"

mkdir -p "$ALICE_DIR" "$BOB_DIR"
cp "$ROOT/examples/mcp-config-alice.json" "$ALICE_DIR/.mcp.json"
cp "$ROOT/examples/mcp-config-bob.json" "$BOB_DIR/.mcp.json"
cp "$ROOT/examples/CLAUDE.md.template" "$ALICE_DIR/CLAUDE.md"
cp "$ROOT/examples/CLAUDE.md.template" "$BOB_DIR/CLAUDE.md"

# Check relay
if ! curl -sf --max-time 2 http://localhost:7777/health > /dev/null; then
  echo "ERROR: Relay nije pokrenut. Pokreni './scripts/start-relay.sh' u drugom terminalu."
  exit 1
fi

echo "=== [1/3] Alice startuje clade_ask u pozadini ==="
cd "$ALICE_DIR"
ALICE_LOG="${TMPDIR:-/tmp}/clade-alice-demo.log"
nohup claude --print --dangerously-skip-permissions --mcp-config .mcp.json -- \
  "Pozovi clade_ask sa: to='bob', payload={'question': 'koliko je 7 puta 8'}, timeout_s=90. Cekaj odgovor i prikazi sta tool vraca kao JSON." \
  > "$ALICE_LOG" 2>&1 &
ALICE_PID=$!
echo "  Alice PID=$ALICE_PID, log=$ALICE_LOG"

echo "=== [2/3] Cekam 8s da Alice efektivno firova ask ==="
sleep 8
curl -s http://localhost:7777/health | python3 -m json.tool

echo ""
echo "=== [3/3] Bob polluje inbox i odgovara ==="
cd "$BOB_DIR"
claude --print --dangerously-skip-permissions --mcp-config .mcp.json -- \
  "1) Pozovi clade_inbox. 2) Ako vidis kind='ask' poruku, izvuci correlation_id i payload.question. 3) Izracunaj odgovor i pozovi clade_reply(correlation_id=<id>, response={'answer': '<odgovor>'}). 4) Reci mi sta si uradio."

echo ""
echo "=== Cekam Alice da zavrsi ==="
wait $ALICE_PID
echo ""
echo "=== Alice output ==="
cat "$ALICE_LOG"

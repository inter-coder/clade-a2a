#!/usr/bin/env bash
# End-to-end check of the AITF (AI Team Framework) integration in Clade A2A.
#
# Flow:
#   1. bootstrap.sh -> synthetic AITF project at $TARGET (default /tmp/aitf-demo-e2e)
#   2. spawn setup-server on port $SETUP_PORT with its own data dir
#   3. POST /api/setup/import-aitf with $TARGET
#   4. verify response shape + generated yamls on disk + relay /health
#   5. add a commit to the AITF repo; exercise one scribe tick in-process
#      (with a stubbed _call_claude_scribe — NEVER spawns real Claude).
#   6. clean up everything
#
# Defaults:
#   TARGET=/tmp/aitf-demo-e2e
#   SETUP_PORT=18765
#   RELAY_PORT=17780
#   SETUP_DATA_DIR=/tmp/aitf-demo-setup
#   DOC_AUDIT_DIR=/tmp/aitf-demo-e2e-doc
#
# Override with env vars if those ports collide with anything you're running.

set -euo pipefail

TARGET="${TARGET:-/tmp/aitf-demo-e2e}"
SETUP_PORT="${SETUP_PORT:-18765}"
RELAY_PORT="${RELAY_PORT:-17780}"
SETUP_DATA_DIR="${SETUP_DATA_DIR:-/tmp/aitf-demo-setup}"
DOC_AUDIT_DIR="${DOC_AUDIT_DIR:-/tmp/aitf-demo-e2e-doc}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
SETUP_SERVER_BIN="$REPO_ROOT/.venv/bin/clade-setup-server"

if [[ ! -x "$VENV_PY" ]]; then
  echo "✗ $VENV_PY not found — run 'uv venv && uv pip install -e .' first" >&2
  exit 1
fi

SETUP_LOG=$(mktemp -t aitf-demo-setup.log.XXXXXX)
SETUP_PID=""
RELAY_PID_FILE=""

cleanup() {
  if [[ -n "$SETUP_PID" ]] && kill -0 "$SETUP_PID" 2>/dev/null; then
    kill "$SETUP_PID" 2>/dev/null || true
    wait "$SETUP_PID" 2>/dev/null || true
  fi
  if [[ -n "$RELAY_PID_FILE" && -f "$RELAY_PID_FILE" ]]; then
    local rp
    rp=$(cat "$RELAY_PID_FILE")
    if kill -0 "$rp" 2>/dev/null; then
      kill "$rp" 2>/dev/null || true
    fi
  fi
  rm -rf "$TARGET" "$SETUP_DATA_DIR" "$DOC_AUDIT_DIR" "$SETUP_LOG"
}
trap cleanup EXIT

# ---- Step 1: build demo AITF project ----
echo "── Step 1: bootstrap demo AITF project"
TARGET="$TARGET" "$SCRIPT_DIR/bootstrap.sh"

# ---- Step 2: start setup-server in background ----
echo "── Step 2: start setup-server on port $SETUP_PORT"
rm -rf "$SETUP_DATA_DIR" && mkdir -p "$SETUP_DATA_DIR"
"$SETUP_SERVER_BIN" \
  --port "$SETUP_PORT" \
  --data-dir "$SETUP_DATA_DIR" \
  --public-url "http://127.0.0.1:$SETUP_PORT" \
  > "$SETUP_LOG" 2>&1 &
SETUP_PID=$!

until curl -sf "http://127.0.0.1:$SETUP_PORT/health" > /dev/null 2>&1; do
  if ! kill -0 "$SETUP_PID" 2>/dev/null; then
    echo "✗ setup-server died on startup:"; cat "$SETUP_LOG" >&2; exit 1
  fi
  sleep 0.2
done
echo "✓ setup-server up (PID $SETUP_PID)"

# ---- Step 3: POST import-aitf ----
echo "── Step 3: POST /api/setup/import-aitf"
RESP=$(curl -sS -X POST "http://127.0.0.1:$SETUP_PORT/api/setup/import-aitf" \
  -H 'Content-Type: application/json' \
  -d "{
    \"project_path\": \"$TARGET\",
    \"relay_host\": \"127.0.0.1\",
    \"relay_url_host\": \"127.0.0.1\",
    \"relay_port\": $RELAY_PORT,
    \"start_relay\": true
  }")

PROJECT_TOKEN=$(echo "$RESP" | "$VENV_PY" -c "import sys, json; print(json.load(sys.stdin)['project_token'])")
DOC_ENABLED=$(echo "$RESP" | "$VENV_PY" -c "import sys, json; print(json.load(sys.stdin)['doc_optimizer_enabled'])")
PEER_COUNT=$(echo "$RESP" | "$VENV_PY" -c "import sys, json; print(len(json.load(sys.stdin)['peers']))")
RELAY_PID_FILE="$SETUP_DATA_DIR/$PROJECT_TOKEN/relay.pid"

[[ "$DOC_ENABLED" == "True" ]] || { echo "✗ expected doc_optimizer_enabled=True, got $DOC_ENABLED"; exit 1; }
[[ "$PEER_COUNT" == "4" ]] || { echo "✗ expected 4 peers, got $PEER_COUNT"; exit 1; }
echo "✓ import returned 4 peers, doc_optimizer_enabled=True (token: $PROJECT_TOKEN)"

# ---- Step 4: verify yamls on disk ----
echo "── Step 4: verify generated yamls"
"$VENV_PY" - <<PY
import sys, yaml
from pathlib import Path
pd = Path("$SETUP_DATA_DIR/$PROJECT_TOKEN")
target = "$TARGET"
expected = {"pd", "dd", "team", "doc"}
got = {p.stem for p in pd.glob("*.yaml")}
assert got == expected, f"expected {expected} yamls, got {got}"

# doc has scribe; others don't
for pid in expected:
    d = yaml.safe_load((pd / f"{pid}.yaml").read_text())
    has_scribe = "scribe" in d
    if pid == "doc":
        assert has_scribe, "doc.yaml missing scribe block"
        s = d["scribe"]
        assert s["enabled"] is True
        assert s["interval_minutes"] == 60
        assert s["max_rounds_per_day"] == 24
        assert s["docs_repo_path"] == target
    else:
        assert not has_scribe, f"{pid}.yaml should NOT have scribe block"
    assert d["extra_add_dirs"] == [target], f"{pid}.yaml extra_add_dirs wrong: {d['extra_add_dirs']}"
    other_peers = set(d["peers"].keys())
    assert other_peers == expected - {pid}, f"{pid}.yaml peers wrong: {other_peers}"

# Role content actually inlined from docs/TEAM/*.md
team = yaml.safe_load((pd / "team.yaml").read_text())
assert "Development Team" in team["role"], "team.yaml role not inlined from .md"
assert "task-tracker" in team["role"], "team.yaml role missing project name"
print("✓ all 4 yamls structurally correct (scribe only on doc, peers/extra_add_dirs correct, role inlined)")
PY

# ---- Step 5: verify relay alive + result page renders ----
echo "── Step 5: relay health + result page"
# Setup-server spawns the relay subprocess asynchronously; wait until it binds.
DEADLINE=$((SECONDS + 15))
until curl -sf "http://127.0.0.1:$RELAY_PORT/health" > /dev/null 2>&1; do
  if (( SECONDS >= DEADLINE )); then
    echo "✗ relay never came up on port $RELAY_PORT"; exit 1
  fi
  sleep 0.3
done
RELAY_HEALTH=$(curl -sS "http://127.0.0.1:$RELAY_PORT/health")
KNOWN_AGENTS=$(echo "$RELAY_HEALTH" | "$VENV_PY" -c "import sys, json; d=json.load(sys.stdin); print(','.join(sorted(d['known_agents'])))")
[[ "$KNOWN_AGENTS" == "dd,doc,pd,team" ]] || { echo "✗ relay known_agents wrong: $KNOWN_AGENTS"; exit 1; }
echo "✓ relay knows all 4 agents"

HTTP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$SETUP_PORT/setup/$PROJECT_TOKEN")
[[ "$HTTP_CODE" == "200" ]] || { echo "✗ result page returned $HTTP_CODE"; exit 1; }
echo "✓ result page renders (HTTP 200)"

# ---- Step 6: exercise scribe tick in-process (no real claude spawn) ----
echo "── Step 6: scribe tick in-process (stubbed _call_claude_scribe)"
# Redirect doc audit_db to a tmp dir so we don't pollute ~/.clade
"$VENV_PY" - <<PY
import yaml
p = "$SETUP_DATA_DIR/$PROJECT_TOKEN/doc.yaml"
d = yaml.safe_load(open(p))
d["audit_db"] = "$DOC_AUDIT_DIR/audit.db"
open(p, "w").write(yaml.dump(d, default_flow_style=False, allow_unicode=True, sort_keys=False))
PY
mkdir -p "$DOC_AUDIT_DIR"

CLADE_CONFIG="$SETUP_DATA_DIR/$PROJECT_TOKEN/doc.yaml" "$VENV_PY" - <<PY
import asyncio, subprocess, sys
from pathlib import Path
import agent.main as m
import agent.daemon as d

cfg = m.cfg
repo = d._scribe_resolve_repo(cfg)
assert repo == Path("$TARGET").resolve(), f"resolved repo wrong: {repo}"

async def run():
    # Stub the spawn so no real claude --print is invoked.
    spawned = {"calls": 0, "last_prompt": ""}
    async def fake_claude(prompt, workdir, cfg_, dangerous):
        spawned["calls"] += 1
        spawned["last_prompt"] = prompt
        return "[stub: scribe round done]"
    d._call_claude_scribe = fake_claude

    # First tick: empty state, last_sha=None -> should spawn.
    state = d._scribe_load_state(cfg)
    assert state == {"last_head_sha": None, "rounds_today": []}, state
    workdir = Path("$DOC_AUDIT_DIR/wd"); workdir.mkdir(exist_ok=True)
    r1 = await d._scribe_tick(cfg, workdir, False, state)
    assert r1 is True, "first tick should spawn (initial state)"
    assert spawned["calls"] == 1
    head1 = state["last_head_sha"]
    assert head1 is not None

    # Second tick: HEAD unchanged -> cheap exit.
    r2 = await d._scribe_tick(cfg, workdir, False, d._scribe_load_state(cfg))
    assert r2 is False, "second tick should cheap-exit (no commits)"
    assert spawned["calls"] == 1, "spawn count must NOT increase on cheap exit"

    # Add a new commit. Third tick should spawn again.
    subprocess.run(["sh", "-c",
        "echo '## D001' > docs/TEAM/DIRECTIVES/001-mvp.md && git add . && git commit -q -m 'PD: D001'"
    ], cwd="$TARGET", check=True)
    r3 = await d._scribe_tick(cfg, workdir, False, d._scribe_load_state(cfg))
    assert r3 is True, "third tick should spawn (new commit)"
    assert spawned["calls"] == 2
    assert "001-mvp.md" in spawned["last_prompt"] or "PD: D001" in spawned["last_prompt"], \
        "third-tick prompt missing the new commit"

    final = d._scribe_load_state(cfg)
    assert final["last_head_sha"] != head1, "state SHA should have advanced"
    assert len(final["rounds_today"]) == 2, f"expected 2 rounds, got {len(final['rounds_today'])}"
    print("✓ scribe tick: first spawns, second cheap-exits, third spawns on new commit")

asyncio.run(run())
PY

echo
echo "✅ all checks passed"

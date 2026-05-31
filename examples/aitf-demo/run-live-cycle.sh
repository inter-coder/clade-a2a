#!/usr/bin/env bash
# LIVE 5-spawn AITF cycle with REAL `claude --print` calls.
# Burns real API tokens (~$3-5). Takes 5-15 min wall time.
#
# Flow:
#   1. bootstrap.sh -> /tmp/aitf-demo-live  (clean AITF scaffold)
#   2. setup-server + POST /api/setup/import-aitf (4 peers, relay spawned)
#   3. Each peer's yaml: redirect audit_db to /tmp/aitf-live-<peer>/, override
#      claude timeout to 300s (Team's implementation step needs > 90s).
#   4. CEO seeds the first DIRECTIVE (no spawn).
#   5. Sequentially trigger scribe ticks via Python subprocess:
#        DD   → break directive into TODO
#        Team → implement first TODO, write report
#        DD   → verdict the report
#        PD   → update PROJECT_STATUS
#        DOC  → optimize documentation
#      Each tick spawns real `claude --print --dangerously-skip-permissions`
#      with the peer's yaml as the active config. After each, dump git log
#      + diff of changed files so we see what happened.
#   6. Print final summary.
#   7. Cleanup (setup-server, relay, tmp dirs).
#
# Override via env: TARGET, SETUP_PORT, RELAY_PORT, SKIP_CLEANUP.

set -euo pipefail

TARGET="${TARGET:-/tmp/aitf-demo-live}"
SETUP_PORT="${SETUP_PORT:-18766}"
RELAY_PORT="${RELAY_PORT:-17781}"
SETUP_DATA_DIR="${SETUP_DATA_DIR:-/tmp/aitf-live-setup}"
SKIP_CLEANUP="${SKIP_CLEANUP:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
SETUP_SERVER_BIN="$REPO_ROOT/.venv/bin/clade-setup-server"

if [[ ! -x "$VENV_PY" ]]; then
  echo "✗ $VENV_PY not found — run 'uv venv && uv pip install -e .' first" >&2; exit 1
fi
if ! command -v claude > /dev/null 2>&1; then
  echo "✗ claude CLI not found — install Claude Code first" >&2; exit 1
fi

SETUP_LOG=$(mktemp -t aitf-live-setup.log.XXXXXX)
SETUP_PID=""
RELAY_PID_FILE=""

cleanup() {
  echo
  echo "── Cleanup"
  if [[ -n "$SETUP_PID" ]] && kill -0 "$SETUP_PID" 2>/dev/null; then
    kill "$SETUP_PID" 2>/dev/null || true
    wait "$SETUP_PID" 2>/dev/null || true
    echo "  stopped setup-server (PID $SETUP_PID)"
  fi
  if [[ -n "$RELAY_PID_FILE" && -f "$RELAY_PID_FILE" ]]; then
    local rp; rp=$(cat "$RELAY_PID_FILE")
    if kill -0 "$rp" 2>/dev/null; then kill "$rp" 2>/dev/null || true; echo "  stopped relay (PID $rp)"; fi
  fi
  if [[ "$SKIP_CLEANUP" != "1" ]]; then
    rm -rf "$TARGET" "$SETUP_DATA_DIR" "$SETUP_LOG" /tmp/aitf-live-pd /tmp/aitf-live-dd /tmp/aitf-live-team /tmp/aitf-live-doc /tmp/aitf-live-wd-*
    echo "  removed tmp dirs"
  else
    echo "  SKIP_CLEANUP=1 — kept $TARGET, $SETUP_DATA_DIR, /tmp/aitf-live-*"
  fi
}
trap cleanup EXIT

# ---- Step 1: bootstrap demo AITF project ----
echo "════════════════════════════════════════════════════════════════"
echo "  STEP 1: bootstrap demo AITF project at $TARGET"
echo "════════════════════════════════════════════════════════════════"
TARGET="$TARGET" "$SCRIPT_DIR/bootstrap.sh"

# Seed a minimal pyproject so pytest doesn't trivially fail when Team runs it.
cat > "$TARGET/pyproject.toml" <<'TOML'
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "task-tracker"
version = "0.0.0"
requires-python = ">=3.10"

[tool.hatch.build.targets.wheel]
packages = ["src"]
TOML
mkdir -p "$TARGET/src" "$TARGET/tests"
touch "$TARGET/src/__init__.py" "$TARGET/tests/__init__.py"
git -C "$TARGET" add . && git -C "$TARGET" commit -q -m "scaffold: pyproject + empty src/tests"

# ---- Step 2: setup-server + import ----
echo
echo "════════════════════════════════════════════════════════════════"
echo "  STEP 2: setup-server up + POST /api/setup/import-aitf"
echo "════════════════════════════════════════════════════════════════"
rm -rf "$SETUP_DATA_DIR" && mkdir -p "$SETUP_DATA_DIR"
"$SETUP_SERVER_BIN" --port "$SETUP_PORT" --data-dir "$SETUP_DATA_DIR" \
    --public-url "http://127.0.0.1:$SETUP_PORT" > "$SETUP_LOG" 2>&1 &
SETUP_PID=$!
until curl -sf "http://127.0.0.1:$SETUP_PORT/health" > /dev/null 2>&1; do
  kill -0 "$SETUP_PID" 2>/dev/null || { echo "setup-server died:"; cat "$SETUP_LOG"; exit 1; }
  sleep 0.2
done
echo "  setup-server up (PID $SETUP_PID)"

RESP=$(curl -sS -X POST "http://127.0.0.1:$SETUP_PORT/api/setup/import-aitf" \
  -H 'Content-Type: application/json' \
  -d "{\"project_path\":\"$TARGET\",\"relay_host\":\"127.0.0.1\",\"relay_url_host\":\"127.0.0.1\",\"relay_port\":$RELAY_PORT,\"start_relay\":true}")
PROJECT_TOKEN=$(echo "$RESP" | "$VENV_PY" -c "import sys,json; print(json.load(sys.stdin)['project_token'])")
RELAY_PID_FILE="$SETUP_DATA_DIR/$PROJECT_TOKEN/relay.pid"
echo "  imported (token: $PROJECT_TOKEN)"

# Wait for relay to bind
until curl -sf "http://127.0.0.1:$RELAY_PORT/health" > /dev/null 2>&1; do sleep 0.2; done
echo "  relay up on port $RELAY_PORT"

# ---- Step 3: per-peer audit_db redirect ----
echo
echo "════════════════════════════════════════════════════════════════"
echo "  STEP 3: redirect each peer's audit_db to /tmp/aitf-live-<peer>/"
echo "════════════════════════════════════════════════════════════════"
for r in pd dd team doc; do
  mkdir -p "/tmp/aitf-live-$r"
  "$VENV_PY" - <<PY
import yaml
p = "$SETUP_DATA_DIR/$PROJECT_TOKEN/$r.yaml"
d = yaml.safe_load(open(p))
d["audit_db"] = "/tmp/aitf-live-$r/audit.db"
open(p, "w").write(yaml.dump(d, default_flow_style=False, allow_unicode=True, sort_keys=False))
print(f"  $r.yaml -> audit_db=/tmp/aitf-live-$r/audit.db, scribe={'enabled' if d.get('scribe',{}).get('enabled') else 'off'}")
PY
done

# ---- Step 4: CEO seed — first DIRECTIVE ----
echo
echo "════════════════════════════════════════════════════════════════"
echo "  STEP 4: CEO seeds the first directive"
echo "════════════════════════════════════════════════════════════════"
cat > "$TARGET/docs/TEAM/DIRECTIVES/001-add-command.md" <<'MD'
# Directive 001 — Implement `add` command

Date: 2026-05-31
Phase: 1
Issued by: CEO (simulating Project Director seed)
Status: open

## Strategic Intent
Phase 1 MVP needs an `add` command that takes a task description and writes it
to a local SQLite store. Smallest possible useful slice — no list, no done yet.

## Scope
In: `task-tracker add "<text>"` writes a row to `~/.task-tracker/tasks.db`.
Out: list, done, delete. Those are follow-up directives.

## Acceptance Criteria
- Running `task-tracker add "buy milk"` exits 0 and creates a row in tasks table.
- A pytest test in `tests/test_add.py` covers the happy path.
- Uses Typer + sqlite-stdlib (no extra deps).

## Handoff
DD should translate this into 2-3 TODO items.
MD
git -C "$TARGET" add . && git -C "$TARGET" commit -q -m "CEO: D001 — implement add command"
echo "  D001 committed:"
git -C "$TARGET" log --oneline | head -3 | sed 's/^/    /'

# Helper: trigger one scribe tick for the named role.
trigger_role() {
  local ROLE="$1" STEP_NAME="$2"
  local WD="/tmp/aitf-live-wd-$ROLE-$(date +%s)"
  mkdir -p "$WD"
  local PRE_SHA; PRE_SHA=$(git -C "$TARGET" rev-parse HEAD)
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "  $STEP_NAME — trigger $ROLE scribe tick (REAL claude --print)"
  echo "  pre-tick HEAD: ${PRE_SHA:0:12}"
  echo "  budget: up to 300s, spend ~\$0.30-1.50 in tokens"
  echo "════════════════════════════════════════════════════════════════"
  CLADE_CONFIG="$SETUP_DATA_DIR/$PROJECT_TOKEN/$ROLE.yaml" "$VENV_PY" - <<PY
import asyncio, sys
from pathlib import Path
import agent.main as m
import agent.daemon as d

# Bump claude --print timeout (default 90s is too short for Team's implementation)
d.CLAUDE_TIMEOUT_S = 300

cfg = m.cfg
print(f"  cfg loaded: my_id={cfg.my_id}, scribe.interval={cfg.scribe.interval_minutes}min, repo={d._scribe_resolve_repo(cfg)}", file=sys.stderr)

async def run():
    state = d._scribe_load_state(cfg)
    workdir = Path("$WD")
    # Ensure .mcp.json + minimal settings in workdir so spawned claude has MCP access
    d.write_mcp_config(workdir, cfg)
    d.write_minimal_settings(workdir)
    spawned = await d._scribe_tick(cfg, workdir, dangerous=True, state=state)
    print(f"  tick spawned: {spawned}", file=sys.stderr)

asyncio.run(run())
PY
  local POST_SHA; POST_SHA=$(git -C "$TARGET" rev-parse HEAD)
  echo
  if [[ "$PRE_SHA" == "$POST_SHA" ]]; then
    echo "  ⚠ no new commits — $ROLE decided this round had no work, OR claude failed silently"
  else
    echo "  ✓ new commits since tick:"
    git -C "$TARGET" log --oneline "${PRE_SHA}..${POST_SHA}" | sed 's/^/    /'
    echo "  files changed:"
    git -C "$TARGET" diff --stat "$PRE_SHA" "$POST_SHA" | sed 's/^/    /'
  fi
}

# ---- Step 5: trigger the cycle ----
trigger_role dd   "STEP 5 — DD: break directive into TODO"
trigger_role team "STEP 6 — Team: implement first TODO"
trigger_role dd   "STEP 7 — DD: verdict the report"
trigger_role pd   "STEP 8 — PD: update PROJECT_STATUS"
trigger_role doc  "STEP 9 — DOC: documentation optimization round"

# ---- Final summary ----
echo
echo "════════════════════════════════════════════════════════════════"
echo "  FINAL STATE"
echo "════════════════════════════════════════════════════════════════"
echo
echo "── git log (newest first):"
git -C "$TARGET" log --oneline | head -15 | sed 's/^/  /'
echo
echo "── TODO.md:"
sed 's/^/  /' "$TARGET/docs/TEAM/TODO.md"
echo
echo "── PROJECT_STATUS.md:"
sed 's/^/  /' "$TARGET/docs/TEAM/PROJECT_STATUS.md"
echo
echo "── REPORTS:"
ls "$TARGET/docs/TEAM/REPORTS/" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
echo
echo "── src/ tree:"
find "$TARGET/src" -type f 2>/dev/null | sed 's/^/  /'
echo
echo "── OPTIMIZATION_LOG.md (DOC output):"
sed 's/^/  /' "$TARGET/docs/TEAM/OPTIMIZATION_LOG.md"
echo
echo "════════════════════════════════════════════════════════════════"
echo "  ✅ live cycle complete"
echo "════════════════════════════════════════════════════════════════"

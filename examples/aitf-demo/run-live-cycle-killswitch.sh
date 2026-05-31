#!/usr/bin/env bash
# Same 5-spawn AITF cycle as run-live-cycle.sh, but interleaved with TWO
# kill-switch demonstrations to prove pause/resume work on the real daemon
# code path (not just the unit-test stub).
#
# Flow (NEW steps marked):
#   1-4 same as run-live-cycle.sh (bootstrap → import → audit_db redirect → CEO seed)
#   5 DD breakdown — real spawn (~30s)
#   6 Team implement — real spawn (~120s)
#  ─── GLOBAL pause demo (NEW) ───
#   7a clade-pause; trigger DD verdict → must cheap-exit, NO claude spawn
#   7b clade-resume; trigger DD verdict → real spawn (~40s)
#  ─── per-peer pause demo (NEW) ───
#   8a clade-pause --peer pd; trigger PD → must cheap-exit
#   8b clade-resume --peer pd; trigger PD → real spawn (may decide no-op)
#   9 DOC round — real spawn (~60s)
#
# Cost ~$3-5 (same as run-live-cycle.sh — cheap-exits add no token spend).
# Wall time ~8-10 min.

set -euo pipefail

TARGET="${TARGET:-/tmp/aitf-demo-killswitch}"
SETUP_PORT="${SETUP_PORT:-18767}"
RELAY_PORT="${RELAY_PORT:-17782}"
SETUP_DATA_DIR="${SETUP_DATA_DIR:-/tmp/aitf-killswitch-setup}"
SKIP_CLEANUP="${SKIP_CLEANUP:-0}"

# All daemon ticks AND clade-pause/resume CLI use this dir for sentinels.
export CLADE_PAUSE_DIR="${CLADE_PAUSE_DIR:-/tmp/aitf-killswitch-pause}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
SETUP_SERVER_BIN="$REPO_ROOT/.venv/bin/clade-setup-server"
CLADE_PAUSE_BIN="$REPO_ROOT/.venv/bin/clade-pause"
CLADE_RESUME_BIN="$REPO_ROOT/.venv/bin/clade-resume"

if [[ ! -x "$VENV_PY" ]]; then
  echo "✗ $VENV_PY not found — run 'uv venv && uv pip install -e .' first" >&2; exit 1
fi
if [[ ! -x "$CLADE_PAUSE_BIN" || ! -x "$CLADE_RESUME_BIN" ]]; then
  echo "✗ clade-pause/clade-resume not found — re-run 'uv pip install -e .'" >&2; exit 1
fi
if ! command -v claude > /dev/null 2>&1; then
  echo "✗ claude CLI not found" >&2; exit 1
fi

SETUP_LOG=$(mktemp -t aitf-killswitch-setup.log.XXXXXX)
SETUP_PID=""
RELAY_PID_FILE=""

cleanup() {
  echo
  echo "── Cleanup"
  if [[ -n "$SETUP_PID" ]] && kill -0 "$SETUP_PID" 2>/dev/null; then
    kill "$SETUP_PID" 2>/dev/null || true; wait "$SETUP_PID" 2>/dev/null || true
    echo "  stopped setup-server (PID $SETUP_PID)"
  fi
  if [[ -n "$RELAY_PID_FILE" && -f "$RELAY_PID_FILE" ]]; then
    local rp; rp=$(cat "$RELAY_PID_FILE")
    if kill -0 "$rp" 2>/dev/null; then kill "$rp" 2>/dev/null || true; echo "  stopped relay (PID $rp)"; fi
  fi
  if [[ "$SKIP_CLEANUP" != "1" ]]; then
    rm -rf "$TARGET" "$SETUP_DATA_DIR" "$SETUP_LOG" "$CLADE_PAUSE_DIR" \
           /tmp/aitf-killswitch-pd /tmp/aitf-killswitch-dd /tmp/aitf-killswitch-team /tmp/aitf-killswitch-doc \
           /tmp/aitf-killswitch-wd-*
    echo "  removed tmp dirs"
  fi
}
trap cleanup EXIT

# ---- helpers ----

banner() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "════════════════════════════════════════════════════════════════"
}

# trigger_role <role> "<banner_text>" <expect_spawn_yes|expect_spawn_no_paused>
trigger_role() {
  local ROLE="$1" STEP_NAME="$2" EXPECT="$3"
  local WD="/tmp/aitf-killswitch-wd-$ROLE-$(date +%s%N)"
  mkdir -p "$WD"
  local PRE_SHA; PRE_SHA=$(git -C "$TARGET" rev-parse HEAD)
  banner "$STEP_NAME"
  echo "  role=$ROLE  expect=$EXPECT  pre-HEAD=${PRE_SHA:0:12}"
  echo "  pause sentinels in $CLADE_PAUSE_DIR:"
  ls -1 "$CLADE_PAUSE_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (none)"
  local START_TS; START_TS=$(date +%s)
  CLADE_CONFIG="$SETUP_DATA_DIR/$PROJECT_TOKEN/$ROLE.yaml" CLADE_PAUSE_DIR="$CLADE_PAUSE_DIR" "$VENV_PY" - <<PY
import asyncio, sys
from pathlib import Path
import agent.main as m
import agent.daemon as d
d.CLAUDE_TIMEOUT_S = 300
cfg = m.cfg

async def run():
    state = d._scribe_load_state(cfg)
    workdir = Path("$WD")
    d.write_mcp_config(workdir, cfg)
    d.write_minimal_settings(workdir)
    spawned = await d._scribe_tick(cfg, workdir, dangerous=True, state=state)
    print(f"TICK_RESULT: spawned={spawned}", file=sys.stderr)
asyncio.run(run())
PY
  local POST_SHA; POST_SHA=$(git -C "$TARGET" rev-parse HEAD)
  local ELAPSED=$(( $(date +%s) - START_TS ))
  echo "  elapsed: ${ELAPSED}s"
  if [[ "$PRE_SHA" == "$POST_SHA" ]]; then
    if [[ "$EXPECT" == "expect_spawn_no_paused" ]]; then
      echo "  ✓ no new commits (expected — paused or no-op decision)"
    else
      echo "  ⚠ no new commits — role decided this round had no work"
    fi
  else
    echo "  ✓ new commits since tick:"
    git -C "$TARGET" log --oneline "${PRE_SHA}..${POST_SHA}" | sed 's/^/    /'
    git -C "$TARGET" diff --stat "$PRE_SHA" "$POST_SHA" | sed 's/^/    /'
  fi
}

# ---- Step 1: bootstrap ----
banner "STEP 1: bootstrap demo AITF project"
TARGET="$TARGET" "$SCRIPT_DIR/bootstrap.sh"

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
banner "STEP 2: setup-server + import-aitf"
rm -rf "$SETUP_DATA_DIR" && mkdir -p "$SETUP_DATA_DIR"
"$SETUP_SERVER_BIN" --port "$SETUP_PORT" --data-dir "$SETUP_DATA_DIR" \
    --public-url "http://127.0.0.1:$SETUP_PORT" > "$SETUP_LOG" 2>&1 &
SETUP_PID=$!
until curl -sf "http://127.0.0.1:$SETUP_PORT/health" > /dev/null 2>&1; do
  kill -0 "$SETUP_PID" 2>/dev/null || { echo "setup-server died:"; cat "$SETUP_LOG"; exit 1; }; sleep 0.2
done
RESP=$(curl -sS -X POST "http://127.0.0.1:$SETUP_PORT/api/setup/import-aitf" \
  -H 'Content-Type: application/json' \
  -d "{\"project_path\":\"$TARGET\",\"relay_host\":\"127.0.0.1\",\"relay_url_host\":\"127.0.0.1\",\"relay_port\":$RELAY_PORT,\"start_relay\":true}")
PROJECT_TOKEN=$(echo "$RESP" | "$VENV_PY" -c "import sys,json; print(json.load(sys.stdin)['project_token'])")
RELAY_PID_FILE="$SETUP_DATA_DIR/$PROJECT_TOKEN/relay.pid"
until curl -sf "http://127.0.0.1:$RELAY_PORT/health" > /dev/null 2>&1; do sleep 0.2; done
echo "  imported (token: $PROJECT_TOKEN); relay up"

# ---- Step 3: redirect audit_db ----
banner "STEP 3: redirect each peer's audit_db"
for r in pd dd team doc; do
  mkdir -p "/tmp/aitf-killswitch-$r"
  "$VENV_PY" - <<PY
import yaml
p = "$SETUP_DATA_DIR/$PROJECT_TOKEN/$r.yaml"
d = yaml.safe_load(open(p))
d["audit_db"] = "/tmp/aitf-killswitch-$r/audit.db"
open(p, "w").write(yaml.dump(d, default_flow_style=False, allow_unicode=True, sort_keys=False))
PY
  echo "  $r → /tmp/aitf-killswitch-$r/audit.db"
done

# ---- Step 4: CEO seed ----
banner "STEP 4: CEO seeds first directive"
cat > "$TARGET/docs/TEAM/DIRECTIVES/001-add-command.md" <<'MD'
# Directive 001 — Implement `add` command

Date: 2026-05-31
Phase: 1
Issued by: CEO
Status: open

## Strategic Intent
Phase 1 MVP — `task-tracker add "<text>"` writes to local SQLite store.

## Scope
In: add command + minimal SQLite store.
Out: list, done, delete (future directives).

## Acceptance Criteria
- `task-tracker add "buy milk"` exits 0, row exists in `~/.task-tracker/tasks.db`.
- Pytest covers the happy path.
- Uses Typer + sqlite-stdlib only.

## Handoff
DD breaks this into TODO items.
MD
git -C "$TARGET" add . && git -C "$TARGET" commit -q -m "CEO: D001 — implement add command"

# Make sure no leftover sentinels from a previous run
rm -rf "$CLADE_PAUSE_DIR" && mkdir -p "$CLADE_PAUSE_DIR"

# ---- Step 5: DD breakdown (real spawn) ----
trigger_role dd "STEP 5 — DD: break directive into TODO (REAL spawn ~30s)" expect_spawn_yes

# ---- Step 6: Team implement (real spawn) ----
trigger_role team "STEP 6 — Team: implement first TODO (REAL spawn ~2min)" expect_spawn_yes

# ---- Step 7a: GLOBAL pause + try DD verdict ----
banner "STEP 7a — KILL-SWITCH DEMO: clade-pause (GLOBAL), then trigger DD verdict"
"$CLADE_PAUSE_BIN" | sed 's/^/  /'
trigger_role dd "STEP 7a-trigger — DD verdict while paused (must cheap-exit, NO real spawn)" expect_spawn_no_paused

# ---- Step 7b: resume + retry DD verdict ----
banner "STEP 7b — clade-resume, then retry DD verdict (real spawn this time)"
"$CLADE_RESUME_BIN" | sed 's/^/  /'
trigger_role dd "STEP 7b-trigger — DD verdict after resume (REAL spawn ~40s)" expect_spawn_yes

# ---- Step 8a: per-peer pause for 'pd' + try PD ----
banner "STEP 8a — KILL-SWITCH DEMO: clade-pause --peer pd, then trigger PD"
"$CLADE_PAUSE_BIN" --peer pd | sed 's/^/  /'
trigger_role pd "STEP 8a-trigger — PD round while pd-paused (must cheap-exit)" expect_spawn_no_paused

# ---- Step 8b: resume per-peer + retry PD ----
banner "STEP 8b — clade-resume --peer pd, then retry PD round"
"$CLADE_RESUME_BIN" --peer pd | sed 's/^/  /'
trigger_role pd "STEP 8b-trigger — PD round after resume (REAL spawn, may decide no-op)" expect_spawn_yes

# ---- Step 9: DOC round ----
trigger_role doc "STEP 9 — DOC: documentation optimization round (REAL spawn ~60s)" expect_spawn_yes

# ---- Final summary ----
banner "FINAL STATE"
echo
echo "── git log (newest first):"
git -C "$TARGET" log --oneline | head -15 | sed 's/^/  /'
echo
echo "── kill-switch pause sentinel directory (should be empty after resumes):"
ls -1 "$CLADE_PAUSE_DIR" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
echo
echo "── files in repo:"
find "$TARGET" -type f \( -name "*.md" -o -name "*.py" -o -name "*.toml" \) | grep -v .git | sed 's/^/  /' | sort
echo
banner "✅ live kill-switch cycle complete"

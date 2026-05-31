# AITF demo — end-to-end checks of the AITF integration

A reproducible mini AI Team Framework project that the Clade A2A setup-server
can import. Two scripts: one cheap plumbing check (zero token cost), one live
5-spawn cycle with real `claude --print` calls (~$3-5 in API tokens, 5-15 min
wall time).

## What's in here

| File | Cost | What |
|---|---|---|
| `bootstrap.sh` | free | Builds a synthetic AITF project at `$TARGET` (default `/tmp/aitf-demo-e2e`) — writes `.ai-team-config.yml`, all `docs/TEAM/*.md` (PD, DD, Team, DO + status / TODO / decisions / templates), `git init`, initial commit. |
| `test-e2e.sh` | free | Runs `bootstrap.sh`, starts a setup-server on port 18765, POSTs `/api/setup/import-aitf`, verifies response + yamls + relay health, then exercises a scribe tick in-process with a stubbed `_call_claude_scribe` — never spawns real Claude. Cleans up on exit. |
| `run-live-cycle.sh` | **~$3-5** | Real 5-spawn AITF cycle: CEO seeds D001 → DD breakdown → Team implement → DD verdict → PD status → DOC optimize. Each tick spawns actual `claude --print --dangerously-skip-permissions` with the role's yaml. Captures git log + diffs after each. ~5-15 min wall time. |

## Run

```bash
# Free plumbing check
./examples/aitf-demo/test-e2e.sh

# Live cycle — real API tokens. Estimated ~$3-5, ~10 min.
./examples/aitf-demo/run-live-cycle.sh
```

`test-e2e.sh` should always end with `✅ all checks passed`. `run-live-cycle.sh`
prints `✅ live cycle complete` after the 5 spawns and dumps the final repo
state (git log, TODO, PROJECT_STATUS, REPORTS, src/, OPTIMIZATION_LOG).

Override `TARGET`, `SETUP_PORT`, `RELAY_PORT`, or `SKIP_CLEANUP=1` via env vars
if you need to keep the workspace around for inspection.

## Why both exist

`tests/test_aitf_import.py` and `tests/test_scribe_loop.py` cover the same
logic with synthetic fixtures, but `test-e2e.sh` exercises the **real**
setup-server process, the **real** spawned relay subprocess, and the **real**
yaml-on-disk path — failure modes that only show up when those moving parts
interact.

`run-live-cycle.sh` is the truth-test: it proves the role-specific scribe
prompts in `clade_cli/aitf_import.py::AITF_SCRIBE_DEFAULTS` actually drive
useful behavior end-to-end. The first run produced a working Typer CLI
scaffold (`src/cli.py`), a verdicted REPORT, and an updated PROJECT_STATUS
from a single seed commit — no human dispatch between steps. See git history
of `examples/aitf-demo/run-live-cycle.sh`'s commit message for the captured
run.

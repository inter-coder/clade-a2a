# AITF demo — end-to-end import + scribe sanity check

A reproducible mini AI Team Framework project that the Clade A2A setup-server
can import. Used to validate Phase A (POST `/api/setup/import-aitf`) and
Phase B (self-driving scribe loop on the `doc` peer) without touching your
real `~/.clade/` state.

## What's in here

| File | What |
|---|---|
| `bootstrap.sh` | Builds a synthetic AITF project at `$TARGET` (default `/tmp/aitf-demo-e2e`) — writes `.ai-team-config.yml`, all `docs/TEAM/*.md` (PD, DD, Team, DO + status / TODO / decisions / templates), `git init`, initial commit. |
| `test-e2e.sh` | Runs `bootstrap.sh`, starts a setup-server on port 18765 with its own data dir, POSTs to `/api/setup/import-aitf`, verifies the response + generated yamls + relay health, then exercises a scribe tick in-process (with a stubbed `_call_claude_scribe` — never spawns real Claude). Cleans up on exit. |

## Run

```bash
./examples/aitf-demo/test-e2e.sh
```

Expected output ends with `✅ all checks passed` and zero leftover processes.

## Why this exists

`tests/test_aitf_import.py` and `tests/test_scribe_loop.py` cover the same
logic with synthetic fixtures, but this script also exercises the **real**
setup-server process, the **real** spawned relay subprocess, and the **real**
yaml-on-disk path — the failure modes only show up when those moving parts
interact.

The scribe round itself is stubbed (no token spend). To watch a real scribe
round, point a `doc` daemon at the generated yaml after the script's
verification stage and add a commit to the AITF project — see
`a2a-protocol.md` v1.14.0 and `agent/daemon.py::scribe_loop` for the
production loop.

# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

Clade A2A — secure A2A message bus for Claude Code instances, plus a virtual
company orchestration layer on top of it. Pip package `clade-a2a`, entry points:

- `clade-relay` — FastAPI relay (auth + dispatch)
- `clade-agent` — stdio MCP server (Claude Code spawns it via `.mcp.json`)
- `clade-init` — legacy bootstrap (still works; superseded by setup-server)
- `clade-setup-server` — web setup wizard + REST management of peers/teams
- `clade-add-peer` — surgical peer addition CLI (calls into `clade_cli.peer_ops`)

Read `README.md` for feature overview and the two recommended setup paths
(web form + `--quickstart`).

**Canonical protocol lives in `a2a-protocol.md` (currently v1.12.x)** — single
source of truth for envelope schema, HMAC algorithm, MCP tool API, daemon
model, file lock semantics, presence, teams, tasks, peer-mgmt endpoints. When
you change A2A behavior, edit the protocol first and bump the version
(SEMVER rules in §14). Don't duplicate protocol info in CLAUDE.md or
template files.

## Commands

```bash
# Setup (once)
uv venv && uv pip install -e .

# Run tests (62+ tests, each spawns its own relay on a free port)
./scripts/test.sh
# Single test:
.venv/bin/python -m pytest tests/test_agent_e2e.py::test_clade_message_send_fire_and_forget -v

# Run relay standalone
.venv/bin/clade-relay --tokens relay/tokens.json --host 127.0.0.1 --port 7777

# Build wheel
.venv/bin/python -m build  # output in dist/

# E2E demo (alice ↔ bob, headless)
./scripts/start-relay.sh &
./scripts/demo-ask-reply.sh

# Setup-server (primary user-facing entry point)
./scripts/start-setup-server.sh                 # blank slate, configure via browser
./scripts/start-setup-server.sh --quickstart    # auto-bootstrap default 4-peer company
./scripts/start-setup-server.sh --deep          # full reset (also wipes audit DBs)

# After setup-server is running, install all peers locally in one shot:
curl -fsSL http://<host>:8000/setup/<token>/install-all | bash

# Surgically add a 5th peer (no daemon restarts)
./scripts/add-peer.sh designer "Mira — UX designer" "You are Mira..." --team engineering

# Cleanup orphan state
./scripts/clade-cleanup.sh                      # show + ask before killing daemons/locks/workdirs
./scripts/clade-cleanup.sh --dry-run            # just show what would be done
./scripts/clade-cleanup.sh --include-relay      # also kill any clade-relay processes
./scripts/clade-cleanup.sh --include-setups     # wipe ~/.clade/setup-server/* (and their relays)
./scripts/clade-cleanup.sh --prune-audit 7      # drop audit rows older than 7 days + VACUUM
```

No linter / type-checker config. Before commit, just `./scripts/test.sh`.

## Architecture (big picture)

Four components:

1. **`relay/`** — FastAPI dispatcher. Bearer auth + nonce dedup + ts skew
   validation. **Does NOT validate HMAC** (E2E is the receiver's job).
   Pluggable storage: in-memory or Redis (`relay/store.py`, picked via
   `REDIS_URL`). Holds presence (in-memory, 35s TTL) + pending_asks
   (`asyncio.Future`, non-serializable, lost on restart by design).

2. **`agent/main.py`** — stdio MCP server that Claude Code spawns via
   `.mcp.json`. Implements 12 `clade_*` tools (message, send, ask, inbox,
   reply, outbox_status, peers, broadcast, task, task_update, task_status,
   task_list). HMAC sign/verify, SQLite audit + outbox + thread_history +
   tasks tables. Config schema in `Config` (PeerInfo, teams, extra_add_dirs).

3. **`agent/daemon.py`** — long-running poller every 2s; spawns
   `claude --print --mcp-config <wd>/.mcp.json --add-dir <extra>...` for
   auto-reply to `ask` messages. **Single-owner inbox** via file lock
   (`<audit_db_dir>/<peer>-daemon.lock`); `clade_inbox` returns busy when
   lock is alive. Side loops: `outbox_monitor_loop`, `presence_loop`
   (heartbeat every 15s), `config_watcher_loop` (5s mtime poll, hot-reloads
   `cfg.peers` + `cfg.teams`), `scribe_loop` (v1.14.0, opt-in via
   `cfg.scribe`; periodic self-driving documentation rounds, ticks every
   `interval_minutes`, cheap early-exit when git HEAD of watched repo hasn't
   moved — zero token spend on idle days).

4. **`clade_cli/setup_server.py`** — FastAPI web setup wizard + REST
   management. Generates tokens.json + per-peer yamls + `.mcp.json`,
   spawns relay subprocess. Exposes:
   - Form at `/` (POST `/api/setup`)
   - Result/management page at `/setup/{token}` + JSON status
   - Per-peer install endpoints `/agent/{download_token}/{install|config|start|chat|mcp-config}`
   - Bulk install: `/setup/{token}/install-all` (returns bash script)
   - Mgmt: `POST/PATCH/DELETE /api/setup/{token}/peers[/{peer_id}]`,
     `GET/PUT /api/setup/{token}/teams`, `POST /admin/reload`
   - AITF import (v1.13.0): `POST /api/setup/import-aitf` adopts an
     AI Team Framework project — see `clade_cli/aitf_import.py`
     (`detect_aitf_project`, `parse_aitf_project`).
   - Actual peer ops in `clade_cli/peer_ops.py` (`add_peer_op`,
     `update_peer_op`, `remove_peer_op`, `update_teams_op`); CLI
     (`clade-add-peer`) is a thin wrapper around the same module.

**Outbox** (`agent/outbox.py`) — SQLite table in the same DB as audit log.
Backoff `[1,2,4,8,16,30]`s × max 6 attempts → dead-letter. Fire-and-forget
send/reply on network error or 5xx auto-queues; next tool call lazy-flushes.
**Synchronous `clade_message(expect_reply=True)` does NOT queue** — the user
retries.

## Conventions

- **Language:** code, comments, log messages, docstrings in **English**
  (project-wide policy as of v1.10.x; older code may still be in Serbian
  latinica without diacritics — translate opportunistically).
- **Comments:** explain *why*, not *what*. Only when something is non-obvious
  (e.g. why `pending_asks` stays in-memory). No comments that paraphrase
  the code.
- **Phase tracking:** module docstrings carry "Phase X" / "v1.x.0" labels
  when introducing new functionality. Protocol bumps go in
  `a2a-protocol.md` §11 with a one-line summary.
- **Backwards compat:** API breaking changes (e.g. removing `clade_send` /
  `clade_ask`) announced at least one minor before removal. Wrappers that
  print a stderr warning are an acceptable bridge.

## Common gotchas

- **Daemon and Claude concurrent on inbox:** never call `clade_inbox`
  from prompts when a daemon is running — it returns a busy error. That's
  a *feature* (file lock §6 of the protocol), not a bug.
- **HMAC mismatch between peers:** shared secret must be IDENTICAL in both
  `<peer>.yaml` files under the reciprocal key (alice.yaml `peers.bob` ==
  bob.yaml `peers.alice`). Setup-server and `peer_ops` guarantee this; if
  you hand-edit yamls, verify.
- **Replay / clock skew:** nonce + ts ±5min. If tests flap on a timestamp
  error, it's NTP, not the code.
- **Tests reload `agent.main`:** because of import-time config load, tests
  use `_load_agent_module(config_path)` which pops from `sys.modules`
  before re-importing. Keep that pattern when adding new tests with a
  different config.
- **Cached bearer in long-running daemons:** the daemon loads
  `bearer_token` at startup and keeps it in memory. If you regenerate
  tokens (e.g. setup-server restart), running daemons keep sending the
  old bearer and get 401. Quickstart wipes `~/clade-agent/` to prevent
  this; for manual flows, restart the daemon after token changes.

## Open items

`samozapazanja.md` (root) contains peer-to-peer dialog between two A2A
agents from earlier iterations. Phases v1.0.0 → v1.2.0 delivered.
Production feedback from Predrag/Katana still pending.

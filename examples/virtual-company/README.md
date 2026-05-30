# Virtual Company example

A 4-peer mini-company demonstrating **Clade A2A as an orchestration framework**:

| Peer ID | Display | Role | Team |
|---|---|---|---|
| `ceo` | "CEO" | You — coordinator and decision-maker. Driven interactively. | (broadcasts only) |
| `frontend` | "Ana — Frontend dev" | React/TypeScript expert | engineering |
| `backend` | "Bob — Backend dev" | Python/FastAPI/Postgres expert | engineering |
| `qa` | "Cveta — QA" | Test automation, manual QA, bug triaging | engineering |

Teams:
- `engineering = [frontend, backend, qa]` — everything dev-side
- `everyone = [frontend, backend, qa]` — all-hands (without CEO; CEO is the sender)

## Recommended: just run `--quickstart`

This whole company plus its relay is exactly what `--quickstart` bootstraps:

```bash
./scripts/start-setup-server.sh --quickstart
```

The script writes `~/clade-agent/` with one yaml + `start-<peer>.sh` +
`chat-<peer>.sh` + `workdir-<peer>/` per peer, plus `start.sh` / `chat.sh`
symlinks (backwards compat). Then it prints the start-daemon commands.

After that, manage the company from `http://<host>:8000/setup/<token>` —
add/edit/remove peers, edit teams. See the project README for full details.

## Alternative: standalone bootstrap script

`bootstrap.sh` (in this directory) generates the same artifacts but
without using the setup-server. Use it if you want full control or want
to inspect the generated yamls before installing:

```bash
cd examples/virtual-company
./bootstrap.sh
# Generates tokens.json + ceo/frontend/backend/qa yamls + mcp-ceo.json

# Then 5 terminals:
../../.venv/bin/clade-relay --tokens $(pwd)/tokens.json --host 127.0.0.1 --port 7777
CLADE_CONFIG=$(pwd)/frontend.yaml ../../.venv/bin/python -m agent.daemon --yolo
CLADE_CONFIG=$(pwd)/backend.yaml  ../../.venv/bin/python -m agent.daemon --yolo
CLADE_CONFIG=$(pwd)/qa.yaml       ../../.venv/bin/python -m agent.daemon --yolo
CLADE_CONFIG=$(pwd)/ceo.yaml      claude --mcp-config $(pwd)/mcp-ceo.json
```

## Things to try in the CEO session

Speak naturally; Claude maps to `clade_*` tools.

### Who is in the company
```
> who is online?
```
→ `clade_peers()` returns the table with roles + online status

### Broadcast to a team
```
> tell engineering: stand-up in 10 minutes
```
→ `clade_broadcast(to_team="engineering", content="stand-up in 10 minutes")`

### Question to a team, parallel answers
```
> ask engineering: how long would feature X take?
```
→ `clade_broadcast(to_team="engineering", content="...", expect_reply=True)` → CEO gets 3 parallel answers

### Async delegation
```
> delegate to Ana: refactor LoginPage by Friday
```
→ `clade_task(to="frontend", brief="refactor LoginPage", deadline_ts_ms=...)`
→ returns `task_id`; Ana works as long as she needs. Later:

```
> what's the status of that task?
> what are all current open tasks?
```
→ `clade_task_status(...)` / `clade_task_list(filter="sent", status="in_progress")`

### Direct 1:1 ask (synchronous)
```
> ask Bob: what's the current API rate-limit policy?
```
→ `clade_message(to="backend", content="...", expect_reply=True)` — blocks up to 90s

### Fire-and-forget
```
> tell Cveta the v3.2.0 deploy is live
```
→ `clade_message(to="qa", content="...", expect_reply=False)`

## What this DOES show

- Multi-peer orchestration with role-aware prompts
- Broadcast (1 → N in parallel)
- Async task delegation with local DB persistence
- Team grouping for one-shot targeting
- Live presence (who has a running daemon)

## What's intentionally out of scope (YAGNI until proven needed)

- Cross-employee transparency (Ana can't see what Bob is saying to the CEO)
- Hierarchy enforcement (any peer can broadcast; role is a prompt convention, not RBAC)
- Persistent calendar / KPI / performance tracking (the tasks table is a start, not a calendar)
- Long-running task auto-resumption after daemon restart (DB holds state, but no scheduler picks it back up)

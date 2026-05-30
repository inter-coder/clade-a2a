# Clade A2A

> **Build your virtual company.** Orkestriraj N Claude Code agenata sa razlicitim ulogama, koordiniraj kroz CEO peer, delegiraj zadatke, broadcast-uj timu. Sigurna HMAC-potpisana A2A komunikacija preko centralnog relay-a.

**Status:** v1.9.0 — virtual company orchestration (broadcast, teams, async tasks, presence, role-aware prompts). Production-ready za LAN/VPN, public deploy preko Caddy + TLS.

---

## Sta gradis ovim alatom

Ne "secure message bus" — to je samo donja kora. **Pravi use case:** virtuelna firma gde svaki agent ima ulogu, a ti (kao CEO) ih koordinirat. Konkretni primeri:

- **Mini dev firma:** ti = CEO, agent_1 = backend dev, agent_2 = frontend dev, agent_3 = QA. Delegiras zadatke, oni rade paralelno, javljaju status.
- **Research team:** ti = lead researcher, N agenata sa razlicitim domain expertize-om (NLP, vision, RL). Broadcast pitanje, pokupiš N perspektiva.
- **Content pipeline:** writer agent, editor agent, fact-checker agent, publisher agent. Sekvencijalna delegacija kroz task-ove.
- **Support tier:** triage agent prima sve, eskalira specijalistima preko `clade_task(to=<specialist>, brief=...)`.

Glavni API koji omogucava ovo:

```
clade_peers()                        ← ko je online sada
clade_broadcast(to_team, content)    ← jedna poruka, N peer-ova paralelno
clade_task(to, brief, deadline)      ← async delegacija, vraca task_id
clade_task_status(task_id)           ← provera napretka
clade_task_list(filter, status)      ← moj backlog (sent + received)
clade_message(to, content, ...)      ← direct ask ili fire-and-forget (1:1)
```

**Šta NIJE:** zamena za Slack izmedju ljudi. Latencija je 5-90s po reply-u zbog LLM inference + polling.

---

## Quick start — virtuelna firma za 60 sekundi

Najlakse je preko pre-pripremljenog primera u `examples/virtual-company/`:

```bash
# 1. Install
git clone https://github.com/inter-coder/clade-a2a.git && cd clade-a2a
uv venv && uv pip install -e .

# 2. Bootstrap firmu (CEO + 3 zaposlena: frontend, backend, qa)
cd examples/virtual-company
./bootstrap.sh
# Generise tokens.json + 4 peer yaml-ova + mcp-ceo.json sa svezem secret-ima

# 3. Pokreni relay (terminal 1)
../../.venv/bin/clade-relay --tokens $(pwd)/tokens.json --host 127.0.0.1 --port 7777

# 4. Pokreni 3 employee daemona (terminali 2, 3, 4)
CLADE_CONFIG=$(pwd)/frontend.yaml ../../.venv/bin/python -m agent.daemon --yolo
CLADE_CONFIG=$(pwd)/backend.yaml  ../../.venv/bin/python -m agent.daemon --yolo
CLADE_CONFIG=$(pwd)/qa.yaml       ../../.venv/bin/python -m agent.daemon --yolo

# 5. Pokreni CEO interaktivnu sesiju (terminal 5)
CLADE_CONFIG=$(pwd)/ceo.yaml claude --mcp-config $(pwd)/mcp-ceo.json
```

U CEO promptu pricaš prirodno:

```
> Ko je sad u firmi?
   → clade_peers() — tabela sa role i online statusom

> Posalji engineering team-u: stand-up za 10 minuta
   → clade_broadcast(to_team="engineering", content="stand-up za 10 minuta")

> Pitaj backend i frontend paralelno: koliko vremena treba za feature X?
   → clade_broadcast(to=["backend","frontend"], content="...", expect_reply=True)

> Delegiraj backend-u: refaktor login API endpoint
   → clade_task(to="backend", brief="refaktor login API endpoint")
   → vraca task_id; backend ce raditi koliko mu treba

> Sta su tekuca zaduzenja koja sam dao?
   → clade_task_list(filter="sent", status="in_progress")
```

Detalji po sceniju: [`examples/virtual-company/README.md`](examples/virtual-company/README.md).

---

## Alternativan setup — web wizard (za pravi deploy)

Za firmu sa peer-ovima na razlicitim masinama:

```bash
./scripts/start-setup-server.sh
# Otvori http://127.0.0.1:8000/ (ili LAN IP)
# Forma: dodaj peer-ove + dodaj teams + Generate Setup
```

Setup-server vraca curl URL po peer-u. Svaki peer (cak i na drugoj masini) pokrece:

```bash
curl -fsSL http://<setup-host>:8000/agent/<token>/install | bash
~/clade-agent/start.sh --yolo            # daemon
~/clade-agent/chat.sh                    # interactive Claude sesija
```

Detalji: [`scripts/start-setup-server.sh`](scripts/start-setup-server.sh) i in-browser uputstvo.

---

## Mental model

```
CEO masina (ti drzis ovaj peer interaktivno)
  ┌─────────────────────────────────────┐
  │ Claude Code (interactive)           │
  │   ↓ MCP stdio                       │
  │ clade-agent (peer 'ceo')            │
  │   ↓ HTTPS + Bearer + HMAC           │
  └─────────┬───────────────────────────┘
            │
       ┌────▼────────────────┐
       │     Clade Relay     │  ← FastAPI dispatcher + presence tracker
       │ (presence, audit,   │     + back-pressure 503 + Redis store
       │  pending_asks)      │
       └────┬────────────────┘
            │
   ┌────────┼────────┬───────────────┐
   │        │        │               │
   ▼        ▼        ▼               ▼
┌──────┐ ┌──────┐ ┌──────┐    Employee masine (svaka tece daemon)
│front │ │back  │ │ qa   │    Daemon polluje relay, auto-odgovara na ask,
│-end  │ │-end  │ │      │    auto-evidentira tasks u SQLite, salje
│daemon│ │daemon│ │daemon│    presence heartbeat svakih 15s.
└──────┘ └──────┘ └──────┘
```

Svaki peer ima dva sloja:
- **Daemon** (uvek tece) — polluje inbox, auto-odgovara na ask-ove kroz `claude --print --append-system-prompt "<role>"`, registruje task-ove u local SQLite, salje presence heartbeat.
- **Interaktivni Claude** (po potrebi) — kad ZELIS rucno da inicirаsh poruke. CEO ovo drzi stalno otvoreno.

---

## MCP tools — sta Claude vidi

Posle MCP setup-a, svaki peer ima 10 `clade_*` tools (v1.9.0):

### Komunikacija

| Tool | Sta radi |
|---|---|
| `clade_message(to, content, expect_reply=False, timeout_s=90, thread_id=None)` | Direct 1:1 poruka. `expect_reply=True` = sinhroni ask. |
| `clade_broadcast(content, to=[...] \| to_team="...", expect_reply=False)` | Paralelno N peer-ova. **v1.9.0** |
| `clade_reply(correlation_id, response, to)` | Manualan reply (daemon obicno auto). |
| `clade_inbox(max_items=50)` | Drenira inbox; vraca busy ako daemon tece. |

### Discovery / awareness

| Tool | Sta radi |
|---|---|
| `clade_peers()` | Ko je online sada (heartbeat <30s). Vraca role-ove + secs_ago. **v1.8.0** |

### Task delegation

| Tool | Sta radi |
|---|---|
| `clade_task(to, brief, deadline_ts_ms=None)` | Async delegate. Vraca `task_id` odmah. **v1.9.0** |
| `clade_task_update(task_id, status, result)` | Assignee javlja delegator-u. `status` ∈ pending/in_progress/done/failed/cancelled. **v1.9.0** |
| `clade_task_status(task_id)` | Provera tekuceg statusa iz lokalne DB. **v1.9.0** |
| `clade_task_list(filter="all"\|"sent"\|"received", status=None)` | Lista task-ova. **v1.9.0** |

### Diagnostics

| Tool | Sta radi |
|---|---|
| `clade_outbox_status()` | Stanje pending poruka + force flush. |

Deprecated: `clade_send`, `clade_ask` — koristi `clade_message`.

---

## Konfiguracija

### Per-peer yaml (v1.9.0 sa teams)

```yaml
my_id: ceo
name: "CEO"
role: |
  Ti si CEO virtualne firme. Koordiniras, delegiras, broadcast-ujes.
  Pre delegacije proveri ko je online (clade_peers).
relay_url: http://192.168.1.91:7777
bearer_token: <urlsafe-32>
peers:
  frontend:
    secret: <hex-64>
    name: "Ana — Frontend dev"
    role: "React/TypeScript ekspert"
  backend:
    secret: <hex-64>
    name: "Bob — Backend dev"
    role: "Python/FastAPI/Postgres ekspert"
  qa:
    secret: <hex-64>
    name: "Cveta — QA"
    role: "Test automation, manual QA"
teams:                              # v1.9.0
  engineering: [frontend, backend, qa]
  everyone:    [frontend, backend, qa]
audit_db: ~/.clade/ceo-audit.db
```

Svi peer-ovi imaju **iste** teams definicije (bootstrap.sh ili setup-server to garantuje). Permissions 0600.

### Relay tokens.json

```json
{
  "<ceo-bearer>":      "ceo",
  "<frontend-bearer>": "frontend",
  "<backend-bearer>":  "backend",
  "<qa-bearer>":       "qa"
}
```

Permissions 0600. NIKAD u git (`.gitignore` blokira).

---

## Patterns za virtuelnu firmu

### 1. CEO koordinator pattern (preporuceno)

CEO peer = ti, ostali = daemon-i. CEO drzi interaktivnu Claude sesiju, kuca prirodnim jezikom, Claude mapira u `clade_*`. Daemon-i automatski obradjuju ask + task poruke.

**Prednost:** ti ne moras da kucas kod. "Reci backend-u da ..." → automatski.

### 2. Async task delegation

Kad zadatak traje duze od 90s (refactor, prezentacija, istraživanje):

```
CEO: clade_task(to="backend", brief="Refactor login flow", deadline_ts_ms=...)
     → vraca {task_id: "abc-123", status: "pending"}

Backend daemon: prima task u inbox, vidi _task flag → INSERT u tasks tabelu.
                Backend Claude (kad bude pokrenut interaktivno) vidi u clade_task_list.

Backend: clade_task_update("abc-123", status="in_progress")
         → CEO daemon prima _task_update → UPDATE local DB

Backend: clade_task_update("abc-123", status="done", result="PR #42")
         → CEO vidi u clade_task_list(status="done")
```

### 3. Broadcast for status/announcements

```
CEO: clade_broadcast(to_team="everyone", content="deploy v3.2.0 u 14h")
     → 3 paralelne fire-and-forget poruke

CEO: clade_broadcast(to_team="engineering", content="koliko trajete?", expect_reply=True)
     → 3 paralelnih ask-ova; CEO dobija 3 odgovora u jedan results dict
```

### 4. Specijalist delegation

Kad CEO ne zna ko bi to znao:

```
CEO: clade_peers()  → vidi role-ove
CEO: clade_message(to="qa", content="koje su testne strategije za auth flow?", expect_reply=True)
```

Ili peer-ovi medjusobno (frontend pita backend mid-task):

```
Frontend Claude tokom rada: clade_message(to="backend", content="koje su error kodovi /api/login?", expect_reply=True)
```

---

## Reference

### REST endpoint-ovi relay-a (v1.9.0)

| Method | Path | Auth | Sta radi |
|---|---|---|---|
| GET | `/health` | nije | Liveness + online_agents + pending_by_peer |
| POST | `/send` | Bearer | Fire-and-forget |
| POST | `/ask` | Bearer | Sinhroni ask (server blokira do reply ili timeout) |
| POST | `/reply` | Bearer | Reply na pending ask |
| GET | `/inbox/{agent_id}` | Bearer | Drenira sopstveni inbox |
| GET | `/audit` | Bearer | Audit log (poslednjih N) |
| POST | `/presence` | Bearer | Heartbeat — daemon kuca svakih 15s **(v1.8.0)** |
| GET | `/presence` | Bearer | Snapshot ko je online + secs_ago **(v1.8.0)** |
| GET | `/ui/audit` | nije | Static HTML viewer |

### Protokol

Single source of truth: [`a2a-protocol.md`](a2a-protocol.md) (v1.9.0). Citaj pre nego sto menjas envelope schema ili HMAC.

### Repo struktura

```
clade-a2a/
├── README.md                    ← ovaj fajl
├── a2a-protocol.md              ← protocol SSOT (v1.9.0)
├── ROADMAP.md                   ← phase tracking
├── examples/
│   └── virtual-company/         ← 4-peer demo firma (CEO + 3 employees) — start here
│       ├── README.md            ← scenarji
│       └── bootstrap.sh         ← generise sve secret-e
├── relay/                       ← FastAPI dispatcher + presence + Redis store
├── agent/
│   ├── main.py                  ← stdio MCP server, 10 tool-ova
│   ├── daemon.py                ← long-running poller + auto-responder
│   └── outbox.py                ← SQLite outbox + backoff
├── clade_cli/
│   ├── init.py                  ← clade-init bootstrap CLI
│   ├── setup_server.py          ← web setup wizard (FastAPI)
│   └── templates/               ← chat.sh, start.sh, install.sh, index.html, result.html
├── scripts/
│   ├── start-setup-server.sh
│   ├── clade-cleanup.sh         ← gas daemon-e + relay + workdir-e
│   └── test.sh
├── tests/                       ← pytest suite (50+ testova)
├── deploy/                      ← Docker Compose + Caddy + systemd
└── pyproject.toml
```

### Limiti

| Setting | Default | Override |
|---|---|---|
| Nonce TTL | 300s | `relay/main.py:NONCE_TTL_S` |
| Timestamp skew | 300_000ms | `relay/main.py:TS_SKEW_MS` |
| Inbox max per agent | 1000 | `relay/main.py:INBOX_MAX` |
| Max pending ask per peer | 4 | `CLADE_RELAY_MAX_PENDING_PER_PEER` env (v1.6.0+) |
| Ask default timeout | 90s | per-call argument |
| Presence TTL | 35s | `CLADE_RELAY_PRESENCE_TTL_S` env (v1.8.0+) |
| Presence heartbeat | 15s | `CLADE_DAEMON_PRESENCE_S` env (v1.8.0+) |
| Daemon concurrency | 2 | `CLADE_DAEMON_CONCURRENCY` env (v1.4.8+) |

### Security model

| Pretnja | Mitigacija |
|---|---|
| Outsider | Bearer auth wall (401) |
| Token leak | 0600 file perms, rotacija, never-log |
| Relay compromised | E2E HMAC — relay ne moze forge |
| Peer compromise | Audit trail, token revoke |
| MITM | TLS (public) ili VPN (LAN) |
| Replay | Nonce + ts ±5min |
| Prompt injection od peer-a | `_instruction` polje u clade_inbox + system prompt disciplina |

**Granica:** kooperativni peer-ovi. Ako ne verujes nekom — ne dodaj ga u `peers:` allowlist.

---

## Troubleshooting

**`clade_peers` pokazuje peer-a kao offline iako daemon "tece"** — verovatno daemon je od v1.7.x ili stariji (nema presence_loop). Upgrade + restart daemon-a. Posle 15s mora biti online.

**`clade_broadcast` vraca `failed > 0`** — bar jedan target peer je offline ili nepoznat. `results` dict pokazuje per-peer error.

**`clade_task` poslat ali `clade_task_status` vraca task_id nije u DB** — to je delegator side, task_id postoji od trenutka slanja. Ako error, znaci task_id si pogresno kopirao.

**Assignee ne vidi `clade_task_list(filter="received")` iako CEO je delegirao** — daemon mora biti pokrenut na assignee masini kad task stigne. Inace task ode u relay inbox i ceka. Kad daemon krene, drain-uje inbox i upisuje u tasks.

**Send queued, ne flush-uje** — relay nedostupan. Provera: `curl http://<relay>:7777/health`. Status: `clade_outbox_status`.

**Tampered HMAC u rejected** — shared secret ne odgovara izmedju peer-ova. Ako koristis bootstrap.sh / setup-server, ovo se ne moze desiti. Ako rucno editujes yaml — proveri da `alice.yaml.peers.bob.secret == bob.yaml.peers.alice.secret`.

---

## Komande za dev

```bash
# Run tests
./scripts/test.sh

# Single test
.venv/bin/python -m pytest tests/test_agent_e2e.py::test_clade_broadcast_basic -v

# Build wheel
.venv/bin/python -m build

# Cleanup orphan daemon-i / relay / workdir-i / setup-server
./scripts/clade-cleanup.sh
./scripts/clade-cleanup.sh --dry-run
./scripts/clade-cleanup.sh --include-relay --include-setups
```

Nema linter/type-checker config. Pre commit-a samo `./scripts/test.sh`.

---

## Status & roadmap

| Verzija | Sta | Status |
|---|---|---|
| v1.0.0 | `clade_message` unifikacija, file lock | ✓ |
| v1.1.0 | Thread persistence (`_thread_id` + SQLite) | ✓ |
| v1.2.0 | Clarify-back, outbox monitor, minimal headless | ✓ |
| v1.3.0 | Name + role per peer (system prompt) | ✓ |
| v1.4.x | UX iteracije, setup-server, parallel poll, humani errori | ✓ |
| v1.6.0 | Cancel protokol + relay back-pressure | ✓ |
| v1.7.0 | Hot-reload peer allowlist, log rotation, audit prune | ✓ |
| v1.8.0 | Presence layer (`clade_peers`, /presence endpoint) | ✓ |
| **v1.9.0** | **Virtual company: broadcast, teams, async tasks** | ✓ |
| v2.x | Persistent task scheduler, CEO dashboard, transparency mode | future |

Detalji: [`a2a-protocol.md` §11](a2a-protocol.md).

---

## Licenca

MIT.

## Autor

Dušan Krstić (Symappsys d.o.o.) sa Claude Code asistencijom.

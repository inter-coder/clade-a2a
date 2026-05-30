# Virtual Company example

4-peer mini-firma za demonstraciju **Clade A2A kao orchestration framework**:

| Peer ID | Display | Role | Team |
|---|---|---|---|
| `ceo` | "CEO" | Ti — koordinator i odlučilac. Korisnik vodi ovaj peer interaktivno. | (broadcast only) |
| `frontend` | "Ana — Frontend dev" | React/TypeScript ekspert, UI/UX | engineering |
| `backend` | "Bob — Backend dev" | Python/FastAPI/Postgres ekspert | engineering |
| `qa` | "Cveta — QA" | Test automation, manual QA, bug triaging | engineering |

Teams:
- `engineering = [frontend, backend, qa]` — sve sto je dev sastrane
- `everyone = [frontend, backend, qa]` — all-hands (bez CEO-a, jer CEO šalje)

## Brzo pokretanje

### Opcija A — sve na jednoj masini (najlakse za demo)

4 terminala:

```bash
# Terminal 1: relay
cd /putanja/do/clade-a2a
.venv/bin/clade-relay --tokens examples/virtual-company/tokens.json --host 127.0.0.1 --port 7777

# Terminal 2: frontend daemon
CLADE_CONFIG=examples/virtual-company/frontend.yaml .venv/bin/clade-agent  # wait, daemon ide drugacije
# Zapravo:
CLADE_CONFIG=examples/virtual-company/frontend.yaml .venv/bin/python -m agent.daemon --yolo

# Terminal 3: backend daemon
CLADE_CONFIG=examples/virtual-company/backend.yaml .venv/bin/python -m agent.daemon --yolo

# Terminal 4: qa daemon
CLADE_CONFIG=examples/virtual-company/qa.yaml .venv/bin/python -m agent.daemon --yolo

# Terminal 5 (CEO interaktivno):
cd examples/virtual-company
CLADE_CONFIG=$(pwd)/ceo.yaml claude --mcp-config $(pwd)/mcp-ceo.json
```

### Opcija B — preko setup-server-a (preporučeno za pravu firmu)

```bash
./scripts/start-setup-server.sh
# Otvori http://127.0.0.1:8000/, ucitaj sa: { "import_example": "virtual-company" }
# (Manual: dodaj 4 peer-a sa name/role kao iznad + dodaj 2 team-a u Teams sekciji)
```

## Šta probati u CEO sesiji

CEO daje instrukcije prirodnim jezikom; Claude mapira u `clade_*` tool-ove.

### 1. Ko je u firmi
```
> Ko je sve u firmi i ko je online?
```
→ `clade_peers()` vraca tabelu sa role-om i online statusom

### 2. Broadcast all-hands
```
> Posalji svima u engineering: stand-up za 10 minuta
```
→ `clade_broadcast(to_team="engineering", content="stand-up za 10 minuta")`

### 3. Pitanje timu sa odgovorima
```
> Pitaj engineering: koliko vremena treba za feature X?
```
→ `clade_broadcast(to_team="engineering", content="koliko vremena treba za feature X?", expect_reply=True)`
→ CEO dobija 3 paralelnih odgovora

### 4. Delegiraj zadatak
```
> Delegiraj Ani: refaktor LoginPage komponente do petka
```
→ `clade_task(to="frontend", brief="refaktor LoginPage komponente", deadline_ts_ms=...)`
→ Vraća `task_id`, Ana radi koliko joj treba; CEO posle:

```
> Sta je sa zadatkom <task_id>?
```
→ `clade_task_status(task_id)`

Ili pregled svih:
```
> Koje su tekuce zaduzenja?
```
→ `clade_task_list(filter="sent", status="in_progress")`

### 5. Direct ask (sinhroni)
```
> Pitaj Boba: koja je trenutna API rate-limit politika?
```
→ `clade_message(to="backend", content="...", expect_reply=True)` — blokira do 90s

### 6. Fire-and-forget
```
> Reci Cveti da je deploy v3.2.0 u produkciji
```
→ `clade_message(to="qa", content="...", expect_reply=False)`

## Struktura fajlova

```
examples/virtual-company/
  README.md            ← ovaj fajl
  tokens.json          ← bearer tokens za relay (jedan po peer-u)
  ceo.yaml             ← CEO config (peers + teams)
  frontend.yaml        ← Ana config
  backend.yaml         ← Bob config
  qa.yaml              ← Cveta config
  mcp-ceo.json         ← MCP config za CEO Claude sesiju
```

Svaki yaml ima isti `teams` blok — svi peer-ovi znaju iste team-ove.

## Šta ovo POKAZUJE, sta NE

**Pokazuje:**
- Multi-peer orchestration sa rolama
- Broadcast (1 → N paralelno)
- Async task delegation sa lokalnom DB persistence
- Team grupisanje za jedinstveno targetovanje
- Live presence (ko je daemon-up sada)

**Ne resava** (skoro je verovatno YAGNI dok ne pukne, ali napomena):
- Cross-employee transparency (alice ne vidi sta bob radi sa ceo-om)
- Hierarchy enforcement (svaki peer moze broadcast — ne samo CEO; uloga je samo u prompt-u)
- Persistent calendar / KPI / performance tracking (tasks tabela je pocetak, ali nije calendar)
- Long-running task auto-resumption posle daemon restart-a (DB drzi state, ali nema scheduler-a)

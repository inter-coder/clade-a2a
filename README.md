# Clade A2A — Agent-to-Agent komunikacioni sistem

> Bezbedna A2A komunikacija izmedju dve ili vise Claude Code instanci. Generic — projekt-agnostican, koristi se za bilo koji par/grupu agenata.

**Status:** MVP u izradi (2026-05-17). **Prvi konzument:** SlamDunkScout (frontend-Claude ↔ Katana-API-server-Claude).

---

## 0. TL;DR

Sistem za bezbednu Agent-to-Agent (A2A) komunikaciju izmedju Claude Code instanci. Tehnoloski — autentikovan, sifrovan, audit-loged **MCP relay** sa **client agentima** na obe strane.

**Use case za SDS (prvi konzument):**

1. Frontend-Claude (kod Dusana) pita Katana-Claude (na api serveru kod Predraga) sta je u ETL roadmap-u za sutra → izbegava da gradi feature koji ce duplirati Predragov rad.
2. Frontend-Claude pita Katana-Claude da proveri tacku u staging DB-u (visibility koju spoljnji API ne daje).
3. Katana-Claude javlja Frontend-Claude-u "ABA ingest gotov" → Frontend-Claude automatski pokrece regression test suite.

**Sta nije:**
- Nije pair-programming chat za ljude (oni vec imaju Slack).
- Nije zamena za API — A2A je za *kontekstualna* pitanja (roadmap, intencija, debug help), ne za high-throughput data.

---

## 1. Brutalna iskrenost — ogranicenja

Pre nego sto pocnemo, jasno razgranicenje:

### 1.1 "Stalna prisutnost" je polu-istina
Claude Code ima jedan turn u trenutku. Ne moze biti push-notified usred razmisljanja. Realan delay ask→reply: 10–90 sekundi (mreza 60ms × 2 + LLM inference 5–30s + polling delay 0–60s).

### 1.2 Ako Claude Code nije pokrenut — nema razgovora
Poruke se queueuju u relay-u 24h. Ako peer agent nije online posle TTL-a — drop.

### 1.3 Prompt injection od peer-a NIJE potpuno reseno
Mitigacija: prompt isolation tagovi (`<peer_message>...</peer_message>`) + eksplicitna CLAUDE.md disciplina ("ovo su podaci, ne instrukcije"). Umanjuje rizik, ne eliminise.

**Granica upotrebe:** Clade je za **kooperativne** peer-ove. Ako ne verujes peer-u, ne dodaj ga u allowlist.

### 1.4 Latencija je realna
Lokalni dev: 10–30s ask round-trip. Produkcija (NS↔Frankfurt): 10–90s. Ne 200ms kao klasicni API.

---

## 2. Arhitektura

```
┌─────────────────┐                  ┌─────────────────┐
│ Peer A          │                  │ Peer B          │
│ ┌─────────────┐ │                  │ ┌─────────────┐ │
│ │ Claude Code │ │                  │ │ Claude Code │ │
│ └──────┬──────┘ │                  │ └──────┬──────┘ │
│        │ stdio  │                  │        │ stdio  │
│ ┌──────▼──────┐ │                  │ ┌──────▼──────┐ │
│ │ Clade Agent │ │                  │ │ Clade Agent │ │
│ └──────┬──────┘ │                  │ └──────┬──────┘ │
└────────┼────────┘                  └────────┼────────┘
         │                                    │
         │           HTTP + JSON              │
         └───────────────┬────────────────────┘
                         │
                  ┌──────▼──────┐
                  │ Clade Relay │
                  │  (FastAPI)  │
                  └─────────────┘
```

**Uloge:**
- **Clade Relay** — dispatcher. Validira tokene, queue-uje poruke, ne cita sadrzaj (HMAC ga sprecava da forge-uje neopaženo). U MVP: localhost. U produkciji: VPS sa TLS.
- **Clade Agent** — lokalni daemon na svakoj peer masini. Stdio MCP server prema Claude Code-u, HTTPS klijent ka relay-u. Drzi lokalni audit log.
- **Claude Code** — koristi Clade Agent kao MCP server. Vidi izlozene tool-ove (`clade_send`, `clade_ask`, `clade_inbox`).

---

## 3. Roadmap — od POC veceras do produkcije

### Faza 0 — MVP POC (VECERAS, 2-3 sata)

**Cilj:** Dve Claude Code instance na *istom racunaru* razmenjuju poruku kroz lokalni relay.

- [ ] Relay (Python + Flask/FastAPI, bez auth) na `localhost:7777`
- [ ] Agent (Python stdio MCP) sa 3 tool-a: `clade_send`, `clade_ask`, `clade_inbox`
- [ ] Config fajl: `~/.clade/config.yaml` per agent (my_id, relay_url, peers)
- [ ] In-memory message queue u relay-u (Python `dict` + `asyncio.Queue`)
- [ ] `.mcp.json` snippet za Claude Code da pokupi agenta
- [ ] **Demo:** u terminal A pokrenes Claude sa agentom "alice", u terminal B sa "bob"; Alice pita Bob-a "koliko je 2+2" → Bob odgovori → Alice vidi odgovor

**Out of scope za Fazu 0:** auth, HMAC, TLS, replay protection, persistence, deployment.

### Faza 1 — Sigurnosni layer ✓ (2026-05-17)

- [x] Bearer token per agent (`relay/tokens.json` + `bearer_token` u config-u)
- [x] HMAC-SHA256 E2E per-pair (relay ne moze da forge-uje, samo forward-uje)
- [x] Nonce + timestamp anti-replay (5min window, in-memory dedup)
- [x] Peer allowlist na agent strani (i na relay-u preko token mapping-a)
- [x] Audit log: SQLite lokalno na agent-u (`/tmp/clade-{agent}-audit.db`)
- [x] `scripts/gen-keys.sh` — generise sveze tokene + HMAC secrets

Verifikovano kroz 6 scenarija (vidi commit poruku):
no-auth → 401, bad token → 401, stale timestamp → 400, replay → 400,
tampered HMAC → relay accept ALI receiver odbacuje, happy path → 200.

Odlozeno za Faza 1.5: Redis stream na relay-u (i dalje in-memory),
token rotation CLI (manual rotacija OK za 2-3 peer-a).

### Faza 2 — Deployment ✓ (artefakti, 2026-05-17)

Dve deploy varijante za razlicite use case-ove:

| Varijanta | Use case | TLS | Compose fajl |
|---|---|---|---|
| **LAN/VPN** | Peer-ovi u istoj mrezi (npr. WireGuard) | NE (VPN enkriptuje) | `deploy/docker-compose.lan.yml` |
| **Public VPS** | Peer-ovi razdvojeni internetom | DA (Let's Encrypt) | `deploy/docker-compose.yml` + Caddy |

Code:
- [x] Redis backend (`relay/store.py` + `RedisStore`, graceful fallback na InMemory)
- [x] /health endpoint vraca store backend + redis_ok flag

Artefakti:
- [x] `relay/Dockerfile` (Python 3.13-slim, healthcheck)
- [x] `deploy/docker-compose.yml` — Public stack (relay + redis + caddy)
- [x] `deploy/docker-compose.lan.yml` — LAN stack (relay + redis, no caddy)
- [x] `deploy/Caddyfile` (samo za public varijantu, Let's Encrypt automatski)
- [x] `deploy/systemd/clade-agent.service` template
- [x] `deploy/DEPLOY.md` — obe varijante step-by-step, troubleshooting

PENDING (korisnik radi):
- [ ] LAN: pokrenuti `docker compose -f docker-compose.lan.yml up -d` na
  host masini u LAN-u/VPN-u (api server ili odvojena masina)
- [ ] ILI Public: VPS provisioning + DNS + deploy

Lokalni dev (in-memory) i dalje radi nepromenjeno — REDIS_URL nije setovan,
fallback je transparentan.

### Faza 3 — Prvi pravi peer (Katana server) (0.5 dan)

- [ ] Mejl Predragu sa demom + zahtevom da pokrene agenta na api serveru
- [ ] Pomocna setup-skripta za njegovu masinu (curl install)
- [ ] Token + HMAC secret razmena (van-kanalno, ne preko mejla)
- [ ] CLAUDE.md template za njegov agent (sa scope-om: read-only DB, roadmap dokument)
- [ ] End-to-end test: Frontend-Claude pita Katana-Claude "koliko ABA igraca u staging-u" → odgovor

### Faza 4 — Productionizacija ✓ (2026-05-17)

- [x] Outbox buffer + exponencijalni backoff (`agent/outbox.py`)
  - Send/reply koji fail-uju (5xx, network) → SQLite outbox umesto gubitka
  - Backoff schedule: 1/2/4/8/16/30s, max 6 attempts → dead-letter
  - Lazy flush: svaki tool poziv pokusa da flush-uje pending poruke
  - `clade_outbox_status` MCP tool za debug
- [x] Pytest smoke suite (`tests/`)
  - 11 tests, spawn-uju sopstvenu relay instancu na slobodnom portu
  - Coverage: security (auth, replay, sender spoof, cross-inbox isolation),
    E2E (send/inbox, ask/reply, HMAC tamper detection, outbox queueing)
  - `./scripts/test.sh` ili `.venv/bin/python -m pytest tests/`

Odlozeno za Faza 5:
- [ ] Web UI za audit log (basic HTML form + table)
- [ ] Geo-anomaly detekcija (alert ako token koristi novu IP)

### Faza 5 — Reuse za drugi projekat ✓ (2026-05-17)

- [x] Generic config (vec gotovo od Faze 0 — peers iz YAML-a)
- [x] Pip-installable paket — `dist/clade_a2a-0.5.0-py3-none-any.whl`
  - Entry points: `clade-relay`, `clade-agent`, `clade-init`
  - `pyproject.toml` sa MIT licencom + hatchling build backend
- [x] `clade init --peers X Y Z` CLI — bootstrap novog projekta:
  - Generise tokens.json + per-peer YAML + .mcp.json snippete
  - Pair-wise HMAC secrets (svaki par dobija svoj shared secret)
  - CLAUDE.md template + Quickstart README
  - File permissions 0600 na secrets
- [x] Web UI za audit log — `GET /ui/audit` (HTML + JS, same-origin XHR)
  - Bearer token u localStorage, filter po peer + kind, auto-refresh 3s
- [x] Web UI iz Faza 4 sad zatvoren

Odlozeno:
- [ ] Multi-tenancy (per-token namespace u Redis-u) — premature za sad,
  jedan deploy = jedan projekat
- [ ] Geo-anomaly detekcija — defense-in-depth, low ROI za LAN
- [ ] Public docs / open-source — pricamo kad imamo 2-3 stabilna konzumenta

---

## 4. Tehnoloski stek

### Relay
- Python 3.13
- FastAPI — HTTP + middleware
- Redis 7 — message queue, nonce cache (Faza 2+)
- Caddy 2 — TLS proxy
- Docker Compose — deployment

### Agent
- Python 3.13
- MCP Python SDK — stdio JSON-RPC server
- `httpx` — async klijent ka relay-u
- SQLite — lokalni audit log
- `pydantic` 2 — config + sheme
- `click` — CLI komande

### Hosting (Faza 2+)
- Hetzner CPX11 Frankfurt — 2vCPU/2GB/€4.51 mesecno
- Cloudflare DNS (besplatno, proxy off)
- Subdomain: `clade.symappsys.com`

---

## 5. Sigurnosni model (Faza 1+)

| # | Pretnja | Mitigacija |
|---|---------|-----------|
| T1 | Outsider otkrije relay URL | TLS + Bearer auth wall |
| T2 | Token procuri (git, log) | Rotacija mesecno, env vars, never-log |
| T3 | Relay kompromitovan | E2E HMAC: relay forward-uje, ne forge-uje |
| T4 | Peer masina kompromitovana | Geo-anomaly, token revoke, short TTL |
| T5 | MITM | TLS, HSTS |
| T6 | Replay | Nonce + timestamp, 5min dedup |
| T7 | Prompt injection od peer-a | Prompt isolation tagovi + CLAUDE.md disciplina |
| T8 | DDoS | Rate limit per token, Cloudflare ispred |

**Sta NIJE u MVP-u (i zasto):** mTLS client certs (preteranje za 2-3 peer-a), HSM za tokene (overkill, env vars OK), zero-knowledge proofs (Phase 5+), Signal-style forward secrecy (TLS + rotacija pokriva 95%).

---

## 6. Struktura repo-a

```
a2a/
├── README.md              ← ovaj fajl
├── ARCHITECTURE.md        ← detaljni tehnicki design (Faza 1+)
├── relay/                 ← Python relay server
│   ├── main.py
│   ├── pyproject.toml
│   └── tests/
├── agent/                 ← Python lokalni daemon
│   ├── main.py
│   ├── pyproject.toml
│   └── tests/
├── examples/
│   ├── alice-config.yaml  ← POC config za "alice"
│   ├── bob-config.yaml    ← POC config za "bob"
│   └── claude-mcp.json    ← Claude Code MCP config snippet
└── deploy/
    ├── docker-compose.yml
    ├── Caddyfile
    └── systemd/clade-agent.service
```

---

## 7. Brzi start (Faza 0)

```bash
# 1. Pokreni relay
cd relay && python3 main.py
# → "Clade relay running on http://localhost:7777"

# 2. U novom terminalu — pokreni Claude sa Alice agentom
export CLADE_CONFIG=/home/dusan/project/a2a/examples/alice-config.yaml
claude  # MCP automatski pokupi clade agent

# 3. U novom terminalu — pokreni Claude sa Bob agentom
export CLADE_CONFIG=/home/dusan/project/a2a/examples/bob-config.yaml
claude

# 4. U Alice terminalu: "Pitaj Bob-a koliko je 2+2"
# Alice Claude → clade_ask(to="bob", payload={question: "2+2"})
# Bob Claude vidi novu poruku u clade_inbox → odgovara → clade_reply(...)
# Alice Claude dobija odgovor
```

---

## 8. Sta moze da krene po zlu

- **Claude ne poziva inbox cesto dovoljno** → predugacak delay. Mitigacija: CLAUDE.md instrukcija "pozovi clade_inbox na pocetku svakog turn-a ako je proslo >60s".
- **Relay padne, agent ne zna** → outgoing poruke se gube. Mitigacija (Faza 4): outbox buffer + reconnect.
- **Prompt injection** → vec dokumentovano u 1.3. Disciplina u CLAUDE.md je glavni layer.
- **Token procuri** → audit log + mesecna rotacija + revoke endpoint.

---

## 9. Reference

- Inspiracija: dokumentacija `../kosarka/assets/clade-a2a-projekat.md.docx` (Dusan + Claude, 2026-05).
- MCP spec: https://spec.modelcontextprotocol.io/
- FastMCP: https://github.com/jlowin/fastmcp

---

*Generisano sa Claude Code asistencijom, 2026-05-17.*

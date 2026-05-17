# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Sta je ovo

Clade A2A — sigurnan A2A message bus za Claude Code instance. Pip paket `clade-a2a`, entry points `clade-relay`, `clade-agent`, `clade-init`. Citaj `README.md` za feature pregled i tri setup mode-a (deploy / wizard / manual).

**Kanonicki protokol je u `a2a-protocol.md` (trenutno v1.2.0)** — single source of truth za envelope schema, HMAC algoritam, MCP tool API, daemon model, file lock semantiku, thread persistence, clarify-back konvenciju, outbox monitor. Kad menjas A2A ponasanje, prvo edituj protokol pa bump verziju (SEMVER pravila u §12 protokola). Ne razbacuj duplikate informacija po CLAUDE.md fajlovima — `clade_cli/init.py` template-i samo referenciraju protokol.

## Komande

```bash
# Setup (jednom)
uv venv && uv pip install -e .

# Run tests (17 testova, spawn-uju sopstvenu relay instancu na free port-u)
./scripts/test.sh
# Single test:
.venv/bin/python -m pytest tests/test_agent_e2e.py::test_clade_message_send_fire_and_forget -v

# Run relay lokalno
.venv/bin/clade-relay --tokens relay/tokens.json --host 127.0.0.1 --port 7777

# Build wheel
.venv/bin/python -m build  # output u dist/

# E2E demo (alice ↔ bob, headless)
./scripts/start-relay.sh &
./scripts/demo-ask-reply.sh

# Lokalni multi-peer wizard / multi-machine deploy
./scripts/clade-wizard.sh    # 3 terminala na jednoj masini
./scripts/clade-deploy.sh    # generise scp bundle-ove za vise masina

# Ako start-<peer>-daemon.sh pukne sa "drugi daemon vec tece":
./scripts/clade-cleanup.sh   # nadje + ugasi daemon procese, lock + stale PID fajlove
./scripts/clade-cleanup.sh --dry-run     # samo prikazi sta bi se ugasilo
./scripts/clade-cleanup.sh --include-relay   # ugasi i relay
```

Nema linter / type-checker konfiguracije. Pre commit-a samo `./scripts/test.sh`.

## Arhitektura (big picture)

Tri komponente, gradjene preko 5 razvojnih faza (vidi `ROADMAP.md`):

1. **`relay/`** — FastAPI dispatcher. Bearer auth + nonce dedup + ts skju validacija. **NE cita HMAC** (E2E je posao receiver-a). Pluggable storage: in-memory ili Redis (`relay/store.py`, biranje preko `REDIS_URL`). `pending_asks` su uvek u memoriji — `asyncio.Future` nije serijabilan; restart fail-uje in-flight asks (svesno).

2. **`agent/main.py`** — stdio MCP server koji Claude Code spawn-uje preko `.mcp.json`. Implementira `clade_message` (kanonicki tool, v1.0.0+), `clade_inbox`, `clade_reply`, `clade_outbox_status`, plus deprecated `clade_send`/`clade_ask` wrappere (uklanjanje u v2.0.0). HMAC sign/verify, SQLite audit + outbox. Thread persistence helperi: `record_thread_message`, `load_thread_history`, `format_thread_for_prompt` (v1.1.0+).

3. **`agent/daemon.py`** — long-running poller koji peer masina drzi up. Polluje `/inbox` svake 2s, spawn-uje `claude --print --mcp-config <wd>/.mcp.json` za auto-reply na `ask` poruke. **Daemon je single-owner inbox-a** preko file lock-a (`<audit_db_dir>/<peer>-daemon.lock`); `clade_inbox` u agent-u vraca busy error ako lock zivi. Pored poll loop-a vrti i `outbox_monitor_loop` (v1.2.0) koji svakih 30s log-uje + flush-uje stale outbox poruke. Daemon-spawn Claude radi sa minimal headless profilom (env vars + settings.json u workdir-u).

**`clade_cli/init.py`** generise bootstrap (tokens.json, per-peer YAML, .mcp.json, slim CLAUDE.md, kopija `a2a-protocol.md`). Skripte u `scripts/` (`clade-wizard.sh`, `clade-deploy.sh`) ga koriste kao backend.

**Outbox** (`agent/outbox.py`) je SQLite tabela u istom DB-u sa audit log-om. Backoff schedule `[1,2,4,8,16,30]` sekundi × max 6 pokusaja → dead-letter. Fire-and-forget send/reply na network error ili 5xx automatski ide u outbox; sledeci tool poziv lazy-flush-uje. **Sinhroni `clade_message(expect_reply=True)` NE ide u outbox** — korisnik retry.

## Konvencije

- **Jezik:** kod, komentari, log poruke, docstring-ovi na srpskom latinica **bez dijakritike** ("sta", "moze", "vec" — ne "šta/može/već"). Markdown narativni delovi mogu sa dijakritikom. Identifikatori su engleski.
- **Komentari:** pisi *zasto*, ne *sta*. Ako nesto izgleda neobicno (npr. zasto `pending_asks` ostaje u memoriji), objasni razlog. Nema komentara koji ponavljaju kod.
- **Phase tracking:** module docstring-ovi nose "Faza X" labelu kad se uvodi nova funkcionalnost (`Faza 2`, `Faza 4 — ovaj fajl`). Novi protokol uvodi paralelno SEMVER (`v1.0.0` u docstring-u + bump u `a2a-protocol.md` §11).
- **Backwards-compat:** API breaking change-evi (npr. uklanjanje `clade_send`/`clade_ask`) najavljuju se bar jedan minor pre uklanjanja. Wrapperi koji warn u stderr su prihvatljivi most.

## Cesti zamke

- **Daemon i Claude konkurentno u inbox-u:** ne pozivaj `clade_inbox` u kodu/CLAUDE.md kad daemon tece — vratice busy. To je *feature* (file lock §6 protokola), ne bug.
- **HMAC mismatch izmedju peer-ova:** shared secret mora biti IDENTICAN u oba `<peer>.yaml` fajla pod uzajamnim kljucevima (alice.yaml `peers.bob` == bob.yaml `peers.alice`). `clade_cli/init.py` to garantuje za novi bootstrap; ako ručno menjas, proveri.
- **Replay / clock skew:** nonce + ts ±5min. Ako test-ovi flap-uju na timestamp gresci, problem je NTP ne kod.
- **Tests reload-uju agent.main:** zbog import-time config load-a, testovi koriste `_load_agent_module(config_path)` koji pop-uje iz `sys.modules` pre re-importa. Drzi taj pattern kad dodajes nove testove sa drugacijim configs.

## Otvorene tacke

`samozapazanja.md` u root-u sadrzi peer-to-peer dijalog izmedju dva A2A agenta o sledecim iteracijama. P0 → v1.0.0, P1 → v1.1.0, P2 → v1.2.0. Sve tri faze samozapazanja isporucene. Sledece: stvarni feedback iz produkcije (Faza 3 — Predrag/Katana).

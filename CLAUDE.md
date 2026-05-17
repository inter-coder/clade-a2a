# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Sta je ovo

Clade A2A — sigurnan A2A message bus za Claude Code instance. **v2.0 grana (v2-arch) je u toku** — single proces po peer-u sa HTTP+unix-socket dual transport, systemd `Type=notify`, jedan `clade` CLI binary. Stara v1.x arhitektura (`agent/main.py`, `agent/daemon.py`, `clade_cli/init.py`, 5 startup skripti) **uklonjena u PR#5** — vidi tag `v1.2.0` na master grani za poslednje stable v1.x.

Trenutni paket-naziv `clade-a2a` (verzija `1.0.0rc1`), entry points: `clade` (glavni CLI: `clade serve` / `clade init`) i `clade-relay` (cross-host scenariji, opcioni).

**Kanonicki protokol je u `a2a-protocol.md`** — single source of truth za envelope schema, HMAC algoritam, MCP tool API. v2 protokol bump cek se za poslednji PR (PR#6 — handshake i CLI dodatne komande); za sad i dalje v1.2.0 sve dok celokupan v2 set ne sleti.

## Komande

```bash
# Setup (jednom)
uv venv && uv pip install -e .

# Run tests
./scripts/test.sh
# Single test:
.venv/bin/python -m pytest tests/test_v2_mcp_http.py::test_mcp_clade_message_ask_roundtrip -v

# Bootstrap (dry-run preview)
.venv/bin/python -m clade init --self katana --peer dusan

# Bootstrap (commit)
.venv/bin/python -m clade init --self katana --peer dusan --apply

# Run peer proces (foreground, za development)
.venv/bin/python -m clade serve --config ~/.config/clade/peers.yaml

# Run kroz systemd (production)
systemctl --user daemon-reload && systemctl --user enable --now clade-katana
journalctl --user -u clade-katana -f

# Run relay (cross-host scenario — opcioni)
.venv/bin/clade-relay --tokens relay/tokens.json --host 127.0.0.1 --port 7777

# Cleanup zaglavljenih daemon procesa (zaostavstina v1.x)
./scripts/clade-cleanup.sh   # vidi prvo dry-run pa --apply
```

Build wheel: `.venv/bin/python -m build`. Nema linter/type-check setup; pre commit-a samo `./scripts/test.sh`.

## Arhitektura (big picture)

Sve novo zivi u **`clade/`** paketu. Stara `agent/` je obrisana u PR#5. Komponente:

1. **`clade/server.py`** — `clade serve --peer X --config peers.yaml`. Always-on asyncio proces po peer-u. Drzi:
   - **UnixSocketTransport.serve()** — peer-to-peer inbox preko `/run/user/<uid>/clade/<peer>.sock`
   - **uvicorn** sa fastmcp Starlette app-om — HTTP `127.0.0.1:port` za MCP (`/mcp/`) + `/health`
   - **outbox_retry_loop** — svakih 30s retry-uje pending entries
   - **sd_notify watchdog** — `READY=1`, `WATCHDOG=1`, `STOPPING=1` (no-op ako nije pod systemd)
   - Audit DB + ThreadCache + Outbox state

2. **`clade/transport/`** — `Transport` Protocol + dva impl: `UnixSocketTransport` (primarni on-host) i `HttpRemoteTransport` (relay-mediated cross-host). Length-prefixed JSON framing.

3. **`clade/mcp_server.py`** — `build_mcp_app(server)` factory: FastMCP HTTP app sa `clade_message` i `clade_outbox_status` tools (closure nad Server-om). Plus `/health` Starlette Route.

4. **`clade/audit.py`** — SQLite write-through (WAL + NORMAL synchronous). Schema sa `status IN ('delivered', 'rejected', 'failed', 'pending')` pokriva outbox stanje. Source of truth za istoriju.

5. **`clade/thread_cache.py`** — In-memory ThreadCache (TTL + bounded deque). `prefill_from_audit()` hidracija na potrebu. Zamenjuje v1 `thread_history` SQLite tabelu.

6. **`clade/outbox.py`** — `Outbox` (audit-iznad-pending-status + `outbox_meta` tabela za scheduling), `send_with_retry()` (in-process [0.1, 0.5, 2.0]s × 3 → enqueue), `outbox_retry_loop()` background task. `MAX_ATTEMPTS=20` (~10 min) → dead-letter.

7. **`clade/peers_config.py`** — `version: 2` schema (per-peer `transport`/`role`, `${env:VAR}` substitucija za secrets). `interactive_peers()` helper za filter.

8. **`clade/init.py`** — `clade init` generator. Dry-run default, `--apply` commit. Generise peers.yaml + systemd unit + `.mcp.json` (sa `type=http`, `url=http://127.0.0.1:PORT` po Claude Code MCP klijent verifikaciji).

9. **`clade/templates.py`** — pure-Python render-i za systemd unit (`Type=notify`, `WatchdogSec=60s`), `.mcp.json`, default peers.yaml.

10. **`relay/`** — i dalje koristi se za `--remote` cross-host scenarije. Bearer auth + nonce dedup. Pluggable storage (in-memory ili Redis). Za on-host v2 NE treba.

**Envelope** u v2 (`clade/envelope.py`): `thread_id` i `reply_to` su **TOP-LEVEL** polja (ne `payload._meta` kao v1). `protocol_version: "2.0.0"` strict major match handshake (planiran PR#6).

## Konvencije

- **Jezik:** kod, komentari, log poruke, docstring-ovi na srpskom latinica **bez dijakritike** ("sta", "moze", "vec"). Markdown narativni delovi mogu sa dijakritikom. Identifikatori engleski.
- **Komentari:** pisi *zasto*, ne *sta*. Ako nesto izgleda neobicno (npr. zasto `pending_asks` ostaje u memoriji u relay-u, ili zasto ThreadCache nije persistovan), objasni razlog. Nema komentara koji ponavljaju kod.
- **SEMVER protokol:** promene `a2a-protocol.md` idu kroz §11 changelog + bump major/minor/patch po §12 pravilima. Ne razbacuj duplikate informacija po CLAUDE.md template-ima — `clade init` generise slim CLAUDE.md koji referencira protokol.

## Cesti zamke

- **MCP klijent transport:** Claude Code 1.x podrzava SAMO `stdio`, `http`, `sse` (deprecated). `unix://` URL NIJE podrzan direktno — zato peer proces izlozuje `http://127.0.0.1:PORT` za MCP klijent + odvojeno unix socket za peer-to-peer. Verifikovano u PR#1.
- **Replay / clock skew:** nonce + ts ±5min na relay strani. Za on-host unix socket transport ne primenjuje se (filesystem permission-i guard).
- **`status='pending'` u audit-u JE outbox stanje:** ne dva odvojena dat tipa. `outbox_meta` tabela cuva retry scheduling state. `Outbox.mark_delivered` brise meta + update-uje status.
- **`clade init` workdir putanje:** default je `~/.local/state/clade/workdirs/<peer>/` (XDG state). Testovi MORAJU prosledjivati `workdir_root` override (preko `plan()`) da ne bi kontaminirali home folder.

## Otvorene tacke

`samozapazanja.md` u root-u — runda 2, v2.0 arhitekturni plan. v1.x faze (samozapazanja0.md backup, untracked) → v1.2.0 stable na master grani. v2 plan ide u 6 PR-ova preko v2-arch grane; trenutno smo na PR#5 (cleanup gotov), PR#6 sledece (handshake + `clade status/logs/send` subcommand-i).

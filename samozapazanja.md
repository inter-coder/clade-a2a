# Samozapazanja — Clade A2A sistem, runda 2 (v2.0 razgovor) — TEHNICKI

**Autor:** katana (interactive sesija)
**Datum:** 2026-05-17
**Kontekst:** Korisnik je nakon v1.2.0 i dalje nezadovoljan — sistem je za njega previse komplikovan. Razgovor sa dusanom (headless peer) u thread-u `simplify-2026-05-17`, tri turn-a. Prethodna runda zapisana u `samozapazanja0.md` i isporucena kroz v1.0 → v1.2. Ovaj dokument je sledeci ciklus, **tehnicki potkrepljen** — dev tim treba da moze direktno implementirati svaku tacku.

---

## 1. Dijagnoza: zasto je v1.2.0 pogorsao stvar

P0–P2 iz prve runde su isporuceni korektno (single tool, file lock, thread persistence, clarify-back, outbox monitor, minimal headless profile). Ali rezultat je **vise koncepata, ne manje**. Korisnik mora da razume:

- `_clarify` flag i `[CLARIFY]` marker konvenciju
- `_thread_id` semantiku i `thread_history` SQLite tabelu
- Daemon vs interactive vs relay vs agent ulogu (4 komponente)
- `dusan.yaml` + `mcp-config-dusan.json` + `.mcp.json` u workdir-u + `.claude/settings.json` sa skill overrides (4 fajla po peer-u)
- 5 startup skripti
- 5 vidljivih MCP tool-ova (`clade_message`, `clade_outbox_status`, `clade_reply`, plus deprecated `clade_send`/`clade_ask`)

Svako v1.x dodavanje je tehnicki bilo dobro, ali nijedno nije na **critical path-u** za 95% slucaja: *"posalji poruku, dobij odgovor"*. To je feature creep, ne polish.

**Glavni pojedinacni bug iz prakse (dusan):** daemon poll loop *warm-stall* nakon ~1h idle-a. Thread persistence to NIJE resilo, samo je smanjilo gubitak konteksta. Korisnik vidi "katana ne odgovara" i restartuje pogresnu komponentu. To je signal za arhitekturni redesign, ne za jos jedan patch.

---

## 2. Predlozena v2.0 arhitektura — tehnicki detalji

### 2.1 Jedan dugorocni proces po peer-u

**Trenutno:** daemon (poll loop) + agent (MCP stdio, spawned po sesiji interactive Claude-a) = 2 procesa po peer-u, plus relay process. File lock se koristi da bi se sprecio race izmedju njih.

**Cilj:** jedan always-on proces po peer-u, koji je istovremeno MCP server (za interactive Claude) i ask-handler (spawn `claude --print` za auto-reply). Inbox vlasnik je definicijski jedan proces — **file lock postaje suvisan i brise se**.

**Process layout (jedna asyncio event loop instanca):**

```
clade-<peer> process
├── HTTP server (uvicorn ili aiohttp, vidi 2.2)
│   ├── POST /mcp        — MCP JSON-RPC endpoint (klijent: interactive Claude)
│   ├── POST /inbox      — peer-to-peer message delivery (klijent: drugi clade-<peer>)
│   └── GET  /health     — JSON status (klijent: clade status, debug)
├── Outbox retry loop    — svakih 30s sken-uje sopstveni outbox, retry sa exp. backoff
├── Ask handler pool     — asyncio.Semaphore(N=4), spawn-uje claude --print procese
├── Audit writer         — write-through SQLite (vidi 2.5)
└── Thread cache         — in-memory dict sa TTL (vidi 2.6)
```

**Bez pozadinskog poll loop-a prema relay-u.** Receiver je vec live HTTP listener; sender pushuje direktno. Cisti out problem warm-stall-a — nema task-a koji moze zaglaviti u idle-u.

### 2.2 Transport sloj — HTTP preko unix socket-a

**Odluka:** HTTP transport preko unix socket-a, ne custom JSON-RPC framing.

**Razlog (dusan):** Claude Code MCP klijent pouzdano podrzava `stdio` i `HTTP/SSE`. Raw newline-delimited JSON-RPC bi nas naterao da pisemo custom MCP klijent — ne vredi. **Dev tim mora verifikovati** da li Claude Code 1.x MCP klijent prihvata `unix:///` URL-ove direktno; ako ne, fallback je `127.0.0.1:PORT` sa randomized portom upisanim u `~/.config/clade/runtime/<peer>.port`.

**Path konvencija:**
```
/run/user/<uid>/clade/<peer>.sock   # primarni
127.0.0.1:<port>                    # fallback / debug
```

Mode 0600 (samo isti UID moze konektovati). Cleanup na exit kroz finally blok + sigterm handler.

**`Transport` interfejs (Python, ~50 LOC):**

```python
class Transport(Protocol):
    async def deliver(self, envelope: Envelope, to_url: str) -> DeliveryResult: ...
    async def serve(self, handler: Callable[[Envelope], Awaitable[Response]]) -> None: ...

class UnixSocketTransport(Transport): ...   # primarna
class HttpRemoteTransport(Transport): ...   # samo za --remote, ide kroz relay
```

**Bez factory pattern-a, bez registry-ja, bez plugin sloja.** Dve konkretne klase, izabere se na startup-u iz `peers.yaml` (`transport: unix` ili `transport: relay`).

**Kriticno upozorenje (dusan):** **NE uvuci FastAPI ili Starlette** za ovaj sloj. Custom JSON-RPC 2.0 dispatcher u ~200 LOC je dovoljan. FastAPI dovuce middleware lanac, validation framework, OpenAPI generator — sve sto ne treba i sve sto otvara nove bug klase. `aiohttp.web` ili goli `asyncio.start_unix_server` + `json.loads` su pravi nivo.

### 2.3 Subprocess hardening — `claude --print` spawn

**Trenutno:** daemon spawn-uje sa minimal env + skill overrides. Tri konkretne stvari koje su se vidjale u praksi (dusan):

```python
async def spawn_claude_print(prompt: str, peer_id: str, cfg: PeerConfig) -> str:
    cmd = ["claude", "--print",
           "--mcp-config", str(cfg.workdir / ".mcp.json"),
           "--append-system-prompt", prompt]
    env = os.environ.copy() | {
        "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cfg.workdir,                      # KONFIGURABILAN per peer (peers.yaml)
        env=env,
        stdin=asyncio.subprocess.DEVNULL,     # MORA DEVNULL, ne PIPE (hang risk)
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.terminate()                      # SIGTERM
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()                       # SIGKILL
            await proc.wait()
        raise AskTimeoutError(f"claude --print >120s, killed")
    return stdout.decode()
```

**Sta NIJE u ovom planu (dusan):** RLIMIT_AS / RLIMIT_CPU. Komplikuje config, 99% slucajeva ne pravi razliku. Dodaj tek ako vidis OOM u produkciji.

**`cwd` konfigurabilnost:** v1.x je hardkodovao `/tmp/clade-daemon-<peer>-<hash>/`. Korisnik je trazio da mu dozvoli da spawn-a u svom radnom direktorijumu. U `peers.yaml`:

```yaml
peers:
  katana:
    workdir: ~/clade-projects/test/workdirs/katana   # tilde-expand on load
    # ...
```

Default: privremen dir generisan u `~/.local/state/clade/workdirs/<peer>/` (ne `/tmp/` — `/tmp` se cisti na reboot, gubimo `.mcp.json`).

### 2.4 `peers.yaml` schema + `clade init` flow

**Jedan fajl po masini:** `~/.config/clade/peers.yaml`. Sve ostalo se izvodi iz njega.

```yaml
version: 2                     # protocol version
self: katana                   # koji peer si TI na ovoj masini
peers:
  katana:
    transport: unix
    socket: /run/user/1000/clade/katana.sock
    workdir: ~/clade-projects/test/workdirs/katana
    role: interactive          # interactive | headless | both
  dusan:
    transport: unix
    socket: /run/user/1000/clade/dusan.sock
    workdir: ~/.local/state/clade/workdirs/dusan
    role: headless
  remote_peer:
    transport: relay
    relay_url: https://relay.example.com
    secret_hex: ${env:CLADE_REMOTE_SECRET}    # iz env, ne u fajlu
```

**`clade init` flow:**

```
$ clade init --self katana
1. Generise ~/.config/clade/peers.yaml ako ne postoji (interactive prompts)
2. Generise ~/.config/systemd/user/clade-katana.service iz template-a
3. Generise <workdir>/.mcp.json za svaki peer sa role=interactive
4. Stampa: "Run: systemctl --user enable --now clade-katana"
```

Nikakav side effect bez `--apply` flag-a (dry-run by default — pokaze sta bi generisao).

### 2.5 Audit SQLite schema (write-through, source of truth)

**Pravilo (dusan):** Ako thread postaje in-memory + TTL, audit log MORA biti write-through i jedini source of truth za history. Commit pre nego sto se vrati ACK senderu.

```sql
CREATE TABLE audit (
  msg_id          TEXT PRIMARY KEY,
  ts_ms           INTEGER NOT NULL,
  direction       TEXT NOT NULL CHECK(direction IN ('in', 'out')),
  peer            TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK(kind IN ('send', 'ask', 'reply')),
  correlation_id  TEXT,
  thread_id       TEXT,
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL CHECK(status IN ('delivered', 'rejected', 'failed', 'pending')),
  error           TEXT
);
CREATE INDEX idx_audit_ts        ON audit(ts_ms DESC);
CREATE INDEX idx_audit_peer_ts   ON audit(peer, ts_ms DESC);
CREATE INDEX idx_audit_thread    ON audit(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX idx_audit_correlation ON audit(correlation_id) WHERE correlation_id IS NOT NULL;

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA wal_autocheckpoint = 1000;
```

**`synchronous = NORMAL` (ne FULL).** Razlog: WAL + NORMAL je industry standard. Gubitak je max poslednjih nekoliko ms na power loss — za audit nije katastrofa (poruka je vec predata na transport sloju). FULL je 5–10x sporiji i ne vredi za ovaj use case.

**NE radi checkpoint na svaki ACK.** `wal_autocheckpoint=1000` (default) radi sam. Na graceful shutdown: `PRAGMA wal_checkpoint(PASSIVE)`.

**File:** `~/.local/state/clade/<peer>/audit.db` (NE `/tmp`, NE workdir).

### 2.6 Thread cache — in-memory sa TTL

**Zameni** `thread_history` SQLite tabelu (v1.1.0) sa jednostavnim in-memory dict-om:

```python
class ThreadCache:
    def __init__(self, ttl_s: int = 3600, max_msgs_per_thread: int = 10):
        self._cache: dict[str, deque[ThreadMsg]] = {}
        self._last_access: dict[str, float] = {}
        self._ttl = ttl_s
        self._max = max_msgs_per_thread

    def append(self, thread_id: str, msg: ThreadMsg) -> None: ...
    def get_context(self, thread_id: str) -> list[ThreadMsg]: ...
    def _evict_expired(self) -> None: ...   # called on every access, O(expired)
```

**Reasoning:** 95% threadova je 1–2 turn-a. Persistencija preko crash-a nije neophodna (audit DB je tu za forenziku). Ako proces crash-uje usred dugog threada, sledeci ask gubi context — to je prihvatljivo, ne data loss.

**Memorija budget:** 10 msg × ~2KB × 1000 active threadova = ~20MB worst-case. Bezbedno.

### 2.7 Outbox sa exponential backoff

**Trenutno:** lazy flush + outbox_monitor_loop u daemon-u sa 30s warn.

**v2.0 model:** sender-driven retry, fixed schedule.

```python
RETRY_SCHEDULE_S = [0.1, 0.5, 2.0]   # 3 in-process pokusaja u ~2.6s
OUTBOX_RETRY_INTERVAL_S = 30          # background loop

async def send(envelope) -> SendResult:
    for delay in RETRY_SCHEDULE_S:
        try:
            return await transport.deliver(envelope)
        except (ConnectionRefusedError, TimeoutError) as e:
            await asyncio.sleep(delay)
            last_err = e
    # In-process retries iscrpeni, predaj outbox-u
    outbox.enqueue(envelope, last_error=str(last_err))
    return SendResult(queued=True)

# Background loop
async def outbox_retry_loop():
    while not shutdown_event.is_set():
        await asyncio.sleep(OUTBOX_RETRY_INTERVAL_S)
        for entry in outbox.pending():
            if entry.attempts > MAX_ATTEMPTS:
                outbox.mark_dead(entry)
                continue
            try:
                await transport.deliver(entry.envelope)
                outbox.mark_delivered(entry)
            except Exception:
                outbox.bump_attempts(entry)   # sledeci pokusaj za 30s
```

**Push notification (v1.2.0 P2#8) se sece.** Connection refused → fallback na outbox je dovoljan signal. Nema zasebnog `OUTBOX_STALE_WARN_S` ni alert-a u terminalu.

**Dead letter:** `MAX_ATTEMPTS = 20` (10 minuta retry-ja). Posle toga: entry ide u `outbox.dead` tabelu sa `dead_reason`, log warning, ne crash.

### 2.8 Health endpoint

```python
@app.get("/health")
async def health() -> dict:
    return {
        "peer": cfg.self,
        "version": __version__,
        "protocol_version": "2.0.0",
        "uptime_s": int(time.monotonic() - START_TIME),
        "inbox_processed_total": metrics.inbox_count,
        "outbox_pending": outbox.pending_count(),
        "outbox_dead": outbox.dead_count(),
        "last_message_at_ms": metrics.last_msg_ts,
        "active_asks": len(pending_asks),
        "thread_cache_size": len(thread_cache),
    }
```

**Klijent:** `clade status --peer <name>` (CLI) i interactive Claude (preko `clade_message` ili direct curl).

### 2.9 Graceful shutdown + sd_notify watchdog

**Tri zahteva:**
1. SIGTERM mora finalize in-flight asks (wait do 10s) pre exit-a.
2. `PRAGMA wal_checkpoint(PASSIVE)` pre close audit DB.
3. Cleanup unix socket fajla (`os.unlink(socket_path)`).
4. **sd_notify watchdog ping** svakih `WatchdogSec/2` (30s ako je `WatchdogSec=60`).

```python
import signal, sdnotify

notifier = sdnotify.SystemdNotifier()
shutdown_event = asyncio.Event()

def handle_sigterm(*_):
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

async def watchdog_loop():
    while not shutdown_event.is_set():
        notifier.notify("WATCHDOG=1")
        await asyncio.sleep(30)

async def main():
    # ... setup ...
    notifier.notify("READY=1")
    asyncio.create_task(watchdog_loop())
    await shutdown_event.wait()
    notifier.notify("STOPPING=1")
    await graceful_shutdown(timeout_s=10)
```

**Reasoning (dusan):** `WatchdogSec` u systemd-u je tacno alat koji je nedostajao u v1.x — kad poll loop zaglavi, watchdog ga ubije i `Restart=on-failure` ga vrati. `Type=simple` + ExecStartPost polling je hack koji nikada ne uhvati prave zamrznute event loop-ove.

### 2.10 Protocol version handshake

Na MCP konekciji (prvi `initialize` request) ili na `POST /inbox` (peer-to-peer), oba strane salju `protocol_version`:

```json
// POST /inbox (header ili u envelope)
{
  "envelope": {...},
  "protocol_version": "2.0.0"
}

// Response ako mismatch:
HTTP/1.1 426 Upgrade Required
{
  "error": "protocol_mismatch",
  "expected": "2.0.0",
  "received": "1.2.0",
  "hint": "Upgrade peer 'dusan' to clade>=2.0"
}
```

**Strict major match, loose minor/patch.** v2.0.x prihvata v2.x.y, odbija v1.x.y i v3.x.y.

Bez ovoga: silently truncated fields → bug koji nestane nakon 6 meseci debugovanja.

### 2.11 `Envelope` kao typed dataclass

**Trenutno:** svaki sloj parsira `dict` po svom. Field drift je samo pitanje vremena.

**Predlog (msgspec za brzinu, ali pydantic je OK ako tim vec koristi):**

```python
import msgspec

class Envelope(msgspec.Struct, kw_only=True):
    msg_id: str
    from_agent: str
    to_agent: str
    kind: Literal["send", "ask", "reply"]
    payload: dict
    nonce: str
    timestamp_ms: int
    hmac: str | None = None              # opcioni za on-host unix socket
    correlation_id: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    protocol_version: str = "2.0.0"

# Single source of truth, oba peer-a importuju isti tip
```

`thread_id` i `reply_to` postaju **top-level polja**, ne `payload["_thread_id"]` (cleaner, type-checkable).

### 2.12 systemd user unit

`~/.config/systemd/user/clade-<peer>.service`:

```ini
[Unit]
Description=Clade A2A peer (%i)
After=network.target

[Service]
Type=notify
ExecStart=/usr/local/bin/clade serve --peer %i
Restart=on-failure
RestartSec=2s
WatchdogSec=60s
StandardOutput=journal
StandardError=journal
Environment=CLADE_CONFIG=%h/.config/clade/peers.yaml

[Install]
WantedBy=default.target
```

**Korisnicki interfejs:**
```bash
systemctl --user enable --now clade-katana
systemctl --user status clade-katana
journalctl --user -u clade-katana -f
```

Brisemo: `start-katana-daemon.sh`, `stop-katana.sh`, `*.pid` fajlovi.

### 2.13 Single CLI entry point — `clade`

**Trenutni split** (`agent/main.py`, `agent/daemon.py`, `relay/main.py`) je istorijski accident. Spojiti u jedan binary:

```
clade serve   --peer <id>                Pokreni always-on proces (systemd ExecStart)
clade init    [--self <id>] [--apply]    Generisi config i systemd unit
clade relay   [--port 8000]              Pokreni relay (samo za --remote scenarije)
clade status  [--peer <id>]              Pita /health endpoint, formatira tabelu
clade logs    --peer <id> [--tail N]     Tail audit DB + journalctl
clade send    --peer <id> <message>      CLI klijent za one-shot poruke (debug, skripte)
```

**Install story:** `pipx install clade-a2a`. Binary, man page, bash completion sve u jednom paketu. PyInstaller single-file je opcioni za korisnike bez Python-a.

---

## 3. Cold-start budget (informativno)

Dusan je razbio 3s spawn `claude --print`:

| Faza | ms | Optimizacija |
|---|---|---|
| Python interpreter + import | 400–600 | Nema (inherentno) |
| MCP server discovery + handshake | 800–1200 | Pre-generated `.mcp.json` u workdir-u stedi ~100–200ms |
| Skill registration | 300–500 | `CLAUDE_CODE_DISABLE_POLICY_SKILLS=1` cuts ~> 0 |
| Model warmup / first token | 500–800 | Nema (server-side) |
| **Total** | **2000–3100ms** | **Realan target sa opt: 1.5–2s** |

**Sub-second spawn nije moguc** dok god se spawn-uje ceo Python+Claude proces. Ako se to pokaze kao bottleneck u P3, opcija je dugorocni Claude pool (1 ili 2 idle procesa po peer-u koji cekaju na pipe-u) — ali to vraca dual-process kompleksnost i nije vredno bez profil benchmarka.

---

## 4. Migration plan — 6 PR-ova, ~1 nedelja

Mali PR-ovi, sekvencijalni, svaki mergebilan nezavisno:

### PR #1 (2 dana): Transport sloj + Envelope dataclass
- `clade/envelope.py` — msgspec dataclass
- `clade/transport/` — `Transport` interfejs + `UnixSocketTransport` + `HttpRemoteTransport`
- Unit testovi (round-trip serialize, mock unix server)
- **Bez** integracije u postojeci agent/daemon — pure new code

### PR #2 (2 dana): `clade serve` always-on proces
- `clade/server.py` — asyncio main loop, HTTP routes, lifecycle
- sd_notify integracija + watchdog loop
- Audit DB write-through (zamenjuje stari audit kod)
- ThreadCache (in-memory, brise `thread_history` tabelu)
- **U ovoj fazi:** `clade serve` radi paralelno sa starim daemon-om. Test side-by-side.

### PR #3 (1 dan): Outbox refactor
- Sender-driven retry sa `RETRY_SCHEDULE_S = [0.1, 0.5, 2.0]`
- Background `outbox_retry_loop` (30s)
- Dead letter posle 20 attempts
- Brise: `outbox_monitor_loop` iz daemon-a, push notification logiku

### PR #4 (1 dan): `clade init` + systemd unit + peers.yaml
- `clade/init.py` — generator
- Template `clade-<peer>.service`
- Dry-run by default, `--apply` za commit
- **U ovoj fazi:** korisnik moze realno preci na novi sistem

### PR #5 (0.5 dan): Deprecation cleanup
- Ukloni `clade_send` i `clade_ask` wrappere (mi smo jedini korisnici)
- Ukloni `[CLARIFY]` marker logiku i `_clarify` flag
- Ukloni `thread_history` SQLite tabelu i migration script
- Ukloni daemon poll loop, file lock kod
- Ukloni 5 startup skripti, replaced by systemctl
- Ukloni duplicate CLAUDE.md kopije u workdir-ovima

### PR #6 (0.5 dan): Version handshake + health endpoint + `clade status`/`logs`/`send`
- Handshake na `/inbox` i MCP `initialize`
- `/health` JSON endpoint
- CLI subcommands (`status`, `logs`, `send`)

**Posle PR #6:** v2.0.0 tag, bump `a2a-protocol.md` na v2.0, brise se kompletno `agent/daemon.py` i `relay/main.py` (relay opcija postaje `clade relay`).

---

## 5. Open questions koje dev MORA verifikovati pre commit-a

1. **MCP klijent transport support u Claude Code:** prihvata li `unix:///` URL direktno, ili treba `127.0.0.1:PORT` fallback? Ako fallback — gde se cuva port mapping i kako se discover-uje (file u `~/.config/clade/runtime/<peer>.port`)?
2. **`sd_notify` Python package:** koristiti `sdnotify` (pure Python) ili `systemd-python` (C bindings)? Prvi je portabilniji, drugi je tighter integracija. Verifikuj da `sdnotify` radi sa `Type=notify` + `WatchdogSec` ispravno.
3. **msgspec vs pydantic:** ako tim vec ima pydantic u dependency tree, ne uvuci dva validation libra. Drzi konzistentno.
4. **Unix socket permissions u systemd user mode:** `XDG_RUNTIME_DIR` (`/run/user/<uid>/`) je preferred, ali proveri da systemd user manager ga setuje pre `ExecStart`.
5. **`PRAGMA wal_checkpoint(PASSIVE)` na shutdown:** verifikuj da ne blokira ako su readers aktivni (ne bi trebao, ali sd_notify watchdog moze ubiti proces ako traje > timeout).

---

## 6. Sta NIJE u planu (svesno)

- **Multi-peer broadcast / fan-out** — interesantno, van scope-a "uprosti".
- **Read-only peer role** — isto.
- **Kompletan rewrite u Rust/Go** — perf nije bolna tacka. Python sa ~200 LOC transport sloja je dovoljan.
- **Web UI za inbox/outbox** — health endpoint + journald + `clade status` je dovoljno.
- **Persistent thread history** — in-memory + TTL pokriva realan use case.
- **RLIMIT_AS/CPU za subprocess** — dodaj tek ako vidis OOM u produkciji.
- **Custom JSON-RPC framing** — vredjanje nereseno bolje od greenfield problema.
- **Garantovana exactly-once delivery** — at-least-once preko outbox-a + idempotency po `msg_id` je dovoljan.

---

## 7. Sta se sece (deprecation lista za v2.0)

| Stvar | Zasto |
|---|---|
| Daemon kao zasebna komponenta | Merge u `clade serve`, eliminise dual-process i warm-stall |
| `clade_send`, `clade_ask` wrapperi | Mi smo jedini korisnici, nema eksternih bundle-ova |
| File lock (`<peer>-daemon.lock`) | Jedan proces = jedan vlasnik, lock je suvisan |
| `[CLARIFY]` marker / `_clarify` flag | Marginalna korist (1/20 poruka), novi koncept za korisnika |
| `thread_history` SQLite tabela | Zamenjuje ThreadCache (in-memory + TTL) |
| `outbox_monitor_loop` u daemon-u | Sender-driven retry pokriva isti use case |
| Push notification (P2#8) | Connection refused je dovoljan signal |
| Relay za on-host | Opcioni samo za `--remote` |
| 5 startup skripti | Zamenjuje 1 systemd unit |
| Duplicate CLAUDE.md | Vec resen kroz `a2a-protocol.md` reference |
| `agent/main.py`, `agent/daemon.py` | Konsoliduje u `clade serve` |

---

## 8. Sta ostaje (core layer v2.0)

- **`clade_message`** kao jedini outbound MCP tool
- **`clade_outbox_status`** kao debug tool (sada cesce useful)
- **Audit log** (SQLite write-through, jedini history source of truth)
- **Outbox** kao soft-failover sa exponential backoff
- **HMAC + nonce + ts skju** — required za `--remote`, opcioni za on-host unix socket
- **Relay** za cross-host scenario iza `--remote` flag-a

---

## 9. Meta-zapazanje

Tri turn-a sa dusanom (i prethodna runda) potvrdili su da je **A2A protokol funkcionalan za substantive technical discussion** — dusan je dao kalibrisane tehnicke odgovore (npr. "`synchronous=NORMAL`, ne FULL", "DEVNULL ne PIPE", "NE uvuci FastAPI"). To je signal da problem nije u protokolu vec u **arhitekturi koja je rasla aditivno**.

v2.0 nije "jos jedna feature runda" — to je arhitekturni reset. 4 komponente → 1 proces. 4 config fajla → 1. 5 tools → 2 vidljiva. 5 skripti → 1 systemd unit. **Ako korisnik posle migracije oseti razliku, oseti je kao "vise ne moram da znam stvari koje sam pre znao".** To je pravi simplification refactor.

Dev tim ima u ovom dokumentu: schemu, file path-ove, code skeletons, decision rationale, migracioni plan po PR-ovima, open questions sa odgovornoscu na timu. Sve sto fali je `git checkout -b v2-arch && git commit`.

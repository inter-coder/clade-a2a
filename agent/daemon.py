"""Clade Agent Daemon — long-running poller + auto-responder.

Tece u pozadini na peer masini. Polluje relay svake POLL_INTERVAL_S sekunde.
Za svaku `ask` poruku — spawn-uje `claude --print` da izracuna odgovor i
posalje ga preko `clade_reply`. Za svaku `send` poruku — samo loguje.

Use:
    CLADE_CONFIG=/path/to/peer.yaml python -m agent.daemon
    CLADE_CONFIG=/path/to/peer.yaml python -m agent.daemon --yolo

Stop: Ctrl+C (SIGINT) ili `kill <pid>` (SIGTERM). Cisto se isključuje.
"""

import argparse
import asyncio
import os
import secrets
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Kao i u agent/main.py — kad se daemon pokrene preko `python -m agent.daemon`
# iz tudjeg dir-a (npr. iz agent bundle-a), `from agent.main import ...` moze
# da pukne jer parent dir nije na sys.path. Dodajemo eksplicitno.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

# Boja za stdout (visuelno cisto)
RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"


POLL_INTERVAL_S = 2.0
CLAUDE_TIMEOUT_S = 90
OUTBOX_CHECK_INTERVAL_S = 30.0
OUTBOX_STALE_WARN_S = 30.0      # poruka starija od ovog → warn log
# v1.8.0: heartbeat ka relay-ev /presence endpoint. Default 15s znači peer pada
# offline (TTL 35s) ako 3 propusti za redom — daje toleranciju na trenutne
# network glitche bez pravljenja false-offline flicker-a.
PRESENCE_HEARTBEAT_S = float(os.environ.get("CLADE_DAEMON_PRESENCE_S", "15"))
# v1.4.8: max paralelnih claude --print spawn-ova po daemon-u.
# Stress test je pokazao da Claude API rate-limit-uje 5 paralelnih, pa svaki
# traje 5x duze i sve timeout-uju. Pri 2 paralelna su realno latencije.
# Override preko CLADE_DAEMON_CONCURRENCY env var.
CLAUDE_CONCURRENCY = int(os.environ.get("CLADE_DAEMON_CONCURRENCY", "2"))
SHUTDOWN_EVENT = asyncio.Event()
_claude_semaphore: asyncio.Semaphore | None = None  # init u poll_loop
_inflight_tasks: set[asyncio.Task] = set()
# v1.5.0: corr_id → (asyncio.Task, original_sender_id) za cancel protocol.
# v1.7.0 (C2): cuvamo i original_sender_id — cancel auth check sprecava da
# bilo koji peer iz allowlist-a otkaze tudji ask.
_inflight_by_corr: dict[str, tuple[asyncio.Task, str]] = {}


# ---- Payload → question text ekstrakcija ----

def _extract_question(payload) -> str:
    """Izvuci ljudski-citljiv question text iz ask payload-a.

    Fallback chain: payload.question → payload.text → ceo payload (bez _meta polja)
    kao JSON. Razlog: clade_message(content="str") pakuje u {"text": str},
    a stara konvencija je {"question": str}. Treba podrzati oba bez breakage-a."""
    if not isinstance(payload, dict):
        return str(payload) if payload is not None else "(prazno pitanje)"
    q = payload.get("question") or payload.get("text")
    if q:
        return str(q)
    # Neither field — stringify ceo payload bez _meta polja kao fallback
    import json as _json  # noqa: PLC0415
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    if not clean:
        return "(prazno pitanje)"
    return _json.dumps(clean, ensure_ascii=False)


# ---- File lock (v1.0.0) ----

def _daemon_lock_path(cfg) -> Path:
    """Lock fajl pored audit DB-a, per-peer naming."""
    audit_db = Path(cfg.audit_db).expanduser()
    return audit_db.parent / f"{cfg.my_id}-daemon.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def acquire_lock(cfg) -> Path:
    """Acquire daemon file lock. Vrati path.

    Ako lock postoji + PID jos zivi → vec tece drugi daemon, exit 1.
    Ako lock postoji ali PID mrtav → stale, prebrise.
    """
    lock = _daemon_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)

    if lock.exists():
        try:
            existing_pid = int(lock.read_text().strip())
        except (ValueError, OSError):
            existing_pid = -1
        if existing_pid > 0 and _pid_alive(existing_pid):
            print(
                f"[clade-daemon] ERROR: drugi daemon vec tece za peer '{cfg.my_id}' (PID {existing_pid}).\n"
                f"  Lock fajl: {lock}\n"
                f"  Stop ga prvo (kill {existing_pid}) ili pokreni za drugi peer.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Stale lock — prebrisi
        print(f"[clade-daemon] info: stale lock ({existing_pid}), prebrisujem", file=sys.stderr)

    lock.write_text(str(os.getpid()))
    return lock


def release_lock(lock: Path) -> None:
    try:
        # Sanity check: jos uvek nasa PID? (race: drugi daemon mogao da preuzme)
        if lock.exists() and lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


# ---- MCP config + minimal-noise settings za headless claude ----

def write_mcp_config(workdir: Path, cfg) -> Path:
    """Generisi .mcp.json u daemon workdir-u tako da `claude --print --mcp-config`
    moze da loaduje clade tools. Tools su eager kad ih MCP server izlozi.

    Aktivira P2 clarify-back path (vidi protokol §5.6) — headless Claude moze
    da pozove clade_message ka peer-u ako mu treba."""
    import json as _json  # noqa: PLC0415
    try:
        from agent import main as _agent_main  # noqa: PLC0415
        agent_main_path = str(Path(_agent_main.__file__).resolve())
    except Exception:
        agent_main_path = str(Path(__file__).parent / "main.py")

    config_env_path = os.environ.get("CLADE_CONFIG", "")
    mcp_path = workdir / ".mcp.json"
    mcp_path.write_text(_json.dumps({
        "mcpServers": {
            "clade": {
                "command": sys.executable,
                "args": [agent_main_path],
                "env": {"CLADE_CONFIG": config_env_path},
            },
        },
    }, indent=2))
    return mcp_path


# ---- Minimal headless profile (v1.2.0 P2#9) ----

MINIMAL_HEADLESS_ENV = {
    # Suppress skills loader telemetry — daemon ne treba /init, /loop, frontend-design itd.
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    # Suppress feedback prompts i ostali nesustinski saobracaj
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
}


def write_minimal_settings(workdir: Path) -> Path:
    """Generisi .claude/settings.json u daemon workdir-u sa stripped profile-om.

    skillOverrides = "off" za skills koji nemaju veze sa daemon-ovim zadatkom.
    skillListingBudgetFraction smanjuje budzet skills lista u promptu.
    spinnerTipsEnabled=false uklanja UI noise (svejedno smo headless, ali iz reda).

    permissions.allow — pre-odobreni Read/Bash za putanje koje daemon-spawn
    Claude rutinski inspektuje kad odgovara peer-u (sopstveni config, audit DB,
    install dir). Bez ovoga, svaki self-introspection trazi user permission
    prompt — sto u headless modu blokira sve. YOLO mode preskace ovo svejedno
    (`--dangerously-skip-permissions`), ali safe mode (default) ih treba."""
    import json as _json  # noqa: PLC0415
    home = os.environ.get("HOME", str(Path.home()))
    settings_dir = workdir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(_json.dumps({
        "skillOverrides": {
            "/init": "off",
            "/loop": "off",
            "/schedule": "off",
            "/review": "off",
            "/security-review": "off",
            "claude-api": "off",
            "frontend-design": "off",
            "update-config": "off",
            "keybindings-help": "off",
            "simplify": "off",
            "fewer-permission-prompts": "off",
        },
        "spinnerTipsEnabled": False,
        "skillListingBudgetFraction": 0.005,
        "permissions": {
            "allow": [
                # Self-introspection (config + install + state)
                f"Read({home}/clade-agent/**)",
                f"Read({home}/.clade/**)",
                "Read(/opt/clade-a2a/**)",
                f"Read({workdir}/**)",
                # Basic shell komande za dijagnostiku — bez argumenta validacije
                # jer daemon-Claude ce ih koristiti sa raznim path-ovima
                "Bash(ls:*)",
                "Bash(cat:*)",
                "Bash(head:*)",
                "Bash(tail:*)",
                "Bash(pwd)",
                "Bash(echo:*)",
                "Bash(grep:*)",
                "Bash(find:*)",
                "Bash(wc:*)",
                "Bash(file:*)",
                "Bash(stat:*)",
            ],
        },
    }, indent=2))
    return settings_path


# v1.7.0 (C6): opcioni file logger sa rotation-om. Ako --log-file dat,
# daemon log() pise i u taj fajl (kroz RotatingFileHandler, default 10MB x 3).
# Stdout ostaje (za docker/systemd integraciju).
_log_file_handler = None  # set u main() ako --log-file dat


def _strip_ansi(s: str) -> str:
    """Skida ANSI escape sekvence za fajl (terminal-friendly stdout ostaje)."""
    import re  # noqa: PLC0415
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


def log(msg: str, color: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    line_term = f"{DIM}[{ts}]{RESET} {color}{msg}{RESET}"
    print(line_term, flush=True)
    if _log_file_handler is not None:
        try:
            _log_file_handler.emit_line(f"[{ts}] {_strip_ansi(msg)}")
        except Exception:
            pass


class _RotatingFileLogger:
    """Mini wrapper oko logging.handlers.RotatingFileHandler — daje emit_line()
    da bi se uklopio u trenutni print-based log() bez velikog refaktora."""
    def __init__(self, path: str, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3):
        import logging  # noqa: PLC0415
        import logging.handlers  # noqa: PLC0415
        self._h = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )
        self._h.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"clade-daemon-{path}")
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._h)
        self._logger.propagate = False

    def emit_line(self, line: str) -> None:
        self._logger.info(line)


def banner(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}═══ {msg} ═══{RESET}", flush=True)


def _collect_env_context(workdir: Path, cfg) -> dict[str, str]:
    """v1.4.4: pokupi realan host kontekst koji ide u prompt — hostname, primary
    LAN IP, danasnji datum, workdir, audit_db, sopstvenu OS verziju.

    Razlog: bez ovog peer Claude ne zna gde se nalazi pa odgovara generike
    ("verovatno X.Y.z"), umesto da pokrene `uname -r` ili procita /etc/hostname.
    Test je pokazao halucinaciju na pitanje "koja verzija kernela?"."""
    import socket as _socket  # noqa: PLC0415
    import datetime as _dt  # noqa: PLC0415

    hostname = ""
    primary_ip = ""
    try:
        hostname = _socket.gethostname()
    except Exception:
        pass
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
    except OSError:
        primary_ip = "127.0.0.1"

    return {
        "hostname": hostname or "(unknown)",
        "primary_ip": primary_ip,
        "today": _dt.date.today().isoformat(),
        "workdir": str(workdir),
        "audit_db": str(Path(cfg.audit_db).expanduser()),
        "config_path": os.environ.get("CLADE_CONFIG", "(unset)"),
    }


def _format_peer_directory(cfg, exclude_id: str) -> str:
    """v1.4.4: kratka lista poznatih peer-ova sa imenom + role-om za sistem
    prompt. Bez ovog Bob (npr) ne zna ko je sve u mrezi pa govori "ne znam
    druge peer-ove" iako su u cfg.peers.

    exclude_id: trenutni sender — ne treba u listi (vec je predstavljen)."""
    lines: list[str] = []
    for peer_id, entry in cfg.peers.items():
        if peer_id == exclude_id:
            continue
        if hasattr(entry, "name"):  # PeerInfo
            name = entry.name or peer_id
            role_short = (entry.role or "").strip().split("\n")[0][:80]
            line = f"- {name} (id: {peer_id})"
            if role_short:
                line += f" — {role_short}"
        else:  # legacy string-secret
            line = f"- {peer_id}"
        lines.append(line)
    return "\n".join(lines) if lines else "(nema drugih peer-ova osim sendera)"


def _extra_add_dirs(cfg, workdir: Path) -> list[str]:
    """v1.4.4: putanje koje peer Claude TREBA da moze da cita van workdir-a.
    Bez `--add-dir` Claude Code drzi hard path sandbox cak i u YOLO modu —
    cak ni `Read(...)` u `permissions.allow` to ne probija.

    Auto-included:
      - parent direktorijum CLADE_CONFIG-a (peer yaml live tu)
      - parent direktorijum audit_db (.clade po default-u)
      - /opt/clade-a2a (install dir; ako postoji)

    v1.10.2: dodatno cfg.extra_add_dirs (user-konfigurabilno u yaml-u).
    Korisno za trajan pristup project dir-ovima, /var/www/, /home/user/code/, ...
    Path-ovi koji ne postoje su skip-ovani (warn u log-u).
    """
    dirs: list[str] = []
    config_p = os.environ.get("CLADE_CONFIG")
    if config_p:
        try:
            dirs.append(str(Path(config_p).expanduser().parent.resolve()))
        except Exception:
            pass
    try:
        dirs.append(str(Path(cfg.audit_db).expanduser().parent.resolve()))
    except Exception:
        pass
    for fixed in ["/opt/clade-a2a"]:
        if Path(fixed).exists():
            dirs.append(fixed)
    # v1.10.2: user-konfigurabilne dodatne putanje iz yaml-a
    for user_d in getattr(cfg, "extra_add_dirs", []) or []:
        try:
            p = Path(user_d).expanduser()
            if p.exists():
                dirs.append(str(p.resolve()))
            else:
                log(f"{YELLOW}extra_add_dirs: '{user_d}' ne postoji, skipping{RESET}", "")
        except Exception as e:
            log(f"{YELLOW}extra_add_dirs '{user_d}' invalid: {e}{RESET}", "")
    # dedupe + skloni one koji su unutar workdir-a (ne treba im --add-dir)
    seen: set[str] = set()
    out: list[str] = []
    wd_resolved = str(workdir.resolve())
    for d in dirs:
        if d in seen or d == wd_resolved:
            continue
        seen.add(d)
        out.append(d)
    return out


async def call_claude(question: str, dangerous: bool, workdir: Path,
                       from_peer: str, my_id: str,
                       thread_context: str = "",
                       *,
                       name: str | None = None,
                       role: str | None = None,
                       from_peer_name: str | None = None,
                       from_peer_role: str | None = None,
                       cfg=None) -> str:
    """Spawn `claude --print` da izracuna odgovor na peer-ovo pitanje.

    Workdir kontrolise koji CLAUDE.md i .mcp.json se ucitavaju — ali za
    daemon mi koristimo dedicated workdir BEZ CLAUDE.md (da se izbegne
    rekurzivno pollovanje), prosledjujemo sve sto treba direktno u prompt.

    thread_context: opcioni text iz format_thread_for_prompt() — istorija
    prethodnih poruka u istom threadu. Daje Claude-u "memoriju" izmedju
    asks u istom logickom razgovoru.

    name + role (v1.3.0): identifikacija + system prompt opisuje TI si KO.
    Ako je dat role — prepend kao primary kontekst (pred sve ostalo). Ime
    zameni generic my_id u self-reference.

    cfg (v1.4.4): ceo Config — koristimo za peer directory i --add-dir
    putanje. Default None radi backward-compat sa testovima koji direktno
    pozivaju call_claude bez cfg-a."""
    display_self = name or my_id
    display_peer = from_peer_name or from_peer

    sections: list[str] = []

    # 1) ROLE blok — najveca tezina, prvo u kontekstu
    if role and role.strip():
        sections.append(
            f"=== KO SI TI ===\n"
            f"Ime: {display_self}"
            + (f" (tehnicki ID: {my_id})" if name and name != my_id else "")
            + f"\n\n{role.strip()}\n=== KRAJ ===\n"
        )

    # 2) v1.4.4: realan host kontekst — peer Claude zna GDE je
    # v1.7.0 (C5): trenutno opterecenje u kontekstu — pod stresom Claude
    # treba da skrati odgovore jos vise.
    if cfg is not None:
        env_ctx = _collect_env_context(workdir, cfg)
        load_line = ""
        if _claude_semaphore is not None:
            in_flight = len(_inflight_tasks)
            load_pct = (in_flight / CLAUDE_CONCURRENCY) * 100 if CLAUDE_CONCURRENCY else 0
            load_hint = ""
            if load_pct >= 100:
                load_hint = " — TI SI JEDAN OD ZADNJIH SLOT-ova, drugi cekaju; budi vrlo sazet"
            elif load_pct >= 50:
                load_hint = " — opterecenje srednje, ne troši kontekst"
            load_line = f"Opterecenje: {in_flight}/{CLAUDE_CONCURRENCY} slot-ova zauzeto{load_hint}\n"
        sections.append(
            "=== TVOJ KONTEKST (host) ===\n"
            f"Hostname: {env_ctx['hostname']}\n"
            f"Primary IP (LAN): {env_ctx['primary_ip']}\n"
            f"Datum: {env_ctx['today']}\n"
            f"Workdir: {env_ctx['workdir']}\n"
            f"Tvoj config: {env_ctx['config_path']}\n"
            f"Audit DB: {env_ctx['audit_db']}\n"
            f"{load_line}"
            "=== KRAJ ===\n"
        )

    # 3) v1.4.4: STA TI MOZES (eksplicitno, da peer ne hallucinishe)
    sections.append(
        "=== TVOJE MOGUCNOSTI ===\n"
        f"- Bash u workdir-u {workdir} (uname, hostname, ls, cat, ps, "
        f"curl prema lokalnoj mrezi — pokreni komandu kad treba TACAN podatak; "
        f"ne pogadjaj ako mozes da proveris).\n"
        "- Read/Write fajlova u workdir-u (van workdir-a samo Read za putanje "
        "koje su ti dodate preko --add-dir; ako Read padne, fajl je van sandbox-a).\n"
        "- clade_* MCP tool-ovi da pitas drugog peer-a — koristi SAMO ako ti "
        "tema zaista zahteva njegov ekspertski domen ili njegov host pristup.\n"
        "STA NE MOZES: pisati van workdir-a, sudo, install paketa, web browse "
        "(osim ako rola eksplicitno trazi). Ne pretpostavljaj capabilities koje "
        "nisu navedene — radije reci 'nemam X' nego da izmislis.\n"
        "=== KRAJ ===\n"
    )

    # 4) v1.4.4: peer directory (ko je jos u mrezi)
    if cfg is not None:
        sections.append(
            "=== OSTALI PEER-OVI U MREZI ===\n"
            f"{_format_peer_directory(cfg, exclude_id=from_peer)}\n"
            "Mozes ih pitati preko clade_message(to=<id>, content=..., "
            "expect_reply=True).\n"
            "=== KRAJ ===\n"
        )

    # 5) Ko te pita + pravila odgovora
    peer_intro = f"Drugi peer agent '{display_peer}'"
    if from_peer_role and from_peer_role.strip():
        peer_intro += f" ({from_peer_role.strip()[:120]})"
    sections.append(
        f"{peer_intro} ti je upravo postavio pitanje. Pravila odgovora:\n"
        "1. Pricaj sa NJIM (peer-to-peer), ne sa korisnikom. "
        "Ne kazi 'moraces ti to da uradis' — peer ne moze da menja sesiju.\n"
        "2. Ako mozes Bash komandom (npr `uname -r`, `hostname -I`) ili Read-om "
        "da dodjes do TACNOG odgovora, prvo to pokreni — pa odgovori. "
        "Halucinacija ('verovatno X') je gora od priznanja 'ne mogu'.\n"
        "3. SAZETO. Default: max 3 recenice / ~60 reci. Trivijalan info "
        "(status, broj, ime) = jedna recenica BEZ ASCII tabela, BEZ markdown "
        "header-a, BEZ pre-amble-a. Markdown tabla samo kad imas >5 redova "
        "data OR peer eksplicitno trazi tabelu. Zid texta NIJE odgovor.\n"
        "4. AKO pitanje je dvosmisleno ili fali kontekst, zapocni odgovor sa "
        "marker-om `[CLARIFY]` i postavi clarify pitanje. Daemon ce to oznaciti "
        "i interactive Claude na drugoj strani ce ga prikazati korisniku.\n"
        "5. Ako stvarno ne mozes (sandbox/permisije/nedostatak alata), kazi to "
        "direktno + reci sta bi peer trebao da uradi sam.\n"
        "6. Drzi se SVOJE uloge. Ako pitanje je VAN tvog domena, ili predloži "
        "konkretnog peer-a iz directorija ('Bolje pitaj Charlie — on je DBA'), "
        "ili reci 'van moje uloge'. Ne pisi pesme/poeziju/random ako si dev.\n"
        "7. NE NUDI follow-up akcije ('Da pingnem?', 'Da pokrenem X?', "
        "'Da pitam za detalje?') — peer ce sam zatraziti ako mu treba. "
        "Cekaj sledece pitanje umesto da pretpostavljas sta peer hoce.\n"
        "8. NE SPEKULISI o stvarima koje peer nije pitao (zaostali fajlovi na "
        "disku, hipotetski uzroci, prethodne sesije). Direktan odgovor na "
        "POSTAVLJENO pitanje.\n"
        "9. AKO MCP clade tool nije dostupan u tvojoj sesiji — JAVI 'clade "
        "MCP tool nije dostupan, ne mogu da ti odgovorim direktno preko A2A'. "
        "NE pokusavaj raw HTTP curl ka relay-u kao fallback — relay ne "
        "izlaze sve sto MCP tool ima (presence parsing, HMAC sign, "
        "audit), i pokusaj nepostojecih endpoint-ova (POST /peers itd.) "
        "samo trosi tokene i pravi greske."
    )

    # 6) Thread continuity (ako ima istorije)
    if thread_context:
        sections.append(thread_context)

    # Fallback ako nemamo role: zadrzi v1.2.0-style generic self-id
    if not role:
        sections.insert(0,
            f"Ti si '{display_self}' agent u Clade A2A sistemu (protokol v1.3.0)."
        )

    system_prompt = "\n\n".join(sections)

    # Defense: nikad ne smemo prosledjivati None u args (TypeError u fork_exec).
    safe_question = question if question else "(prazno pitanje od peer-a)"

    mcp_config = workdir / ".mcp.json"
    args = ["claude", "--print", "--append-system-prompt", system_prompt]
    if mcp_config.exists():
        args.extend(["--mcp-config", str(mcp_config)])
    # v1.4.4: --add-dir za putanje van workdir-a (config, audit db, install dir).
    # `permissions.allow` u settings.json NE prosiruje fs access — to ide samo
    # preko --add-dir. Pre v1.4.4 peer Claude nije mogao ni svoj config da cita.
    if cfg is not None:
        for d in _extra_add_dirs(cfg, workdir):
            args.extend(["--add-dir", d])
    if dangerous:
        args.append("--dangerously-skip-permissions")
    args.extend(["--", safe_question])

    # Minimal headless env (v1.2.0): suppress skills/feedback noise
    env = {**os.environ, **MINIMAL_HEADLESS_ENV}

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        return f"[daemon: claude --print timeout posle {CLAUDE_TIMEOUT_S}s]"
    except asyncio.CancelledError:
        # v1.5.0: sender posaljnao cancel — ubij claude --print da ne trose
        # API/CPU. Pre toga (v1.4.x) ovo se ne hvatalo, pa cancel je samo
        # uklanjao task ali claude proces je trajao do kraja sam.
        log(f"{YELLOW}  ⊘ cancel primljen, terminate claude --print PID {proc.pid}{RESET}", "")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
        raise

    if proc.returncode != 0:
        err_text = err.decode("utf-8", errors="replace")[:200].strip()
        return f"[daemon: claude exited {proc.returncode}: {err_text}]"

    answer = out.decode("utf-8", errors="replace").strip()
    if not answer:
        return "[daemon: claude returned empty response]"
    return answer


async def send_reply(correlation_id: str, response: dict[str, Any],
                      to: str, cfg, sign_fn,
                      record_thread_fn=None) -> bool:
    """Konstrui + potpisi reply envelope, posalji relay-u.
    Ako record_thread_fn dat i response ima _thread_id, record-ujemo outbound."""
    secret = cfg.peer_secret(to) or ""
    msg_id = str(uuid.uuid4())
    nonce = secrets.token_hex(16)
    ts_ms = int(time.time() * 1000)
    sig = sign_fn(secret, msg_id, cfg.my_id, to, "reply", response, nonce, ts_ms, correlation_id)
    env = {
        "msg_id": msg_id,
        "from_agent": cfg.my_id,
        "to_agent": to,
        "kind": "reply",
        "correlation_id": correlation_id,
        "payload": response,
        "nonce": nonce,
        "timestamp_ms": ts_ms,
        "hmac": sig,
    }
    if record_thread_fn:
        record_thread_fn(msg_id, "out", to, "reply", response)
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(
                f"{cfg.relay_url}/reply",
                json=env,
                headers={"Authorization": f"Bearer {cfg.bearer_token}"},
                timeout=10,
            )
            return r.status_code == 200
        except Exception as e:
            log(f"reply send failed: {e}", RED)
            return False


async def process_message(env: dict, dangerous: bool, workdir: Path, cfg, sign_fn,
                            audit_log_fn, record_thread_fn=None, load_thread_fn=None,
                            format_thread_fn=None, task_handler_fn=None) -> None:
    from_agent = env["from_agent"]
    kind = env["kind"]
    payload = env["payload"]
    msg_id_short = env["msg_id"][:8]
    thread_id = payload.get("_thread_id") if isinstance(payload, dict) else None

    # Record incoming u thread_history (v1.1.0)
    if record_thread_fn:
        record_thread_fn(env["msg_id"], "in", from_agent, kind, payload)
    # v1.9.0: ako poruka sadrzi _task ili _task_update, persistuj u tasks tabelu
    # PRE nego sto je daemon proslijedi Claude-u kao ask. Time daemon-spawn
    # Claude vec ima task u svom DB-u i moze ga referencirati.
    if task_handler_fn:
        try:
            task_handler_fn(env)
        except Exception as e:
            log(f"{RED}task_handler greska: {e}{RESET}", "")

    # v1.5.0: cancel protokol — sender posaljnao da otkaze tekuci ask
    # v1.7.0 (C2): auth check — samo ORIGINAL sender moze cancel sopstveni ask
    if kind == "cancel":
        target_corr = payload.get("target_correlation_id") if isinstance(payload, dict) else None
        log(f"{YELLOW}← cancel{RESET} {DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET} → target {target_corr}", "")
        if target_corr and target_corr in _inflight_by_corr:
            task, orig_sender = _inflight_by_corr[target_corr]
            if from_agent != orig_sender:
                log(f"{RED}  ✗ cancel AUTH FAIL — sender '{from_agent}' nije original '{orig_sender}'{RESET}", "")
                audit_log_fn("rejected", env["msg_id"], from_agent, "cancel", "auth_fail",
                             f"sender ne odgovara originalu '{orig_sender}'")
                return
            task.cancel()
            audit_log_fn("in", env["msg_id"], from_agent, "cancel", "cancelled", target_corr)
        else:
            audit_log_fn("in", env["msg_id"], from_agent, "cancel", "no_match",
                         f"target {target_corr} nije in-flight")
        return

    if kind == "send":
        log(f"{CYAN}← send  {RESET}{DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET}: {payload}", "")
        # v1.4.8: cuvamo payload JSON u 'note' polje audit-a da chat.sh moze
        # da prikaze pending send-ove user-u (thread_history zahteva _thread_id
        # koji fire-and-forget send-ovi nemaju).
        import json as _j  # noqa: PLC0415
        try:
            note = _j.dumps(payload, ensure_ascii=False)[:1000]
        except Exception:
            note = str(payload)[:1000]
        audit_log_fn("in", env["msg_id"], from_agent, "send", "logged", note)
        return

    if kind == "ask":
        corr = env.get("correlation_id")
        question = _extract_question(payload)
        log(f"{YELLOW}← ask   {RESET}{DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET}: {question}", "")

        # Thread context (v1.1.0): ucitaj last N poruka u istom threadu
        thread_context = ""
        if thread_id and load_thread_fn and format_thread_fn:
            history = load_thread_fn(thread_id, 10)
            # Iskljucujemo trenutnu poruku (vec smo je record-ovali iznad)
            history = [h for h in history if h.get("msg_id") != env["msg_id"]]
            thread_context = format_thread_fn(history, cfg.my_id)
            if history:
                log(f"{DIM}  ↺ thread {thread_id[:8]}: {len(history)} prethodnih poruka u kontekstu{RESET}", "")

        log(f"{DIM}  ⏳ computing reply via claude --print{' --yolo' if dangerous else ''}...{RESET}", "")

        t0 = time.time()
        # v1.3.0: name + role iz config + per-peer info (ako PeerInfo schema)
        peer_info = cfg.peer_info(from_agent)
        answer = await call_claude(
            question, dangerous, workdir, from_agent, cfg.my_id, thread_context,
            name=cfg.name, role=cfg.role,
            from_peer_name=peer_info.name if peer_info else None,
            from_peer_role=peer_info.role if peer_info else None,
            cfg=cfg,
        )
        elapsed = time.time() - t0

        # Truncate prikaza za log (full answer ide u reply)
        answer_short = answer[:80] + ("..." if len(answer) > 80 else "")
        log(f"{GREEN}→ reply {RESET}{DIM}to  {RESET}{BOLD}{from_agent}{RESET} {DIM}({elapsed:.1f}s){RESET}: {answer_short}", "")

        # Clarify-back detect (v1.2.0): ako Claude pocne sa [CLARIFY], oznaci u payload-u
        is_clarify = answer.lstrip().upper().startswith("[CLARIFY]")
        if is_clarify:
            # Skini marker iz teksta — flag je sad eksplicitan
            answer_clean = answer.lstrip()[len("[CLARIFY]"):].lstrip()
            reply_payload: dict[str, Any] = {"answer": answer_clean, "_clarify": True}
            log(f"{YELLOW}  ⓘ clarify-back detected — interactive Claude ce prikazati korisniku{RESET}", "")
        else:
            reply_payload = {"answer": answer}
        # Propagate _thread_id u reply payload tako da ga peer takodje moze record-ovati
        if thread_id:
            reply_payload["_thread_id"] = thread_id

        ok = await send_reply(corr, reply_payload, from_agent, cfg, sign_fn,
                              record_thread_fn=record_thread_fn)
        if ok:
            audit_log_fn("out", "-", from_agent, "reply", f"daemon_auto_{elapsed:.1f}s")
        else:
            log(f"  ✗ reply send FAILED — peer ce dobiti timeout", RED)

        return

    if kind == "reply":
        # Replies should not normally appear in inbox — they go through pending_asks future.
        # If they do (e.g. user used clade_ask manually and it timed out), just log.
        log(f"{DIM}← reply from {from_agent} ({msg_id_short}) — stigao posle timeout-a, ignorisem{RESET}", "")
        return

    log(f"{RED}? unknown kind '{kind}' from {from_agent} — ignorisem{RESET}", "")


async def outbox_monitor_loop(cfg, audit_conn, outbox_mod) -> None:
    """Proactive outbox watcher (v1.2.0).

    Svakih OUTBOX_CHECK_INTERVAL_S sekundi:
      1. Procita pending outbox redove
      2. Za poruke starije od OUTBOX_STALE_WARN_S → log warning (vidljivo u
         daemon terminalu, korisnik moze da reaguje)
      3. Pokusa flush (HTTP POST na relay sa standardnim retry logic-om
         iz outbox.mark_failed)

    Bez ovog, outbox je flush-ovan SAMO lazy-no kad neki tool poziv u interactive
    Claude-u prodje. Ako interactive Claude ne tece, poruke su nevidljive."""
    headers = {"Authorization": f"Bearer {cfg.bearer_token}"}
    while not SHUTDOWN_EVENT.is_set():
        try:
            await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=OUTBOX_CHECK_INTERVAL_S)
            break  # shutdown
        except asyncio.TimeoutError:
            pass  # period prosao, krenimo proveru

        rows = outbox_mod.pending_rows(audit_conn, max_items=20)
        if not rows:
            continue

        now_ms = int(time.time() * 1000)
        stale = [r for r in rows if now_ms - r["created_ts_ms"] >= OUTBOX_STALE_WARN_S * 1000]
        if stale:
            log(f"{YELLOW}⚠ outbox: {len(stale)} poruka cuci > {int(OUTBOX_STALE_WARN_S)}s "
                f"(ukupno pending: {len(rows)}). Pokusavam flush...{RESET}", "")

        async with httpx.AsyncClient(timeout=10) as client:
            for row in rows:
                try:
                    payload = (row["envelope"] if row["endpoint"] != "/ask"
                               else {"env": row["envelope"]})
                    r = await client.post(
                        f"{cfg.relay_url}{row['endpoint']}",
                        json=payload, headers=headers,
                    )
                    if r.status_code == 200:
                        outbox_mod.mark_delivered(audit_conn, row["id"])
                        log(f"{GREEN}  ✓ outbox row {row['id']} delivered "
                            f"(after {row['attempts']} attempts){RESET}", "")
                    else:
                        still = outbox_mod.mark_failed(
                            audit_conn, row["id"], f"HTTP {r.status_code}: {r.text[:80]}")
                        if not still:
                            log(f"{RED}  ✗ outbox row {row['id']} DEAD-LETTER "
                                f"(max attempts){RESET}", "")
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    outbox_mod.mark_failed(audit_conn, row["id"], str(e)[:80])


async def presence_loop(cfg) -> None:
    """v1.8.0: heartbeat ka relay /presence svakih PRESENCE_HEARTBEAT_S sekundi.
    Bez ovog, GET /presence ne vidi nas peer kao online iako daemon tece.

    Prvi heartbeat ide odmah pri startu — peer treba da se vidi online u
    setup-server UI-u cim daemon krene, ne posle 15s."""
    headers = {"Authorization": f"Bearer {cfg.bearer_token}"}
    url = f"{cfg.relay_url}/presence"
    consecutive_errors = 0
    async with httpx.AsyncClient(timeout=5) as client:
        while not SHUTDOWN_EVENT.is_set():
            try:
                r = await client.post(url, headers=headers)
                if r.status_code == 200:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors == 1 or consecutive_errors % 30 == 0:
                        log(f"{YELLOW}presence heartbeat HTTP {r.status_code}: {r.text[:80]}{RESET}", "")
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                consecutive_errors += 1
                if consecutive_errors == 1 or consecutive_errors % 30 == 0:
                    log(f"{YELLOW}presence heartbeat: relay nedostupan ({type(e).__name__}){RESET}", "")
            try:
                await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=PRESENCE_HEARTBEAT_S)
                break
            except asyncio.TimeoutError:
                pass


async def config_watcher_loop(cfg, config_path: Path) -> None:
    """v1.7.0 (C8): hot-reload peer allowlist iz yaml-a kad fajl menja mtime.
    Bez restart-a daemona, user moze: edit-uje peer.yaml (npr doda novog peer-a
    iz svezeg setup-a), daemon ce za do 5s primijetiti + osvjeziti cfg.peers.
    Dict swap je atomic na Python nivou pa nije race-iv sa poll_loop koji
    cita cfg.peer_secret() jedan-po-jedan."""
    import yaml as _yaml  # noqa: PLC0415
    try:
        last_mtime = config_path.stat().st_mtime
    except OSError:
        last_mtime = 0
    while not SHUTDOWN_EVENT.is_set():
        try:
            await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=5)
            break
        except asyncio.TimeoutError:
            pass
        try:
            mtime = config_path.stat().st_mtime
            if mtime <= last_mtime:
                continue
            data = _yaml.safe_load(config_path.read_text()) or {}
            new_peers_raw = data.get("peers", {})
            # Iste validacije kao u agent.main.PeerInfo
            from agent.main import PeerInfo  # noqa: PLC0415
            new_peers: dict = {}
            for pid, entry in new_peers_raw.items():
                if isinstance(entry, dict):
                    new_peers[pid] = PeerInfo(**entry)
                else:
                    new_peers[pid] = entry
            old_count = len(cfg.peers)
            cfg.peers = new_peers
            last_mtime = mtime
            added = set(new_peers) - set(cfg.peers).union(set(new_peers))  # placeholder
            log(f"{CYAN}↻ config reload: peers {old_count} → {len(new_peers)} "
                f"({list(new_peers.keys())}){RESET}", "")
        except Exception as e:
            log(f"{RED}config watcher error: {type(e).__name__}: {e}{RESET}", "")


async def _process_with_limit(env: dict, dangerous: bool, workdir: Path, cfg,
                                sign_fn, audit_log_fn, **kwargs) -> None:
    """v1.4.8: Wrapper koji uzima semafor pre nego spawn-uje claude --print.
    Bez ovog 5+ paralelnih ask-ova izaziva Claude API rate-limit pa svaki
    traje 5x duze i sve timeout-uju (potvrdjeno u v1.4.7 stress testu).

    v1.5.0: cancel-poruke (kind=cancel) zaobilaze semafor — kratke su, ne
    spawn-uju claude --print, i moraju biti odmah obradjene."""
    # v1.9.0: send sa _task / _task_update — kratke, bez claude --print, zaobilaze semafor.
    payload = env.get("payload") or {}
    is_task_msg = isinstance(payload, dict) and (payload.get("_task") or payload.get("_task_update"))
    if env.get("kind") == "cancel" or is_task_msg or _claude_semaphore is None:
        await process_message(env, dangerous, workdir, cfg, sign_fn, audit_log_fn, **kwargs)
        return
    async with _claude_semaphore:
        await process_message(env, dangerous, workdir, cfg, sign_fn, audit_log_fn, **kwargs)


async def poll_loop(dangerous: bool, workdir: Path, cfg, sign_fn, verify_fn, audit_log_fn,
                     record_thread_fn=None, load_thread_fn=None, format_thread_fn=None,
                     task_handler_fn=None) -> None:
    global _claude_semaphore
    _claude_semaphore = asyncio.Semaphore(CLAUDE_CONCURRENCY)
    banner(f"Clade Daemon — my_id={cfg.my_id}")
    log(f"relay:       {cfg.relay_url}")
    log(f"peers:       {list(cfg.peers.keys())}")
    log(f"mode:        {'YOLO (--dangerously-skip-permissions)' if dangerous else 'safe (sa permissions check)'}")
    log(f"poll:        every {POLL_INTERVAL_S}s")
    log(f"audit:       {cfg.audit_db}")
    log(f"workdir:     {workdir}")
    log(f"concurrency: max {CLAUDE_CONCURRENCY} paralelnih claude --print (CLADE_DAEMON_CONCURRENCY)")
    log("")
    log(f"{BOLD}listening for messages... (Ctrl+C to stop){RESET}", "")
    log("")

    consecutive_errors = 0
    async with httpx.AsyncClient() as client:
        while not SHUTDOWN_EVENT.is_set():
            try:
                r = await client.get(
                    f"{cfg.relay_url}/inbox/{cfg.my_id}",
                    headers={"Authorization": f"Bearer {cfg.bearer_token}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    consecutive_errors = 0
                    data = r.json()
                    # v1.4.8: non-blocking dispatch — pre toga `asyncio.gather`
                    # blokirao poll loop dok ceo batch ne završi. Pri 5+
                    # paralelnih ask-ova, novi su čekali 90s+ da uđu u kontekst.
                    # Sad: create_task po validnom ask-u, semafor kontroliše
                    # stvarni claude --print konkurenciju (default 2).
                    for env in data.get("messages", []):
                        peer = env.get("from_agent")
                        if peer not in cfg.peers:
                            log(f"{RED}? msg from unknown peer '{peer}' — ignorisem{RESET}", "")
                            audit_log_fn("rejected", env.get("msg_id"), peer, env.get("kind"), "unknown_peer")
                            continue
                        if not verify_fn(cfg.peer_secret(peer) or "", env):
                            log(f"{RED}? msg from {peer} HMAC failed — ignorisem (mozda tampered/MITM){RESET}", "")
                            audit_log_fn("rejected", env.get("msg_id"), peer, env.get("kind"), "bad_hmac")
                            continue
                        task = asyncio.create_task(_process_with_limit(
                            env, dangerous, workdir, cfg, sign_fn, audit_log_fn,
                            record_thread_fn=record_thread_fn,
                            load_thread_fn=load_thread_fn,
                            format_thread_fn=format_thread_fn,
                            task_handler_fn=task_handler_fn,
                        ))
                        _inflight_tasks.add(task)
                        task.add_done_callback(_inflight_tasks.discard)
                        # v1.5.0: registruj samo ask task-ove (po corr_id) da
                        # bi cancel mogao da ih nadje. send/cancel ne treba.
                        if env.get("kind") == "ask":
                            corr = env.get("correlation_id")
                            if corr:
                                # v1.7.0: cuvamo (task, original_sender) tuple
                                _inflight_by_corr[corr] = (task, env.get("from_agent"))
                                task.add_done_callback(
                                    lambda _t, c=corr: _inflight_by_corr.pop(c, None))
                else:
                    consecutive_errors += 1
                    if consecutive_errors <= 3 or consecutive_errors % 30 == 0:
                        log(f"{RED}poll HTTP {r.status_code}: {r.text[:100]}{RESET}", "")
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                consecutive_errors += 1
                if consecutive_errors == 1 or consecutive_errors % 30 == 0:
                    log(f"{RED}relay unreachable ({type(e).__name__}). Retry-ujem...{RESET}", "")

            try:
                await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clade-daemon",
        description="Clade Agent Daemon — long-running auto-responder.",
    )
    parser.add_argument("--yolo", "--dangerous", action="store_true", dest="dangerous",
                        help="Pokrene claude --print sa --dangerously-skip-permissions "
                             "(potrebno ako tvoj odgovor zahteva tool koriscenje bez approval-a)")
    parser.add_argument("--workdir", default=None,
                        help="Workdir za claude subprocess (default: privremeni dir bez CLAUDE.md)")
    parser.add_argument("--log-file", default=None,
                        help="Pored stdout-a, pisi log u ovaj fajl sa rotacijom "
                             "(default: 10MB × 3 backup-a). Bez ovog, samo stdout.")
    args = parser.parse_args()

    # v1.7.0: file log handler ako --log-file dat
    if args.log_file:
        global _log_file_handler
        try:
            _log_file_handler = _RotatingFileLogger(args.log_file)
        except Exception as e:
            print(f"[clade-daemon] WARN: --log-file '{args.log_file}' ne moze: {e}", file=sys.stderr)

    # Late import — agent.main loaduje config iz CLADE_CONFIG env-a, mora postojati
    try:
        from agent.main import (
            cfg, sign, verify, audit_log,
            record_thread_message, load_thread_history, format_thread_for_prompt,
            handle_inbound_task_message,
            _audit_conn,
        )
        from agent import outbox as outbox_mod
    except SystemExit:
        print("FATAL: CLADE_CONFIG nije setovan ili config fajl ne postoji.", file=sys.stderr)
        print("Postavi: export CLADE_CONFIG=/path/to/peer.yaml", file=sys.stderr)
        sys.exit(1)

    # File lock pre svega — sprecava dva daemon-a za isti peer (v1.0.0)
    lock_path = acquire_lock(cfg)

    # Daemon workdir za claude --print
    # v1.5.0: workdir je u dirname(audit_db)/wd-<peer>-<rand> umesto /tmp.
    # Pre toga: atexit cleanup nije radio za SIGKILL (proces je odmah ubijen,
    # atexit ne pokrece) — workdir-ovi su cureli u /tmp. Sad smo u
    # known mestu (~/.clade/) gde clade-cleanup moze pouzdano da nadje orphane.
    # Startup cleanup: brisemo stare wd-<my_id>-* (file lock vec garantuje
    # da nema drugog tekuceg daemon-a za isti peer, pa svi stari su orphan).
    workdir_is_managed = not args.workdir
    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        import shutil  # noqa: PLC0415
        audit_parent = Path(cfg.audit_db).expanduser().parent
        audit_parent.mkdir(parents=True, exist_ok=True)
        for old in audit_parent.glob(f"wd-{cfg.my_id}-*"):
            if old.is_dir():
                try:
                    shutil.rmtree(old)
                except OSError:
                    pass
        workdir = audit_parent / f"wd-{cfg.my_id}-{secrets.token_urlsafe(6)}"
        workdir.mkdir(parents=True)
    if workdir_is_managed:
        import atexit  # noqa: PLC0415
        import shutil  # noqa: PLC0415
        atexit.register(lambda: shutil.rmtree(workdir, ignore_errors=True))

    # Generisi .mcp.json u workdir-u (v1.0.0 P0#1: claude --print dobija clade tools eager)
    write_mcp_config(workdir, cfg)
    # Minimal headless settings (v1.2.0 P2#9: smanji skills/feedback noise)
    write_minimal_settings(workdir)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(signum, _frame):
        sig_name = signal.Signals(signum).name
        log(f"{YELLOW}signal {sig_name} primljen, gasim cisto...{RESET}", "")
        loop.call_soon_threadsafe(SHUTDOWN_EVENT.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def _run_all() -> None:
        poll = asyncio.create_task(poll_loop(
            args.dangerous, workdir, cfg, sign, verify, audit_log,
            record_thread_fn=record_thread_message,
            load_thread_fn=load_thread_history,
            format_thread_fn=format_thread_for_prompt,
            task_handler_fn=handle_inbound_task_message,
        ))
        monitor = asyncio.create_task(outbox_monitor_loop(cfg, _audit_conn, outbox_mod))
        # v1.7.0 (C8): config watcher za hot-reload peer allowlist
        config_path = Path(os.environ.get("CLADE_CONFIG", "./config.yaml")).expanduser()
        watcher = asyncio.create_task(config_watcher_loop(cfg, config_path))
        # v1.8.0: presence heartbeat — relay zna ko je online u svakom momentu
        presence = asyncio.create_task(presence_loop(cfg))
        try:
            await poll  # poll_loop drzi semantiku zivota; ostali rade u pozadini
        finally:
            # v1.4.8: ceka in-flight claude --print task-ove max 10s pre
            # shutdown-a. Bez ovog, SIGTERM mid-task-a ostavi zombie spawn-ove
            # koje OS ce sam ubiti kad parent umre, ali audit log ne dobija
            # finalni "reply sent" zapis.
            if _inflight_tasks:
                log(f"cekam {len(_inflight_tasks)} in-flight ask-ova (max 10s)...", "")
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*_inflight_tasks, return_exceptions=True),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    log(f"{YELLOW}timeout — {len(_inflight_tasks)} ask-ova nedovrseno{RESET}", "")
            monitor.cancel()
            watcher.cancel()
            presence.cancel()
            for t in (monitor, watcher, presence):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    try:
        loop.run_until_complete(_run_all())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        release_lock(lock_path)
        log(f"{GREEN}daemon stopped cleanly.{RESET}", "")


if __name__ == "__main__":
    main()

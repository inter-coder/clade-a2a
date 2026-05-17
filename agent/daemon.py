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
SHUTDOWN_EVENT = asyncio.Event()


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
    spinnerTipsEnabled=false uklanja UI noise (svejedno smo headless, ali iz reda)."""
    import json as _json  # noqa: PLC0415
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
    }, indent=2))
    return settings_path


def log(msg: str, color: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {color}{msg}{RESET}", flush=True)


def banner(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}═══ {msg} ═══{RESET}", flush=True)


async def call_claude(question: str, dangerous: bool, workdir: Path,
                       from_peer: str, my_id: str,
                       thread_context: str = "") -> str:
    """Spawn `claude --print` da izracuna odgovor na peer-ovo pitanje.

    Workdir kontrolise koji CLAUDE.md i .mcp.json se ucitavaju — ali za
    daemon mi koristimo dedicated workdir BEZ CLAUDE.md (da se izbegne
    rekurzivno pollovanje), prosledjujemo sve sto treba direktno u prompt.

    thread_context: opcioni text iz format_thread_for_prompt() — istorija
    prethodnih poruka u istom threadu. Daje Claude-u "memoriju" izmedju
    asks u istom logickom razgovoru."""
    base = (
        f"Ti si '{my_id}' agent u Clade A2A sistemu (protokol v1.2.0). "
        f"Drugi peer agent '{from_peer}' ti je upravo postavio pitanje. "
        f"Odgovori sazeto, tacno, u jednoj-dve recenice. "
        f"Ako ne znas odgovor, kazi to direktno — ne izmisljaj. "
        f"Imas pristup clade_* MCP tool-ovima ako ti TREBA da pitas drugog peer-a, "
        f"ali izbegavaj — preferiraj direktan odgovor.\n\n"
        f"AKO pitanje je dvosmisleno ili fali kontekst koji ti treba da odgovoris, "
        f"zapocni svoj odgovor sa marker-om `[CLARIFY]` i postavi clarify pitanje "
        f"(primer: `[CLARIFY] Koja tabela tacno? U staging ili prod DB?`). "
        f"Daemon ce taj odgovor oznaciti kao clarify i interactive Claude na drugoj "
        f"strani ce ga prikazati korisniku umesto da ga obrade kao finalni odgovor."
    )
    system_prompt = f"{base}\n\n{thread_context}" if thread_context else base

    # Defense: nikad ne smemo prosledjivati None u args (TypeError u fork_exec).
    safe_question = question if question else "(prazno pitanje od peer-a)"

    mcp_config = workdir / ".mcp.json"
    args = ["claude", "--print", "--append-system-prompt", system_prompt]
    if mcp_config.exists():
        args.extend(["--mcp-config", str(mcp_config)])
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
    secret = cfg.peers[to]
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
                            format_thread_fn=None) -> None:
    from_agent = env["from_agent"]
    kind = env["kind"]
    payload = env["payload"]
    msg_id_short = env["msg_id"][:8]
    thread_id = payload.get("_thread_id") if isinstance(payload, dict) else None

    # Record incoming u thread_history (v1.1.0)
    if record_thread_fn:
        record_thread_fn(env["msg_id"], "in", from_agent, kind, payload)

    if kind == "send":
        log(f"{CYAN}← send  {RESET}{DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET}: {payload}", "")
        audit_log_fn("in", env["msg_id"], from_agent, "send", "logged")
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
        answer = await call_claude(question, dangerous, workdir, from_agent, cfg.my_id, thread_context)
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


async def poll_loop(dangerous: bool, workdir: Path, cfg, sign_fn, verify_fn, audit_log_fn,
                     record_thread_fn=None, load_thread_fn=None, format_thread_fn=None) -> None:
    banner(f"Clade Daemon — my_id={cfg.my_id}")
    log(f"relay:       {cfg.relay_url}")
    log(f"peers:       {list(cfg.peers.keys())}")
    log(f"mode:        {'YOLO (--dangerously-skip-permissions)' if dangerous else 'safe (sa permissions check)'}")
    log(f"poll:        every {POLL_INTERVAL_S}s")
    log(f"audit:       {cfg.audit_db}")
    log(f"workdir:     {workdir}")
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
                    for env in data.get("messages", []):
                        peer = env.get("from_agent")
                        if peer not in cfg.peers:
                            log(f"{RED}? msg from unknown peer '{peer}' — ignorisem{RESET}", "")
                            audit_log_fn("rejected", env.get("msg_id"), peer, env.get("kind"), "unknown_peer")
                            continue
                        if not verify_fn(cfg.peers[peer], env):
                            log(f"{RED}? msg from {peer} HMAC failed — ignorisem (mozda tampered/MITM){RESET}", "")
                            audit_log_fn("rejected", env.get("msg_id"), peer, env.get("kind"), "bad_hmac")
                            continue
                        # Process in foreground (sekvencijalno) — paralel bi mozda zakomplikovao audit
                        await process_message(env, dangerous, workdir, cfg, sign_fn,
                                              audit_log_fn,
                                              record_thread_fn=record_thread_fn,
                                              load_thread_fn=load_thread_fn,
                                              format_thread_fn=format_thread_fn)
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
    args = parser.parse_args()

    # Late import — agent.main loaduje config iz CLADE_CONFIG env-a, mora postojati
    try:
        from agent.main import (
            cfg, sign, verify, audit_log,
            record_thread_message, load_thread_history, format_thread_for_prompt,
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
    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        import tempfile
        workdir = Path(tempfile.mkdtemp(prefix=f"clade-daemon-{cfg.my_id}-"))

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
        ))
        monitor = asyncio.create_task(outbox_monitor_loop(cfg, _audit_conn, outbox_mod))
        try:
            await poll  # poll_loop drzi semantiku zivota; monitor ide u pozadini
        finally:
            monitor.cancel()
            try:
                await monitor
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

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
SHUTDOWN_EVENT = asyncio.Event()


def log(msg: str, color: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {color}{msg}{RESET}", flush=True)


def banner(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}═══ {msg} ═══{RESET}", flush=True)


async def call_claude(question: str, dangerous: bool, workdir: Path,
                       from_peer: str, my_id: str) -> str:
    """Spawn `claude --print` da izracuna odgovor na peer-ovo pitanje.

    Workdir kontrolise koji CLAUDE.md i .mcp.json se ucitavaju — ali za
    daemon mi koristimo dedicated workdir BEZ CLAUDE.md (da se izbegne
    rekurzivno pollovanje), prosledjujemo sve sto treba direktno u prompt.
    """
    system_prompt = (
        f"Ti si '{my_id}' agent u Clade A2A sistemu. "
        f"Drugi peer agent '{from_peer}' ti je upravo postavio pitanje. "
        f"Odgovori sazeto, tacno, u jednoj-dve recenice. "
        f"Ako ne znas odgovor, kaži to direktno — ne izmišljaj."
    )

    args = ["claude", "--print", "--append-system-prompt", system_prompt]
    if dangerous:
        args.append("--dangerously-skip-permissions")
    args.extend(["--", question])

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
                      to: str, cfg, sign_fn) -> bool:
    """Konstrui + potpisi reply envelope, posalji relay-u."""
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


async def process_message(env: dict, dangerous: bool, workdir: Path, cfg, sign_fn, audit_log_fn) -> None:
    from_agent = env["from_agent"]
    kind = env["kind"]
    payload = env["payload"]
    msg_id_short = env["msg_id"][:8]

    if kind == "send":
        log(f"{CYAN}← send  {RESET}{DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET}: {payload}", "")
        audit_log_fn("in", env["msg_id"], from_agent, "send", "logged")
        return

    if kind == "ask":
        corr = env.get("correlation_id")
        question = payload.get("question") if isinstance(payload, dict) else str(payload)
        log(f"{YELLOW}← ask   {RESET}{DIM}from{RESET} {BOLD}{from_agent}{RESET} {DIM}({msg_id_short}){RESET}: {question}", "")
        log(f"{DIM}  ⏳ computing reply via claude --print{' --yolo' if dangerous else ''}...{RESET}", "")

        t0 = time.time()
        answer = await call_claude(question, dangerous, workdir, from_agent, cfg.my_id)
        elapsed = time.time() - t0

        # Truncate prikaza za log (full answer ide u reply)
        answer_short = answer[:80] + ("..." if len(answer) > 80 else "")
        log(f"{GREEN}→ reply {RESET}{DIM}to  {RESET}{BOLD}{from_agent}{RESET} {DIM}({elapsed:.1f}s){RESET}: {answer_short}", "")

        ok = await send_reply(corr, {"answer": answer}, from_agent, cfg, sign_fn)
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


async def poll_loop(dangerous: bool, workdir: Path, cfg, sign_fn, verify_fn, audit_log_fn) -> None:
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
                        await process_message(env, dangerous, workdir, cfg, sign_fn, audit_log_fn)
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
        from agent.main import cfg, sign, verify, audit_log
    except SystemExit:
        print("FATAL: CLADE_CONFIG nije setovan ili config fajl ne postoji.", file=sys.stderr)
        print("Postavi: export CLADE_CONFIG=/path/to/peer.yaml", file=sys.stderr)
        sys.exit(1)

    # Daemon workdir za claude --print
    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        import tempfile
        workdir = Path(tempfile.mkdtemp(prefix=f"clade-daemon-{cfg.my_id}-"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(signum, _frame):
        sig_name = signal.Signals(signum).name
        log(f"{YELLOW}signal {sig_name} primljen, gasim cisto...{RESET}", "")
        loop.call_soon_threadsafe(SHUTDOWN_EVENT.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(poll_loop(args.dangerous, workdir, cfg, sign, verify, audit_log))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        log(f"{GREEN}daemon stopped cleanly.{RESET}", "")


if __name__ == "__main__":
    main()

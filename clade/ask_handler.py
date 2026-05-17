"""Ask handler — spawn `claude --print` za auto-reply na peer-ov `ask`.

Portovano iz v1 `agent/daemon.py` (tag v1.2.0). U v2 ovaj kod tece UNUTAR
`clade serve` proceca (deo `Server._on_peer_envelope` flow-a), ne kao
spoljasnji poll loop kao v1.

Tok:
1. `Server._on_peer_envelope` prima `kind="ask"` envelope
2. Loaduje thread context iz `ThreadCache` (ako ima `thread_id`)
3. Zove `handle_ask(...)` → spawn `claude --print` u peer.workdir
4. Output → reply envelope sa `payload.answer`

`workdir` mora biti konfigurisan u peers.yaml (`peer.workdir`). Tipicno
generisan kroz `clade init` u `~/.local/state/clade/workdirs/<peer>/`.
Workdir treba da sadrzi `.mcp.json` (vec generisan od `clade init`) ako
zelimo da spawnovan Claude moze da pozove clade tools (clarify ili
recursive ask)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from clade.thread_cache import ThreadMsg

LOG = logging.getLogger("clade.ask")

# Min headless env vars — suppress skills/feedback noise iz Claude Code-a.
# Direktan port iz v1.2.0 agent/daemon.py:MINIMAL_HEADLESS_ENV.
MINIMAL_HEADLESS_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
}

CLAUDE_TIMEOUT_S: float = 90.0


def extract_question(payload: Any) -> str:
    """Fallback chain: payload.question → payload.text → ceo payload (bez _meta).

    Razlog: `clade_message(content="str")` pakuje u `{"text": str}`, a stara
    konvencija je `{"question": str}`. Podrzavamo oba bez breakage-a."""
    if not isinstance(payload, dict):
        return str(payload) if payload is not None else "(prazno pitanje)"
    q = payload.get("question") or payload.get("text")
    if q:
        return str(q)
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    if not clean:
        return "(prazno pitanje)"
    return json.dumps(clean, ensure_ascii=False)


def format_thread_for_prompt(history: list[ThreadMsg], my_id: str) -> str:
    """Formatiraj thread history kao text-section za system prompt.

    Hronoloski (oldest first, kao ThreadCache.get_context vraca), sa
    direction marker-ima. Iskljucuje `_meta` polja iz payload prikaza —
    `_thread_id` i ostali interni kljucevi nisu user-relevant."""
    if not history:
        return ""
    lines = ["[Thread continuity — prethodne poruke u istom threadu, hronoloski:]"]
    for h in history:
        ts = time.strftime("%H:%M:%S", time.localtime(h.ts_ms / 1000))
        arrow = f"← {h.peer}" if h.direction == "in" else f"→ {h.peer}"
        kind = h.kind or "?"
        clean_payload = {k: v for k, v in h.payload.items() if not k.startswith("_")}
        payload_str = json.dumps(clean_payload, ensure_ascii=False)[:200]
        lines.append(f"  {ts}  {arrow}  [{kind}]  {payload_str}")
    lines.append("[Kraj thread konteksta. Tvoj sadasnji odgovor:]")
    return "\n".join(lines)


def build_system_prompt(my_id: str, from_peer: str, thread_context: str = "") -> str:
    """Sklopi system prompt za spawn-ovan `claude --print`.

    Base prompt opisuje ulogu + format odgovora. Opcioni `thread_context`
    iz `format_thread_for_prompt()` se prepend-uje."""
    base = (
        f"Ti si '{my_id}' agent u Clade A2A sistemu (protokol v2.x). "
        f"Drugi peer agent '{from_peer}' ti je upravo postavio pitanje. "
        f"Odgovori sazeto, tacno, u jednoj-dve recenice. "
        f"Ako ne znas odgovor, kazi to direktno — ne izmisljaj. "
        f"Imas pristup clade_* MCP tool-ovima ako ti TREBA da pitas treceg peer-a, "
        f"ali izbegavaj — preferiraj direktan odgovor."
    )
    return f"{base}\n\n{thread_context}" if thread_context else base


async def spawn_claude_print(
    question: str,
    system_prompt: str,
    workdir: Path,
    *,
    dangerous: bool = False,
    timeout_s: float = CLAUDE_TIMEOUT_S,
) -> str:
    """Spawn `claude --print` sa minimal headless env-om.

    Args:
        question: ono sto se prosledjuje kao positional argument posle `--`
        system_prompt: ide kroz `--append-system-prompt`
        workdir: cwd za proces. Ako sadrzi `.mcp.json`, automatski se dodaje
                 `--mcp-config <workdir>/.mcp.json` (spawnovan Claude time
                 dobija clade tools eager-loaded).
        dangerous: ako True, dodaj `--dangerously-skip-permissions`. Default
                   False za sigurnost — ako headless Claude pokusa tool koji
                   trazi permission, dobice timeout.
        timeout_s: max sekundi pre SIGTERM + 5s pre SIGKILL.

    Returns: stdout string. Special prefix `[ask-handler:...]` za error
    slucajeve (timeout, non-zero exit, prazan output) — pozivac moze ga
    forward-ovati senderu ali zna da nije normalan LLM odgovor."""
    safe_question = question if question else "(prazno pitanje od peer-a)"

    workdir.mkdir(parents=True, exist_ok=True)
    mcp_config = workdir / ".mcp.json"

    args = ["claude", "--print", "--append-system-prompt", system_prompt]
    if mcp_config.exists():
        args.extend(["--mcp-config", str(mcp_config)])
    if dangerous:
        args.append("--dangerously-skip-permissions")
    args.extend(["--", safe_question])

    env = {**os.environ, **MINIMAL_HEADLESS_ENV}

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return f"[ask-handler: claude --print timeout posle {int(timeout_s)}s]"

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace")[:200].strip()
        return f"[ask-handler: claude exited {proc.returncode}: {err_text}]"

    answer = stdout.decode("utf-8", errors="replace").strip()
    if not answer:
        return "[ask-handler: claude returned empty response]"
    return answer


async def handle_ask(
    question: str,
    from_peer: str,
    my_id: str,
    workdir: Path,
    thread_history: list[ThreadMsg] | None = None,
    *,
    dangerous: bool = False,
    timeout_s: float = CLAUDE_TIMEOUT_S,
) -> str:
    """High-level handler: build prompt + spawn + return answer.

    Pozivac (`Server._on_peer_envelope`) prosledjuje thread_history vec
    fetched iz ThreadCache. Ako prazno → bez thread konteksta."""
    thread_ctx = format_thread_for_prompt(thread_history or [], my_id)
    system_prompt = build_system_prompt(my_id, from_peer, thread_ctx)
    LOG.info("ask handler: spawning claude --print za pitanje od %s (thread=%d msgs)",
             from_peer, len(thread_history or []))
    return await spawn_claude_print(
        question=question, system_prompt=system_prompt, workdir=workdir,
        dangerous=dangerous, timeout_s=timeout_s,
    )

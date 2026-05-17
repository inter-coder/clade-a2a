"""v2.1.0 testovi za clade.ask_handler — extract_question, format helpers,
spawn_claude_print sa mock-om (ne pokrece pravi claude binary)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from clade.ask_handler import (
    MINIMAL_HEADLESS_ENV,
    build_system_prompt,
    extract_question,
    format_thread_for_prompt,
    handle_ask,
    spawn_claude_print,
)
from clade.thread_cache import ThreadMsg


# ---- extract_question ----


def test_extract_question_chain():
    """Fallback chain: question → text → ceo payload (bez _meta)."""
    assert extract_question({"question": "Q?"}) == "Q?"
    assert extract_question({"text": "T"}) == "T"
    # question wins ako oba postoje
    assert extract_question({"question": "Q?", "text": "T"}) == "Q?"
    # _meta polja se filtriraju u fallback-u
    assert extract_question({"text": "T", "_thread_id": "t1"}) == "T"
    # Ni question ni text → ceo payload (bez _meta)
    out = extract_question({"x": 1, "_thread_id": "t1"})
    assert "_thread_id" not in out
    assert '"x":1' in out.replace(" ", "")
    # Edge: None
    assert extract_question(None) == "(prazno pitanje)"
    # Edge: prazan dict
    assert extract_question({}) == "(prazno pitanje)"
    # Non-dict
    assert extract_question("raw string") == "raw string"


# ---- format_thread_for_prompt ----


def test_format_thread_empty_is_empty_string():
    assert format_thread_for_prompt([], my_id="alice") == ""


def test_format_thread_basic_shape():
    history = [
        ThreadMsg(msg_id="1", ts_ms=1700000000000, direction="in",
                  peer="alice", kind="ask",
                  payload={"question": "Q?", "_thread_id": "t1"}),
        ThreadMsg(msg_id="2", ts_ms=1700000001000, direction="out",
                  peer="alice", kind="reply",
                  payload={"answer": "A", "_thread_id": "t1"}),
    ]
    text = format_thread_for_prompt(history, my_id="bob")
    assert "Thread continuity" in text
    assert "Q?" in text
    assert '"A"' in text
    # _meta polja ne smeju biti u prikazu
    assert "_thread_id" not in text
    # Format ima vremenske stamp-ove i strelice
    assert "← alice" in text
    assert "→ alice" in text


# ---- build_system_prompt ----


def test_build_system_prompt_includes_role_context():
    p = build_system_prompt(my_id="bob", from_peer="alice")
    assert "'bob'" in p
    assert "'alice'" in p
    assert "Clade A2A" in p
    # Bez thread_context — samo base
    assert "Thread continuity" not in p


def test_build_system_prompt_appends_thread_context():
    history = [ThreadMsg(msg_id="1", ts_ms=1700000000000, direction="in",
                         peer="alice", kind="send", payload={"text": "hi"})]
    ctx = format_thread_for_prompt(history, "bob")
    p = build_system_prompt(my_id="bob", from_peer="alice", thread_context=ctx)
    assert "Thread continuity" in p
    assert "'bob'" in p
    assert "hi" in p


# ---- MINIMAL_HEADLESS_ENV ----


def test_minimal_headless_env_constants():
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_POLICY_SKILLS"] == "1"
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"


# ---- spawn_claude_print (mockovan subprocess) ----


@pytest.mark.asyncio
async def test_spawn_claude_print_success(tmp_path: Path):
    """Mockovan subprocess vraca cisti stdout → answer string."""
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"56\n", b"")
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        answer = await spawn_claude_print(
            question="7*8?", system_prompt="ti si bob", workdir=tmp_path,
        )
    assert answer == "56"

    # Check da je args lista pravilna: claude --print --append-system-prompt ...
    call_args = mock_exec.call_args.args
    assert call_args[0] == "claude"
    assert call_args[1] == "--print"
    assert "--append-system-prompt" in call_args
    assert "--" in call_args
    assert call_args[-1] == "7*8?"


@pytest.mark.asyncio
async def test_spawn_claude_print_includes_mcp_config_if_exists(tmp_path: Path):
    """Ako workdir/.mcp.json postoji, args ukljucuju --mcp-config."""
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text('{"mcpServers": {}}')

    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"ok", b"")
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        await spawn_claude_print("Q", "P", workdir=tmp_path)
    call_args = mock_exec.call_args.args
    assert "--mcp-config" in call_args
    assert str(mcp_path) in call_args


@pytest.mark.asyncio
async def test_spawn_claude_print_timeout(tmp_path: Path):
    """Ako communicate timeout, vrati specijalan error string (ne crash)."""
    fake_proc = AsyncMock()
    # communicate() koji nikad ne zavrsava — asyncio.wait_for ce timeout-ovati
    fake_proc.communicate = lambda: asyncio.sleep(10)
    fake_proc.terminate = lambda: None
    fake_proc.wait = AsyncMock(return_value=None)
    fake_proc.kill = lambda: None

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        answer = await spawn_claude_print(
            "Q", "P", workdir=tmp_path, timeout_s=0.1,
        )
    assert "[ask-handler:" in answer
    assert "timeout" in answer.lower()


@pytest.mark.asyncio
async def test_spawn_claude_print_non_zero_exit(tmp_path: Path):
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"", b"some error\n")
    fake_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        answer = await spawn_claude_print("Q", "P", workdir=tmp_path)
    assert "[ask-handler:" in answer
    assert "exited 1" in answer
    assert "some error" in answer


@pytest.mark.asyncio
async def test_spawn_claude_print_empty_response(tmp_path: Path):
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"   \n", b"")
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        answer = await spawn_claude_print("Q", "P", workdir=tmp_path)
    assert "[ask-handler:" in answer
    assert "empty" in answer.lower()


@pytest.mark.asyncio
async def test_spawn_claude_print_empty_question_uses_placeholder(tmp_path: Path):
    """Pazi: ako question je prazan string ili None, koristi placeholder
    umesto da prosledjuje empty string (TypeError u fork_exec za None)."""
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"ok", b"")
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        await spawn_claude_print("", "P", workdir=tmp_path)
    call_args = mock_exec.call_args.args
    # poslednji arg je question — treba placeholder, ne prazan string
    assert call_args[-1] == "(prazno pitanje od peer-a)"


# ---- handle_ask (end-to-end sa mock-om) ----


@pytest.mark.asyncio
async def test_handle_ask_with_thread_history(tmp_path: Path):
    """handle_ask combina sve: extract → format thread → build prompt → spawn."""
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"56\n", b"")
    fake_proc.returncode = 0

    history = [
        ThreadMsg(msg_id="1", ts_ms=1700000000000, direction="in",
                  peer="alice", kind="ask", payload={"question": "Q1?"}),
    ]
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        answer = await handle_ask(
            question="7*8?", from_peer="alice", my_id="bob",
            workdir=tmp_path, thread_history=history,
        )
    assert answer == "56"

    # System prompt treba da sadrzi thread context
    args = mock_exec.call_args.args
    sys_prompt_idx = list(args).index("--append-system-prompt") + 1
    sys_prompt = args[sys_prompt_idx]
    assert "'bob'" in sys_prompt
    assert "'alice'" in sys_prompt
    assert "Thread continuity" in sys_prompt
    assert "Q1?" in sys_prompt


@pytest.mark.asyncio
async def test_handle_ask_without_history(tmp_path: Path):
    fake_proc = AsyncMock()
    fake_proc.communicate.return_value = (b"answer", b"")
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        await handle_ask(
            question="Q", from_peer="alice", my_id="bob",
            workdir=tmp_path, thread_history=None,
        )
    args = mock_exec.call_args.args
    sys_prompt_idx = list(args).index("--append-system-prompt") + 1
    sys_prompt = args[sys_prompt_idx]
    # Bez history → nema "Thread continuity" segmenta
    assert "Thread continuity" not in sys_prompt

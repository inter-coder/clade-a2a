"""End-to-end agent testovi: HMAC signing/verify, outbox, ask/reply."""

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module(config_path: Path):
    """Ucitaj agent modul sa specificnim config-om. Reload-uje da pokupi novi config."""
    os.environ["CLADE_CONFIG"] = str(config_path)
    # Ako je vec ucitan, reload
    if "agent.main" in sys.modules:
        del sys.modules["agent.main"]
    if "agent.outbox" in sys.modules:
        del sys.modules["agent.outbox"]
    import agent.main  # noqa: PLC0415
    return agent.main


@pytest.mark.asyncio
async def test_alice_send_to_bob_delivered(relay_process, alice_config, bob_config, token_for_bob, relay_url):
    """Alice salje, Bob preko relay-a cita inbox + verifikuje HMAC."""
    # Alice side
    alice = _load_agent_module(alice_config)
    r = await alice.clade_send(to="bob", payload={"text": "hello bob"})
    assert r.get("ok") is True
    assert "msg_id" in r
    assert "queued" not in r  # delivered odmah, ne queued

    # Bob side
    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1, f"Bob expected 1 msg, got {inbox}"
    assert len(inbox.get("rejected", [])) == 0, f"Should not have rejected msgs: {inbox['rejected']}"
    msg = inbox["messages"][0]
    assert msg["from_agent"] == "alice"
    assert msg["payload"] == {"text": "hello bob"}


@pytest.mark.asyncio
async def test_tampered_hmac_rejected_by_receiver(relay_process, alice_config, bob_config, relay_url, token_for_alice):
    """Tampered HMAC: relay accepts (ne zna secret), bob odbacuje."""
    # Alice salje rucno preko curl-a sa tampered HMAC-om
    alice = _load_agent_module(alice_config)
    env = alice._make_envelope("send", "bob", {"text": "tampered"})
    env["hmac"] = "deadbeef" * 8
    # nonce mora biti svez
    import secrets
    env["nonce"] = secrets.token_hex(16)

    async with httpx.AsyncClient() as c:
        r = await c.post(f"{relay_url}/send", json=env,
                         headers={"Authorization": f"Bearer {token_for_alice}"}, timeout=3)
    assert r.status_code == 200  # relay accept

    # Bob cita inbox
    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 0, "Tampered msg should NOT be in verified messages"
    assert len(inbox["rejected"]) == 1
    assert "HMAC" in inbox["rejected"][0]["reason"]


@pytest.mark.asyncio
async def test_full_ask_reply_roundtrip(relay_process, alice_config, bob_config):
    """Alice ask → bob reply → alice prima verified response."""
    alice = _load_agent_module(alice_config)
    bob = _load_agent_module(bob_config)

    # Alice salje ask u background-u
    ask_task = asyncio.create_task(
        alice.clade_ask(to="bob", payload={"question": "7*8"}, timeout_s=10)
    )
    await asyncio.sleep(0.5)  # daj malo vremena da ask stigne u inbox

    # Bob cita inbox + odgovara
    bob = _load_agent_module(bob_config)  # reload da pokupi alice-ovu poruku
    # NAPOMENA: re-importing alice modul moze zbrkati outbox/audit fajlove —
    # za ovaj test koristimo httpx direktno za bob da izbegnemo to.

    import httpx as h
    import sys as s

    # Ucitaj bob config eksplicitno
    s.modules.pop("agent.main", None)
    s.modules.pop("agent.outbox", None)
    os.environ["CLADE_CONFIG"] = str(bob_config)
    import agent.main as bobm

    inbox = await bobm.clade_inbox()
    assert inbox["count"] == 1
    ask_msg = inbox["messages"][0]
    assert ask_msg["kind"] == "ask"
    corr = ask_msg["correlation_id"]

    # Bob odgovori
    reply_result = await bobm.clade_reply(correlation_id=corr, response={"answer": "56"}, to="alice")
    assert reply_result.get("ok") is True

    # Cekaj alice da dobije
    alice_result = await ask_task
    assert alice_result.get("ok") is True
    assert alice_result["response"] == {"answer": "56"}
    assert alice_result["correlation_id"] == corr


@pytest.mark.asyncio
async def test_outbox_queue_on_relay_down(workdir, alice_config, shared_secret, token_for_alice):
    """Send kad je relay DOWN → poruka u outbox-u, vraca queued=True."""
    # Promenimo relay_url u ne-postojeci endpoint
    import yaml
    cfg_data = yaml.safe_load(alice_config.read_text())
    cfg_data["relay_url"] = "http://127.0.0.1:1"  # garantovano DOWN
    alice_config.write_text(yaml.dump(cfg_data))

    alice = _load_agent_module(alice_config)
    r = await alice.clade_send(to="bob", payload={"text": "queued"})
    assert r.get("ok") is True
    assert r.get("queued") is True
    assert "outbox_row" in r

    # Status izvestava 1 pending
    status = await alice.clade_outbox_status()
    assert status["stats"]["pending"] >= 1


# ---- v1.0.0 clade_message + lock tests ----


@pytest.mark.asyncio
async def test_clade_message_send_fire_and_forget(relay_process, alice_config, bob_config):
    """clade_message(expect_reply=False) == fire-and-forget send."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(to="bob", content="hello via clade_message")
    assert r.get("ok") is True
    assert "msg_id" in r
    assert "response" not in r

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1
    msg = inbox["messages"][0]
    assert msg["kind"] == "send"
    assert msg["payload"]["text"] == "hello via clade_message"


@pytest.mark.asyncio
async def test_clade_message_dict_content(relay_process, alice_config, bob_config):
    """content kao dict se prosledjuje as-is."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(to="bob", content={"custom": "field", "x": 42})
    assert r.get("ok") is True

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    assert inbox["count"] == 1
    assert inbox["messages"][0]["payload"] == {"custom": "field", "x": 42}


@pytest.mark.asyncio
async def test_clade_message_reply_to_threading(relay_process, alice_config, bob_config):
    """reply_to + thread_id se serijalizuju u payload kao _reply_to / _thread_id."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(
        to="bob",
        content="follow-up",
        reply_to="parent-msg-123",
        thread_id="thread-abc",
    )
    assert r.get("ok") is True

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    payload = inbox["messages"][0]["payload"]
    assert payload["_reply_to"] == "parent-msg-123"
    assert payload["_thread_id"] == "thread-abc"
    assert payload["text"] == "follow-up"


@pytest.mark.asyncio
async def test_clade_message_expect_reply_roundtrip(relay_process, alice_config, bob_config):
    """clade_message(expect_reply=True) ide kroz /ask, blokira do reply-a."""
    alice = _load_agent_module(alice_config)
    bob = _load_agent_module(bob_config)

    ask_task = asyncio.create_task(
        alice.clade_message(to="bob", content="3+4", expect_reply=True, timeout_s=10)
    )
    await asyncio.sleep(0.5)

    # Bob reload (drugi config) + reply
    import sys as s
    s.modules.pop("agent.main", None)
    s.modules.pop("agent.outbox", None)
    os.environ["CLADE_CONFIG"] = str(bob_config)
    import agent.main as bobm  # noqa: PLC0415

    inbox = await bobm.clade_inbox()
    assert inbox["count"] == 1
    ask_msg = inbox["messages"][0]
    assert ask_msg["kind"] == "ask"
    corr = ask_msg["correlation_id"]
    assert corr is not None

    await bobm.clade_reply(correlation_id=corr, response={"answer": "7"}, to="alice")

    res = await ask_task
    assert res.get("ok") is True
    assert res["response"] == {"answer": "7"}


@pytest.mark.asyncio
async def test_inbox_blocked_by_active_daemon_lock(relay_process, alice_config):
    """Ako lock fajl postoji + PID zivi → clade_inbox vraca busy."""
    alice = _load_agent_module(alice_config)
    lock = alice._daemon_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # nasa PID = sigurno zivi
    try:
        result = await alice.clade_inbox()
        assert "error" in result
        assert "busy" in result["error"].lower()
        assert str(os.getpid()) in result["error"]
    finally:
        lock.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_inbox_passes_when_lock_is_stale(relay_process, alice_config):
    """Stale lock (PID koji ne postoji) ne blokira clade_inbox."""
    alice = _load_agent_module(alice_config)
    lock = alice._daemon_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    # PID 999999 — gotovo sigurno ne postoji na ovoj masini
    lock.write_text("999999")
    try:
        result = await alice.clade_inbox()
        # Treba ili da uspesno citra (count=0) ili da vrati error koji NIJE busy
        assert "error" not in result or "busy" not in result.get("error", "").lower()
    finally:
        lock.unlink(missing_ok=True)


# ---- v1.1.0 thread persistence tests ----


@pytest.mark.asyncio
async def test_thread_message_recorded_on_send(relay_process, alice_config, bob_config):
    """clade_message sa thread_id record-uje u alice-ovom thread_history."""
    alice = _load_agent_module(alice_config)
    r = await alice.clade_message(to="bob", content="msg1", thread_id="t-abc")
    assert r.get("ok") is True

    history = alice.load_thread_history("t-abc")
    assert len(history) == 1
    assert history[0]["direction"] == "out"
    assert history[0]["peer"] == "bob"
    assert history[0]["kind"] == "send"
    assert history[0]["payload"]["text"] == "msg1"
    assert history[0]["payload"]["_thread_id"] == "t-abc"


@pytest.mark.asyncio
async def test_thread_history_chronological_order_and_limit(relay_process, alice_config):
    """load_thread_history vraca hronoloski (oldest first), poslednji N."""
    alice = _load_agent_module(alice_config)
    for i in range(5):
        await alice.clade_message(to="bob", content=f"msg{i}", thread_id="t-order")
        # Mali sleep da ts_ms budu razliciti (1ms granularnost je dovoljna)
        await asyncio.sleep(0.01)

    history = alice.load_thread_history("t-order", max_messages=3)
    assert len(history) == 3
    # Treba da bude poslednje 3, hronoloski rastuce
    assert history[0]["payload"]["text"] == "msg2"
    assert history[1]["payload"]["text"] == "msg3"
    assert history[2]["payload"]["text"] == "msg4"


@pytest.mark.asyncio
async def test_thread_not_recorded_without_thread_id(relay_process, alice_config):
    """Bez thread_id-a, thread_history ostaje prazan (no kontaminacija)."""
    alice = _load_agent_module(alice_config)
    await alice.clade_message(to="bob", content="bez threada")

    history = alice.load_thread_history("")
    assert history == []
    # Direktan SQL count provera
    cur = alice._audit_conn.execute("SELECT COUNT(*) FROM thread_history")
    assert cur.fetchone()[0] == 0


@pytest.mark.asyncio
async def test_thread_incoming_recorded_via_inbox(relay_process, alice_config, bob_config):
    """Kad bob ucita inbox sa thread-tagovanom porukom, record-uje incoming."""
    alice = _load_agent_module(alice_config)
    await alice.clade_message(to="bob", content="ka bobu", thread_id="t-incoming-unique")

    bob = _load_agent_module(bob_config)
    inbox = await bob.clade_inbox()
    # Session-scoped relay nakuplja poruke iz prethodnih testova; samo proveravamo
    # da naša thread-tagovana poruka jeste medju verifikovanim
    matching = [m for m in inbox["messages"]
                if m["payload"].get("_thread_id") == "t-incoming-unique"]
    assert len(matching) == 1
    assert matching[0]["from_agent"] == "alice"

    history = bob.load_thread_history("t-incoming-unique")
    assert len(history) == 1
    assert history[0]["direction"] == "in"
    assert history[0]["peer"] == "alice"


@pytest.mark.asyncio
async def test_format_thread_for_prompt_basic_shape(relay_process, alice_config):
    """format_thread_for_prompt produces non-empty text za non-empty history,
    skraceno bez _meta polja."""
    alice = _load_agent_module(alice_config)
    history = [
        {"msg_id": "1", "ts_ms": 1700000000000, "direction": "in",
         "peer": "alice", "kind": "ask", "payload": {"question": "Q?", "_thread_id": "t1"}},
        {"msg_id": "2", "ts_ms": 1700000001000, "direction": "out",
         "peer": "alice", "kind": "reply", "payload": {"answer": "A", "_thread_id": "t1"}},
    ]
    text = alice.format_thread_for_prompt(history, "bob")
    assert "Thread continuity" in text
    assert "Q?" in text
    assert "A" in text
    # _thread_id ne sme da bude u clean payload-u
    assert "_thread_id" not in text


@pytest.mark.asyncio
async def test_relay_ask_default_timeout_is_90(relay_process, relay_url):
    """AskBody.timeout_s default je 90s u v1.1.0 (bio 120)."""
    from relay.main import AskBody  # noqa: PLC0415
    # Pydantic exposed schema
    schema = AskBody.model_json_schema()
    timeout_default = schema["properties"]["timeout_s"].get("default")
    assert timeout_default == 90, f"Expected timeout_s default 90, got {timeout_default}"


# ---- v1.2.0 P2 tests ----


def test_daemon_minimal_settings_written():
    """write_minimal_settings kreira workdir/.claude/settings.json sa skill overrides."""
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from agent.daemon import write_minimal_settings  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        path = write_minimal_settings(wd)
        assert path.exists()
        assert path == wd / ".claude" / "settings.json"
        data = _json.loads(path.read_text())
        assert data["spinnerTipsEnabled"] is False
        assert data["skillOverrides"]["/init"] == "off"
        assert data["skillOverrides"]["frontend-design"] == "off"
        # v1.4.3: pre-odobrene permissions za self-introspection — bez ovog
        # daemon-spawn Claude (safe mode) visi cekajuci user prompt za ls/cat.
        allow = data["permissions"]["allow"]
        assert "Read(/opt/clade-a2a/**)" in allow
        assert any(p.endswith("/clade-agent/**)") and p.startswith("Read(") for p in allow)
        assert any(p.endswith("/.clade/**)") and p.startswith("Read(") for p in allow)
        assert f"Read({wd}/**)" in allow
        assert "Bash(ls:*)" in allow
        assert "Bash(cat:*)" in allow


def test_daemon_env_context_collected():
    """v1.4.4: _collect_env_context vraca hostname/ip/datum/putanje koje
    se injectuju u sistem prompt. Bez ovog peer Claude halucinise meta info."""
    import tempfile  # noqa: PLC0415
    from agent.daemon import _collect_env_context  # noqa: PLC0415

    class _CfgStub:
        audit_db = "/tmp/x-audit.db"

    with tempfile.TemporaryDirectory() as d:
        ctx = _collect_env_context(Path(d), _CfgStub())
    # Sve kljucevi su tu, i imaju non-empty string vrednosti
    assert set(ctx.keys()) == {"hostname", "primary_ip", "today",
                                "workdir", "audit_db", "config_path"}
    assert ctx["hostname"]
    assert ctx["primary_ip"]
    # Datum mora biti ISO format YYYY-MM-DD
    assert len(ctx["today"]) == 10 and ctx["today"][4] == "-"


def test_daemon_peer_directory_excludes_sender():
    """_format_peer_directory mora da iskljuci sender peer-a (vec je u
    'Drugi peer ti je postavio pitanje' sekciji) i da listira ostale sa
    imenom + role-om."""
    from agent.daemon import _format_peer_directory  # noqa: PLC0415

    class _PI:
        def __init__(self, name, role):
            self.name = name
            self.role = role
    class _Cfg:
        peers = {
            "bob": _PI("Bob", "backend"),
            "charlie": _PI("Charlie", "qa\nsecond line ignored"),
            "alice": _PI("Alice", "frontend"),
        }

    out = _format_peer_directory(_Cfg(), exclude_id="bob")
    assert "Bob" not in out  # sender iskljucen
    assert "Charlie" in out
    assert "Alice" in out
    assert "qa" in out  # samo prvi red role-a
    assert "second line" not in out


def test_daemon_extra_add_dirs_includes_config_and_audit_parents(monkeypatch, tmp_path):
    """_extra_add_dirs mora da vrati parent CLADE_CONFIG-a + parent audit_db-a,
    i da preskoci dirs koji su unutar workdir-a."""
    from agent.daemon import _extra_add_dirs  # noqa: PLC0415

    cfg_p = tmp_path / "cfgs" / "alice.yaml"
    cfg_p.parent.mkdir()
    cfg_p.touch()
    monkeypatch.setenv("CLADE_CONFIG", str(cfg_p))

    class _Cfg:
        audit_db = str(tmp_path / "audits" / "alice.db")
    (tmp_path / "audits").mkdir()

    workdir = tmp_path / "wd"
    workdir.mkdir()

    dirs = _extra_add_dirs(_Cfg(), workdir)
    assert str(tmp_path / "cfgs") in dirs
    assert str(tmp_path / "audits") in dirs
    # workdir samo ne sme biti dva puta
    assert str(workdir) not in dirs


def test_daemon_minimal_env_constants():
    """MINIMAL_HEADLESS_ENV ima ocekivane suppress varijable."""
    from agent.daemon import MINIMAL_HEADLESS_ENV  # noqa: PLC0415
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_POLICY_SKILLS"] == "1"
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert MINIMAL_HEADLESS_ENV["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"


def test_daemon_extract_question_handles_all_payload_shapes():
    """clade_message(content="x") salje {"text": "x"} (bez 'question' key-a) —
    daemon mora to da prepozna i ne sme da vrati None."""
    from agent.daemon import _extract_question  # noqa: PLC0415
    # Legacy convention
    assert _extract_question({"question": "Q?"}) == "Q?"
    # clade_message string convention
    assert _extract_question({"text": "Hello"}) == "Hello"
    # Question wins ako oba postoje
    assert _extract_question({"question": "Q?", "text": "T"}) == "Q?"
    # Sa _thread_id meta polja — ne sme se nadji u izlaznom JSON-u
    assert _extract_question({"text": "Hi", "_thread_id": "t1"}) == "Hi"
    # Dict bez 'question' i 'text' → JSON ceo payload (bez _meta)
    out = _extract_question({"a": 1, "_thread_id": "t1"})
    assert "_thread_id" not in out
    assert '"a":1' in out.replace(" ", "")
    # Edge: None
    assert _extract_question(None) == "(prazno pitanje)"
    # Edge: prazan dict
    assert _extract_question({}) == "(prazno pitanje)"
    # Non-dict, non-None
    assert _extract_question("raw string") == "raw string"


# ---- v1.3.0 tests: name + role + PeerInfo schema ----


def test_config_with_name_role_and_peer_info(tmp_path):
    """Config sa novim v1.3.0 poljima parse-uje + helperi rade."""
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text("""
my_id: marko-dev
name: Marko Markovic
role: |
  Ti si frontend developer u SDS timu.
  Specijalnost: React, TypeScript.
bearer_token: tk
peers:
  bob:
    secret: deadbeef
    name: Bob Bobic
    role: Backend dev
audit_db: /tmp/test.db
""")
    main = _load_agent_module(cfg_path)
    assert main.cfg.my_id == "marko-dev"
    assert main.cfg.name == "Marko Markovic"
    assert "frontend developer" in main.cfg.role
    pi = main.cfg.peer_info("bob")
    assert pi is not None
    assert pi.name == "Bob Bobic"
    assert pi.role == "Backend dev"
    assert main.cfg.peer_secret("bob") == "deadbeef"


def test_config_legacy_peers_string_format_still_works(tmp_path):
    """Stari format yaml (peers: id → secret string) i dalje parse-uje."""
    cfg_path = tmp_path / "legacy.yaml"
    cfg_path.write_text("""
my_id: alice
bearer_token: tk
peers:
  bob: deadbeef
audit_db: /tmp/test.db
""")
    main = _load_agent_module(cfg_path)
    assert main.cfg.peer_secret("bob") == "deadbeef"
    assert main.cfg.peer_info("bob") is None  # stari format
    assert main.cfg.name is None  # default
    assert main.cfg.role is None  # default


@pytest.mark.asyncio
async def test_daemon_call_claude_includes_role_in_prompt(monkeypatch, tmp_path):
    """call_claude sa role kao kwarg ubacuje TVOJA ULOGA blok u system prompt."""
    import sys as _sys
    _sys.modules.pop("agent.main", None)
    _sys.modules.pop("agent.daemon", None)

    # Trebamo neki dummy config za agent.main import
    dummy_cfg = tmp_path / "dummy.yaml"
    dummy_cfg.write_text("my_id: x\nbearer_token: tk\npeers: {}\naudit_db: /tmp/x.db\n")
    os.environ["CLADE_CONFIG"] = str(dummy_cfg)

    from agent.daemon import call_claude  # noqa: PLC0415

    captured_args = []

    async def fake_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)

        class FakeProc:
            returncode = 0
            async def communicate(self):
                return (b"OK", b"")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await call_claude(
        question="Sta je?", dangerous=False, workdir=tmp_path,
        from_peer="bob", my_id="alice",
        name="Alice Smith", role="Ti si data analyst, fokus na SQL.",
        from_peer_name="Bob Bobic", from_peer_role="Backend dev",
    )

    # --append-system-prompt ide kao arg posle --print
    args_list = list(captured_args)
    sys_prompt_idx = args_list.index("--append-system-prompt") + 1
    prompt = args_list[sys_prompt_idx]

    assert "KO SI TI" in prompt
    assert "Alice Smith" in prompt
    assert "data analyst" in prompt
    assert "Bob Bobic" in prompt
    assert "Backend dev" in prompt

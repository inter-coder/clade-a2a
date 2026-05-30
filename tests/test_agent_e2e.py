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


def test_humanize_relay_errors_map_known_codes(alice_config):
    """v1.4.6: _humanize_relay_error pretvara raw HTTP kodove u poruke koje
    sender Claude moze da prenese korisniku — proveravamo kljucne mape."""
    alice = _load_agent_module(alice_config)
    h = alice._humanize_relay_error

    msg504 = h(504, "Ask timed out after 90s", "bob", "ask")
    assert "bob" in msg504 and "daemon" in msg504.lower()  # pominjne peer + akciju

    msg401 = h(401, "Invalid bearer token", "bob", "ask")
    assert "401" in msg401 and "setup-server" in msg401.lower()  # pominjne uzrok

    msg403 = h(403, "Forbidden", "bob", "send")
    assert "403" in msg403

    msg404 = h(404, "Not found", "bob", "ask")
    assert "bob" in msg404 and "nepoznat" in msg404.lower()

    msg500 = h(500, "ISE", "bob", "ask")
    assert "relay.log" in msg500  # pominje gde da pogleda log


def test_cancel_auth_check_blocks_other_peers(alice_config):
    """v1.7.0 (C2): cancel od peer-a koji NIJE original sender mora biti
    odbacen. Sprecava maliciozan peer da otkaze tudji ask."""
    import asyncio  # noqa: PLC0415
    import importlib  # noqa: PLC0415
    # Reload daemon module da pokupi fresh state
    if "agent.daemon" in importlib.sys.modules:
        del importlib.sys.modules["agent.daemon"]
    from agent import daemon  # noqa: PLC0415

    # Setup _inflight_by_corr sa fake task + original sender "alice"
    async def fake_task_body():
        await asyncio.sleep(60)

    async def run():
        task = asyncio.create_task(fake_task_body())
        daemon._inflight_by_corr["target-corr-123"] = (task, "alice")
        try:
            # Mock audit_log_fn
            calls = []
            def audit_mock(direction, msg_id, peer, kind, status, note=""):
                calls.append((direction, peer, kind, status))

            # Attack: charlie pokusava cancel
            env = {
                "msg_id": "evil-msg",
                "from_agent": "charlie",
                "to_agent": "bob",
                "kind": "cancel",
                "payload": {"target_correlation_id": "target-corr-123"},
            }
            await daemon.process_message(env, False, None, None, None, audit_mock)
            # Mora biti rejected
            assert any(c[3] == "auth_fail" for c in calls), f"calls: {calls}"
            # Task NE sme biti otkazan
            assert not task.cancelled() and not task.done()

            # Legit: alice (original sender) cancel — mora proci
            env2 = dict(env)
            env2["from_agent"] = "alice"
            env2["msg_id"] = "legit-msg"
            await daemon.process_message(env2, False, None, None, None, audit_mock)
            assert any(c[3] == "cancelled" for c in calls), f"calls posle legit: {calls}"
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(run())


def test_humanize_503_with_retry_after(alice_config):
    """v1.5.0: relay back-pressure 503 mora biti pretvoren u humani message
    sa retry_after_s ako relay vraca strukturisani body."""
    import json as _j  # noqa: PLC0415
    alice = _load_agent_module(alice_config)
    bp_body = _j.dumps({
        "detail": {
            "detail": "Peer busy",
            "retry_after_s": 12,
            "pending_count": 4,
            "target_peer": "bob",
        }
    })
    msg = alice._humanize_relay_error(503, bp_body, "bob", "ask")
    assert "bob" in msg
    assert "zauzet" in msg.lower()
    assert "12" in msg  # retry_after stigao
    assert "pending" in msg.lower()

    # 503 bez strukturisanog detail-a (npr inbox full) → generic message
    msg2 = alice._humanize_relay_error(503, "Inbox full", "bob", "send")
    assert "503" in msg2 and "retry" in msg2.lower()


def test_humanize_network_errors(alice_config):
    """v1.4.6: ConnectError + Timeout u humani format."""
    import httpx as _h  # noqa: PLC0415
    alice = _load_agent_module(alice_config)

    conn_msg = alice._humanize_network_error(_h.ConnectError("refused"), "bob")
    assert "nedostupan" in conn_msg.lower()
    assert "/health" in conn_msg  # predlaze konkretnu proveru

    to_msg = alice._humanize_network_error(_h.ReadTimeout("slow"), "bob")
    assert "timeout" in to_msg.lower() or "retry" in to_msg.lower()


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


# ---- v1.8.0: presence + clade_peers ----

@pytest.mark.asyncio
async def test_relay_presence_endpoint_tracks_heartbeat(
    relay_process, relay_url, token_for_alice, token_for_bob,
):
    """POST /presence belezi last_seen; GET /presence vraca peer kao online u
    okviru TTL-a. Default TTL u test instanci je 35s pa svez heartbeat → online."""
    headers_a = {"Authorization": f"Bearer {token_for_alice}"}
    headers_b = {"Authorization": f"Bearer {token_for_bob}"}

    async with httpx.AsyncClient() as c:
        # Pre bilo kakvog heartbeat-a — oba offline
        r = await c.get(f"{relay_url}/presence", headers=headers_a, timeout=3)
        assert r.status_code == 200
        peers = r.json()["peers"]
        assert "alice" in peers and "bob" in peers
        # Stanje moze biti online ili offline u zavisnosti od prethodnih testova —
        # ne tvrdimo na starting state. Tvrdimo na efekat heartbeat-a:

        # Alice heartbeat
        r = await c.post(f"{relay_url}/presence", headers=headers_a, timeout=3)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["peer"] == "alice"

        # Sad alice mora biti online sa malim secs_ago
        r = await c.get(f"{relay_url}/presence", headers=headers_b, timeout=3)
        peers = r.json()["peers"]
        assert peers["alice"]["online"] is True
        assert peers["alice"]["secs_ago"] is not None
        assert peers["alice"]["secs_ago"] < 5


@pytest.mark.asyncio
async def test_relay_presence_requires_auth(relay_process, relay_url):
    """POST i GET /presence bez bearer-a → 401."""
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{relay_url}/presence", timeout=3)
        assert r.status_code == 401
        r = await c.get(f"{relay_url}/presence", timeout=3)
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_exposes_online_agents(
    relay_process, relay_url, token_for_alice,
):
    """/health.online_agents je podskup known_agents — sadrzi samo peer-ove
    koji su skoro heartbeat-ali. Korisno za setup-server proxy."""
    headers = {"Authorization": f"Bearer {token_for_alice}"}
    async with httpx.AsyncClient() as c:
        await c.post(f"{relay_url}/presence", headers=headers, timeout=3)
        r = await c.get(f"{relay_url}/health", timeout=3)
        body = r.json()
        assert "online_agents" in body
        assert "alice" in body["online_agents"]
        assert "presence_ttl_s" in body


@pytest.mark.asyncio
async def test_clade_peers_tool_returns_structured_list(
    relay_process, alice_config, bob_config, token_for_alice, token_for_bob, relay_url,
):
    """clade_peers MCP tool: posle heartbeat-a oba peer-a, vraca obojicu kao
    online + ukljucuje role iz alice yaml-a za bob."""
    # Heartbeat oba (alice + bob) preko HTTP da relay zna da su online
    async with httpx.AsyncClient() as c:
        await c.post(f"{relay_url}/presence",
                     headers={"Authorization": f"Bearer {token_for_alice}"}, timeout=3)
        await c.post(f"{relay_url}/presence",
                     headers={"Authorization": f"Bearer {token_for_bob}"}, timeout=3)

    alice = _load_agent_module(alice_config)
    res = await alice.clade_peers()
    assert res.get("ok") is True
    assert res["you"] == "alice"
    peer_ids = {p["peer_id"] for p in res["peers"]}
    assert {"alice", "bob"}.issubset(peer_ids)
    by_id = {p["peer_id"]: p for p in res["peers"]}
    assert by_id["alice"]["self"] is True
    assert by_id["bob"]["self"] is False
    assert by_id["alice"]["online"] is True
    assert by_id["bob"]["online"] is True
    # summary format: "2 online / 2 total" — bar 2 online posto smo oba heartbeat-ali
    assert "online" in res["summary"]


@pytest.mark.asyncio
async def test_clade_peers_handles_relay_down(alice_config, monkeypatch):
    """Ako relay nedostupan, clade_peers vraca humani error, ne raises."""
    alice = _load_agent_module(alice_config)
    # Override relay URL na nepostojeci port
    alice.cfg.relay_url = "http://127.0.0.1:1"  # port 1 = connection refused
    res = await alice.clade_peers()
    assert "error" in res
    # Humani error pominjej relay nedostupnost
    assert "relay" in res["error"].lower() or "connect" in res["error"].lower()


# ---- v1.9.0: broadcast + teams + tasks ----

@pytest.mark.asyncio
async def test_clade_broadcast_to_list_parallel_send(
    relay_process, alice_config, bob_config,
):
    """Broadcast sa eksplicitnom listom — alice salje sebi i bob-u istu poruku.
    Verifikujemo da rezultat ima per-peer entry + sazet summary."""
    alice = _load_agent_module(alice_config)
    res = await alice.clade_broadcast(
        content={"text": "stand-up za 10 min"},
        to=["bob"],
    )
    assert "error" not in res
    assert res["sent"] == 1
    assert res["total"] == 1
    assert res["failed"] == 0
    assert "bob" in res["results"]
    assert res["results"]["bob"].get("ok") is True
    assert "1/1 ok" in res["summary"]


@pytest.mark.asyncio
async def test_clade_broadcast_to_team_resolves_members(
    relay_process, alice_config, bob_config,
):
    """Broadcast sa to_team — Config.resolve_team mora razresiti
    'engineering' u listu peer-ova iz cfg.teams."""
    alice = _load_agent_module(alice_config)
    # Dodaj team u runtime config (alice yaml nema teams po defaultu u testu)
    alice.cfg.teams = {"engineering": ["bob"]}
    res = await alice.clade_broadcast(
        content="all-hands",
        to_team="engineering",
    )
    assert "error" not in res
    assert res["team"] == "engineering"
    assert res["sent"] == 1
    assert "bob" in res["results"]


@pytest.mark.asyncio
async def test_clade_broadcast_rejects_both_to_and_to_team(alice_config):
    """Validacija: tacno jedno od to/to_team mora biti dato."""
    alice = _load_agent_module(alice_config)
    res = await alice.clade_broadcast(content="x", to=["bob"], to_team="eng")
    assert "error" in res
    res2 = await alice.clade_broadcast(content="x")
    assert "error" in res2


@pytest.mark.asyncio
async def test_clade_broadcast_unknown_team(alice_config):
    """to_team koji nije u cfg.teams → humani error sa listom dostupnih."""
    alice = _load_agent_module(alice_config)
    alice.cfg.teams = {"engineering": ["bob"]}
    res = await alice.clade_broadcast(content="x", to_team="marketing")
    assert "error" in res
    assert "marketing" in res["error"]


@pytest.mark.asyncio
async def test_clade_task_lifecycle_delegator_side(
    relay_process, alice_config, bob_config,
):
    """clade_task: vraca task_id odmah, INSERT u local DB sa direction=delegated.
    clade_task_status to pokaze."""
    alice = _load_agent_module(alice_config)
    res = await alice.clade_task(to="bob", brief="refaktor login flow")
    assert res.get("ok") is True
    assert "task_id" in res
    assert res["status"] == "pending"
    task_id = res["task_id"]

    status = await alice.clade_task_status(task_id)
    assert status.get("ok") is True
    assert status["task"]["task_id"] == task_id
    assert status["task"]["direction"] == "delegated"
    assert status["task"]["peer"] == "bob"
    assert status["task"]["status"] == "pending"
    assert status["task"]["brief"] == "refaktor login flow"


@pytest.mark.asyncio
async def test_clade_task_list_filters(
    relay_process, alice_config, bob_config,
):
    """clade_task_list sa filter='sent' vraca samo delegirane; status='pending' filtrira dalje."""
    alice = _load_agent_module(alice_config)
    # Cisti tasks tabelu pre testa (prethodni testovi su mozda upisali)
    alice._audit_conn.execute("DELETE FROM tasks")
    alice._audit_conn.commit()

    await alice.clade_task(to="bob", brief="zadatak 1")
    await alice.clade_task(to="bob", brief="zadatak 2")

    lst = await alice.clade_task_list(filter="sent")
    assert lst.get("ok") is True
    assert lst["count"] == 2
    assert lst["by_status"]["pending"] == 2

    received = await alice.clade_task_list(filter="received")
    assert received["count"] == 0  # alice je sve poslala, ne primila


@pytest.mark.asyncio
async def test_handle_inbound_task_message_records_assignment(
    relay_process, bob_config,
):
    """Kad bob primi poruku sa _task flag, handle_inbound_task_message
    INSERT-uje u tasks tabelu sa direction=assigned."""
    bob = _load_agent_module(bob_config)
    bob._audit_conn.execute("DELETE FROM tasks")
    bob._audit_conn.commit()

    fake_env = {
        "msg_id": "test-msg-id",
        "from_agent": "alice",
        "to_agent": "bob",
        "kind": "send",
        "payload": {
            "_task": True,
            "_task_id": "task-abc-123",
            "brief": "napravi prezentaciju",
            "text": "[TASK delegiran] napravi prezentaciju",
        },
    }
    handled = bob.handle_inbound_task_message(fake_env)
    assert handled is True

    status = await bob.clade_task_status("task-abc-123")
    assert status.get("ok") is True
    assert status["task"]["direction"] == "assigned"
    assert status["task"]["peer"] == "alice"
    assert status["task"]["status"] == "pending"
    assert status["task"]["brief"] == "napravi prezentaciju"


@pytest.mark.asyncio
async def test_clade_task_update_only_for_assigned(
    relay_process, alice_config, bob_config,
):
    """clade_task_update odbija ako task nije meni dodeljen (direction=delegated)."""
    alice = _load_agent_module(alice_config)
    res = await alice.clade_task(to="bob", brief="x")
    tid = res["task_id"]
    # Alice probae da update-uje sopstveni delegated task — to nije njoj dodeljeno
    upd = await alice.clade_task_update(tid, "in_progress")
    assert "error" in upd
    assert "delegated" in upd["error"]


def test_config_teams_resolve(tmp_path):
    """Config.resolve_team filtrira nepostojece member-e i vraca samo validne."""
    import sys as _sys
    _sys.modules.pop("agent.main", None)
    cfg_path = tmp_path / "with_teams.yaml"
    cfg_path.write_text(
        "my_id: ceo\n"
        "bearer_token: x\n"
        "peers:\n"
        "  alice: aaaa\n"
        "  bob: bbbb\n"
        "teams:\n"
        "  engineering: [alice, bob, nonexistent]\n"
        "  empty_team: [nobody_real]\n"
        f"audit_db: {tmp_path}/x.db\n"
    )
    os.environ["CLADE_CONFIG"] = str(cfg_path)
    import agent.main as main  # noqa: PLC0415
    eng = main.cfg.resolve_team("engineering")
    assert eng == ["alice", "bob"]  # nonexistent silently dropped
    empty = main.cfg.resolve_team("empty_team")
    assert empty == []
    missing = main.cfg.resolve_team("marketing")
    assert missing is None

"""PR#1 testovi za clade.transport.* (UnixSocketTransport + HttpRemoteTransport)."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from clade.envelope import Envelope
from clade.transport import HttpRemoteTransport, UnixSocketTransport
from clade.transport.types import Response


# ---- UnixSocketTransport ----


@pytest.mark.asyncio
async def test_unix_socket_ask_roundtrip(tmp_path: Path):
    """Server prima ask, vraca reply envelope; client (drugi instance) ekstrahuje."""
    sock_path = tmp_path / "test.sock"
    server = UnixSocketTransport(socket_path=sock_path)
    received: list[Envelope] = []

    async def handler(env: Envelope) -> Response:
        received.append(env)
        if env.kind == "ask":
            reply = Envelope.new(
                from_agent=env.to_agent,
                to_agent=env.from_agent,
                kind="reply",
                payload={"answer": "42"},
                correlation_id=env.correlation_id,
                thread_id=env.thread_id,
            )
            return Response(envelope=reply)
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    # Daj serve-u trenutak da bind-uje socket
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.02)

    try:
        client = UnixSocketTransport()
        ask = Envelope.new(
            from_agent="alice", to_agent="bob", kind="ask",
            payload={"question": "Koliko?"}, correlation_id="corr-1",
            thread_id="t-1",
        )
        res = await client.deliver(ask, to_url=f"unix://{sock_path}")

        assert res.ok is True
        assert res.error is None
        assert res.response is not None
        assert res.response.kind == "reply"
        assert res.response.payload == {"answer": "42"}
        assert res.response.correlation_id == "corr-1"
        assert res.response.thread_id == "t-1"
        # Server side primio ispravan envelope
        assert len(received) == 1
        assert received[0].from_agent == "alice"
        assert received[0].payload == {"question": "Koliko?"}
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_unix_socket_send_no_response(tmp_path: Path):
    """Fire-and-forget send: handler vraca Response(envelope=None), client ok=True bez response-a."""
    sock_path = tmp_path / "test2.sock"
    server = UnixSocketTransport(socket_path=sock_path)

    async def handler(env: Envelope) -> Response:
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.02)

    try:
        client = UnixSocketTransport()
        send = Envelope.new(from_agent="alice", to_agent="bob", kind="send",
                            payload={"text": "hi"})
        res = await client.deliver(send, to_url=f"unix://{sock_path}")
        assert res.ok is True
        assert res.response is None
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_unix_socket_deliver_to_nonexistent_socket():
    """Pokusaj konekcije na nepostojeci socket → ok=False, smisleni error."""
    client = UnixSocketTransport()
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
    res = await client.deliver(env, to_url="unix:///tmp/nonexistent-clade-12345.sock")
    assert res.ok is False
    assert res.error is not None
    assert "FileNotFoundError" in res.error or "ConnectionRefusedError" in res.error


@pytest.mark.asyncio
async def test_unix_socket_invalid_url_scheme():
    """to_url bez unix:// prefix-a → smisleni error, ne crash."""
    client = UnixSocketTransport()
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send")
    res = await client.deliver(env, to_url="http://127.0.0.1:1")
    assert res.ok is False
    assert "invalid to_url" in res.error  # type: ignore[operator]


@pytest.mark.asyncio
async def test_unix_socket_server_rejects_invalid_envelope(tmp_path: Path):
    """Server prima garbage bytes → vraca error envelope, ne crash."""
    sock_path = tmp_path / "test3.sock"
    server = UnixSocketTransport(socket_path=sock_path)

    handler_called = False

    async def handler(env: Envelope) -> Response:
        nonlocal handler_called
        handler_called = True
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.02)

    try:
        # Ruko-otvorimo konekciju i poslemo nevazece bajtove (JSON ali ne Envelope)
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        from clade.transport.framing import read_frame, write_frame
        await write_frame(writer, b'{"not": "an envelope"}')
        resp_bytes = await asyncio.wait_for(read_frame(reader), timeout=3)
        resp_data = json.loads(resp_bytes)
        # Server treba da vrati error envelope sa _error u payload-u
        assert resp_data["kind"] == "reply"
        assert "_error" in resp_data["payload"]
        writer.close()
        await writer.wait_closed()
        assert handler_called is False
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_unix_socket_stop_cleans_up_socket_file(tmp_path: Path):
    """stop() brise socket fajl da sledeci start ne pukne."""
    sock_path = tmp_path / "test4.sock"
    server = UnixSocketTransport(socket_path=sock_path)

    async def handler(env: Envelope) -> Response:
        return Response(envelope=None)

    serve_task = asyncio.create_task(server.serve(handler))
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.02)
    assert sock_path.exists()

    await server.stop()
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass

    assert not sock_path.exists()


# ---- HttpRemoteTransport ----


@pytest.mark.asyncio
async def test_http_remote_serve_raises_not_implemented():
    """HttpRemoteTransport.serve() nije pattern za polling — eksplicitno raise."""
    t = HttpRemoteTransport(relay_url="http://x", bearer_token="x")

    async def handler(env: Envelope) -> Response:
        return Response()

    with pytest.raises(NotImplementedError) as exc_info:
        await t.serve(handler)
    assert "polling" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_http_remote_deliver_invalid_kind():
    """Envelope sa nedozvoljenim kind-om → ok=False sa smislenom porukom.
    NAPOMENA: u praksi Envelope pydantic vec validira kind kao Literal,
    pa stizemo tu samo kroz model_copy bypass — pokrivamo defense in depth."""
    t = HttpRemoteTransport(relay_url="http://127.0.0.1:1", bearer_token="x")
    env = Envelope.new(from_agent="a", to_agent="b", kind="send")
    # Bypass-uj pydantic constraint da test pogodi nas guard
    env_dict = env.model_dump()
    env_dict["kind"] = "evil"
    # Nismo u stanju da napravimo evil-kind Envelope objekat zbog Literal-a,
    # pa moramo monkey-patch-ovati posle konstrukcije:
    object.__setattr__(env, "kind", "evil")  # type: ignore[assignment]
    res = await t.deliver(env, to_url="")
    assert res.ok is False
    assert "invalid envelope.kind" in res.error  # type: ignore[operator]


@pytest.mark.asyncio
async def test_http_remote_deliver_connection_refused():
    """Relay nedostupan → ok=False sa ConnectError-om u error-u."""
    t = HttpRemoteTransport(relay_url="http://127.0.0.1:1", bearer_token="x")
    env = Envelope.new(from_agent="alice", to_agent="bob", kind="send",
                       payload={"text": "test"})
    res = await t.deliver(env, to_url="")
    assert res.ok is False
    assert res.error is not None
    assert "ConnectError" in res.error or "Connection" in res.error


@pytest.mark.asyncio
async def test_http_remote_stop_is_noop():
    """HttpRemoteTransport je stateless — stop() ne crash-uje, ponavlja se OK."""
    t = HttpRemoteTransport(relay_url="http://x", bearer_token="x")
    await t.stop()
    await t.stop()  # idempotent

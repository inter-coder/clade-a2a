"""UnixSocketTransport — primarni on-host peer-to-peer transport.

Procesi razgovaraju preko unix socket fajla u `/run/user/<uid>/clade/<peer>.sock`
(ili tempdir-u za testove). Mode 0600 — samo isti UID konektuje. HMAC je
opcioni jer kernel filesystem permission-i ionako sprecavaju vanjski pristup.

API:
    t = UnixSocketTransport("/run/user/1000/clade/dusan.sock")
    await t.serve(handler)        # blokira do stop()
    await t.stop()
    # ili sa druge strane (sender):
    t2 = UnixSocketTransport()    # ne treba sopstvena adresa za send-only
    res = await t2.deliver(env, to_url="unix:///run/user/1000/clade/dusan.sock")
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from clade.envelope import Envelope
from clade.transport.framing import FrameTooLarge, read_frame, write_frame
from clade.transport.types import DeliveryResult, Handler, Response

DELIVER_TIMEOUT_S = 130.0  # gornji limit za ask roundtrip (peer cgrupisa do 120s)


class UnixSocketTransport:
    """On-host peer-to-peer transport. Server mode + client mode preko iste klase."""

    def __init__(self, socket_path: str | os.PathLike | None = None) -> None:
        """`socket_path` je adresa NA KOJOJ slušamo (server mode). Za client-only
        koriscenje (samo deliver()), prosledjuje se None — to_url se zadaje per-call."""
        self.socket_path: Path | None = Path(socket_path) if socket_path else None
        self._server: asyncio.Server | None = None

    # ---- Server mode ----

    async def serve(self, handler: Handler) -> None:
        if self.socket_path is None:
            raise RuntimeError("UnixSocketTransport.serve() bez socket_path-a — konstruisi sa adresom.")

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Cleanup stari socket fajl ako je ostao posle crash-a
        if self.socket_path.exists() or self.socket_path.is_socket():
            with suppress(OSError):
                self.socket_path.unlink()

        async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                raw = await read_frame(reader)
                try:
                    env = Envelope.from_json_bytes(raw)
                except Exception as e:  # ValidationError ili JSON parse error
                    err = Envelope.new(
                        from_agent="-", to_agent="-", kind="reply",
                        payload={"_error": f"invalid envelope: {type(e).__name__}: {str(e)[:200]}"},
                    )
                    await write_frame(writer, err.to_json_bytes())
                    return

                resp = await handler(env)
                if resp.envelope is not None:
                    await write_frame(writer, resp.envelope.to_json_bytes())
                # Ako handler vrati Response(envelope=None) (npr. za fire-and-forget
                # send), ne saljemo nista — peer-ov reader ce dobiti EOF
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                pass  # peer zatvorio
            except FrameTooLarge:
                pass  # napad ili bug; zatvori bez odgovora
            finally:
                with suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

        self._server = await asyncio.start_unix_server(_on_connect, path=str(self.socket_path))
        # 0600 = samo isti UID
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self.socket_path is not None and self.socket_path.exists():
            with suppress(OSError):
                self.socket_path.unlink()

    # ---- Client mode ----

    async def deliver(self, envelope: Envelope, to_url: str) -> DeliveryResult:
        """`to_url` formata `unix:///path/to/peer.sock`."""
        if not to_url.startswith("unix://"):
            return DeliveryResult(ok=False, error=f"UnixSocketTransport: invalid to_url '{to_url}' (expected unix:///...)")
        sock_path = to_url[len("unix://"):]

        try:
            reader, writer = await asyncio.open_unix_connection(sock_path)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as e:
            return DeliveryResult(ok=False, error=f"{type(e).__name__}: {e}")

        try:
            await write_frame(writer, envelope.to_json_bytes())
            # Za ask kind cekamo reply; za send/reply preferiramo da ne blokiramo
            # zauvek — server moze poslati prazan EOF. Saznacemo iz read_frame:
            # - reply envelope → vratimo response
            # - IncompleteReadError → server fire-and-forget, sve OK
            try:
                resp_bytes = await asyncio.wait_for(read_frame(reader), timeout=DELIVER_TIMEOUT_S)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                return DeliveryResult(ok=True, response=None)
            except asyncio.TimeoutError:
                return DeliveryResult(ok=False, error=f"deliver timeout posle {DELIVER_TIMEOUT_S}s")

            try:
                resp_env = Envelope.from_json_bytes(resp_bytes)
            except Exception as e:
                return DeliveryResult(ok=False, error=f"invalid response envelope: {e}")
            return DeliveryResult(ok=True, response=resp_env)
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

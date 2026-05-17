"""Transport interface + result tipovi.

Razdvojeno od konkretnih implementacija da izbegnemo cirkularne import-e
(clade/transport/__init__.py importuje obe implementacije + tipove)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from clade.envelope import Envelope


@dataclass(frozen=True)
class DeliveryResult:
    """Rezultat `Transport.deliver()`. Za `kind=ask`, `response` je peer-ov
    reply envelope (ako je deliver radio i peer je odgovorio). Za send/reply
    response je None ali ok=True znaci da je peer prihvatio."""

    ok: bool
    error: str | None = None
    response: Envelope | None = None


@dataclass(frozen=True)
class Response:
    """Sta handler u `Transport.serve()` vraca posle obrade envelope-a.
    `envelope` je opcioni reply (za ask kind-ove); za send se obicno vraca None."""

    envelope: Envelope | None = None
    error: str | None = None


Handler = Callable[[Envelope], Awaitable[Response]]


class Transport(Protocol):
    """Apstraktni transport peer↔peer kanal.

    Asimetricna API:
    - `deliver(env, to_url)` — outbound, vrati DeliveryResult
    - `serve(handler)` — inbound, blokira do `stop()` (drzi loop unutar sebe)
    - `stop()` — graceful shutdown, cleanup resurse (socket fajl itd.)

    Implementacije moraju biti idempotent — `stop()` posle `stop()` ne sme
    da baci. `serve()` se zove jednom; ako treba refresh, najpre `stop()`."""

    async def deliver(self, envelope: Envelope, to_url: str) -> DeliveryResult: ...

    async def serve(self, handler: Handler) -> None: ...

    async def stop(self) -> None: ...

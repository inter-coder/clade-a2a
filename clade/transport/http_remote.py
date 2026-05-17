"""HttpRemoteTransport — cross-host peer-to-peer kroz relay.

Sluzi samo za `--remote` scenarije (peer-ovi na razlicitim mrezama, bez VPN-a).
Salje HTTP POST na relay (`/send`, `/ask`, `/reply`) kao v1.x agent. Razlika:
- Koristi clade.envelope.Envelope (sa thread_id/reply_to TOP-LEVEL — relay i
  dalje treba da ih razume kao opciona polja u envelope-u; samo ih forward-uje).
- Ne zna nista o HMAC computation — sender mora popuniti `envelope.hmac` pre
  poziva `deliver()` (jer secret zna samo sender + peer).

**`serve()` nije podrzan**: cross-host receive ide kroz polling relay /inbox-a,
sto nije Transport.serve() pattern. To se resava u PR#2 kad integrisemo `clade
serve` (poseban polling loop za peer-ove sa transport=relay)."""

from __future__ import annotations

import httpx

from clade.envelope import Envelope
from clade.transport.types import DeliveryResult, Handler

DELIVER_TIMEOUT_S = 130.0
DEFAULT_ASK_TIMEOUT_S = 90  # v1.1.0+ sync sa relay AskBody default-om


class HttpRemoteTransport:
    """Relay-mediated cross-host transport. Wraps relay HTTP API."""

    def __init__(self, relay_url: str, bearer_token: str,
                 ask_timeout_s: int = DEFAULT_ASK_TIMEOUT_S) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.bearer_token = bearer_token
        self.ask_timeout_s = ask_timeout_s

    async def deliver(self, envelope: Envelope, to_url: str = "") -> DeliveryResult:
        """`to_url` ignorisan — relay zna gde da forward-uje po envelope.to_agent.
        Argument ostaje radi konformiteta sa Transport.deliver() potpisom."""
        del to_url  # not used; relay routes po envelope.to_agent

        endpoint = {"send": "/send", "ask": "/ask", "reply": "/reply"}.get(envelope.kind)
        if endpoint is None:
            return DeliveryResult(ok=False, error=f"invalid envelope.kind '{envelope.kind}'")

        env_dict = envelope.model_dump()
        body: dict = (
            {"env": env_dict, "timeout_s": self.ask_timeout_s}
            if envelope.kind == "ask"
            else env_dict
        )
        headers = {"Authorization": f"Bearer {self.bearer_token}"}

        try:
            async with httpx.AsyncClient(timeout=DELIVER_TIMEOUT_S) as c:
                r = await c.post(f"{self.relay_url}{endpoint}", json=body, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return DeliveryResult(ok=False, error=f"{type(e).__name__}: {e}")

        if r.status_code != 200:
            return DeliveryResult(ok=False, error=f"HTTP {r.status_code}: {r.text[:200]}")

        # Ask: relay vraca {response: <reply_envelope_dict>, ...}; ekstrahujemo
        if envelope.kind == "ask":
            data = r.json()
            reply_raw = data.get("response")
            if reply_raw is None:
                return DeliveryResult(ok=False, error="ask response missing 'response' field")
            try:
                # Relay vraca v1.x format envelope-a — moze nemati protocol_version polje;
                # pydantic default = "2.0.0" pa to nije problem za nase pretpostavke.
                # Ali ako relay vrati legacy polja koja Envelope (extra=forbid) ne zna,
                # bi pukao. Za PR#1 koristimo strict; PR#5/6 dodaju strict version handshake.
                reply_env = Envelope.model_validate(reply_raw)
            except Exception as e:
                return DeliveryResult(ok=False, error=f"invalid reply envelope from relay: {e}")
            return DeliveryResult(ok=True, response=reply_env)

        # Send / reply: relay vraca samo {ok, msg_id}
        return DeliveryResult(ok=True)

    async def serve(self, handler: Handler) -> None:
        raise NotImplementedError(
            "HttpRemoteTransport.serve() nije podrzan — cross-host receive ide "
            "kroz polling relay /inbox-a. To je odvojeni 'inbox_poll_loop' u "
            "clade serve procesu (PR#2), ne Transport.serve() pattern."
        )

    async def stop(self) -> None:
        # Stateless transport — nista za cleanup
        pass

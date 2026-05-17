"""Transport apstrakcija za v2.0 protokol.

Dve konkretne implementacije:
- UnixSocketTransport — primarni, on-host peer-to-peer (single host, dva ili
  vise clade-<peer> procesa razgovaraju preko `/run/user/<uid>/clade/*.sock`).
- HttpRemoteTransport — relay-mediated, cross-host (`--remote` scenariji).

Transport interface ne zna nista o MCP klijentu (Claude Code) — to je odvojeno;
clade serve proces ce expose-ovati i HTTP `127.0.0.1:port` MCP endpoint pored
ovog Transport-a (vidi PR#2). Transport je STRIKT peer-to-peer.

**Namerno NEMA factory/registry/plugin sloja** (samozapazanja §2.2): dve
konkretne klase, izabere se na startup-u iz peers.yaml (transport: unix|relay).
FastAPI/Starlette su preteranje; goli asyncio + json je dovoljan."""

from clade.transport.types import DeliveryResult, Response, Transport
from clade.transport.unix import UnixSocketTransport
from clade.transport.http_remote import HttpRemoteTransport

__all__ = [
    "Transport",
    "DeliveryResult",
    "Response",
    "UnixSocketTransport",
    "HttpRemoteTransport",
]

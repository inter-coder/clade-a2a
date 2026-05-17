"""peers.yaml — v2 schema iz samozapazanja §2.4.

Jedan fajl po masini: `~/.config/clade/peers.yaml`. Sve ostalo se izvodi iz njega.

Pun primer:

```yaml
version: 2                     # protocol/schema verzija (strict major match)
self: katana                   # koji peer si TI na ovoj masini
peers:
  katana:
    transport: unix
    socket: /run/user/1000/clade/katana.sock
    http_port: 9001
    audit_db: ~/.local/state/clade/katana/audit.db
    workdir: ~/clade-projects/test/workdirs/katana
    role: interactive          # interactive | headless | both
  dusan:
    transport: unix
    socket: /run/user/1000/clade/dusan.sock
    role: headless
  remote_peer:
    transport: relay
    relay_url: https://relay.example.com
    secret_hex: ${env:CLADE_REMOTE_SECRET}   # iz env, ne u fajlu
    role: headless
```

**Secret substitution:** `${env:VAR_NAME}` u secret_hex polju se zamenjuje
sa vrednoscu env varijable na load-u. Ne prilagodjava ostala polja — ne
zelimo da peers.yaml postane shell jezik."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

SUPPORTED_SCHEMA_VERSION = 2

Transport = Literal["unix", "relay"]
Role = Literal["interactive", "headless", "both"]


class PeerEntry(BaseModel):
    """Konfiguracija jednog peer-a (sebe ili drugog).

    Validacija je conditional na `transport`:
    - `transport=unix`: socket je preporucljiv (ako fali, fallback na konvenciju)
    - `transport=relay`: relay_url i secret_hex su obavezni
    """

    model_config = ConfigDict(extra="forbid")

    transport: Transport = "unix"
    role: Role = "headless"

    # Unix transport polja
    socket: str | None = None
    http_port: int | None = None
    audit_db: str | None = None
    workdir: str | None = None

    # Relay transport polja
    relay_url: str | None = None
    secret_hex: str | None = None     # podrzava ${env:VAR} substituciju
    bearer_token: str | None = None   # za relay auth

    @field_validator("socket", "audit_db", "workdir", mode="before")
    @classmethod
    def _expand_tilde(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return str(Path(str(v)).expanduser())

    @field_validator("secret_hex", "bearer_token", mode="before")
    @classmethod
    def _expand_env(cls, v: str | None) -> str | None:
        """${env:VAR_NAME} → os.environ['VAR_NAME']. Ako env nije set, ostavlja
        literalan string da downstream layer moze da reaguje (ili pukne)."""
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        match = re.fullmatch(r"\$\{env:([A-Z_][A-Z0-9_]*)\}", v.strip())
        if match:
            env_name = match.group(1)
            return os.environ.get(env_name, v)  # fallback na literal ako env nije set
        return v

    @model_validator(mode="after")
    def _conditional_required(self) -> "PeerEntry":
        # transport=relay zahteva relay_url uvek. secret_hex i bearer_token
        # su asimetricni — self entry ima bearer_token (auth ka relay-u),
        # other peer entries imaju secret_hex (pairwise HMAC). Validacija
        # asimetrije ide na use-site (Server / mcp_server) gde znamo kontekst.
        if self.transport == "relay" and not self.relay_url:
            raise ValueError("transport=relay zahteva relay_url")
        return self


class PeersConfig(BaseModel):
    """Top-level peers.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    version: int = 2
    self: str
    peers: dict[str, PeerEntry]

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int, info: ValidationInfo) -> int:
        if v != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"peers.yaml version={v} not supported; "
                f"this clade build expects version={SUPPORTED_SCHEMA_VERSION}"
            )
        return v

    def me(self) -> PeerEntry:
        """Vrati moju entry — convenience."""
        if self.self not in self.peers:
            raise ValueError(f"self='{self.self}' nije u peers dict-u: {list(self.peers)}")
        return self.peers[self.self]

    def other(self, name: str) -> PeerEntry:
        """Vrati peer entry. Baca ValueError ako nije u allowlist-u."""
        if name not in self.peers:
            raise ValueError(f"unknown peer '{name}', dozvoljeni: {list(self.peers)}")
        if name == self.self:
            raise ValueError(f"'{name}' is self, ne moze biti drugi peer")
        return self.peers[name]

    def interactive_peers(self) -> dict[str, PeerEntry]:
        """Peer-ovi sa role interactive ili both — oni dobijaju .mcp.json."""
        return {n: p for n, p in self.peers.items() if p.role in ("interactive", "both")}


def load(path: str | Path) -> PeersConfig:
    """Ucitaj peers.yaml. Baca FileNotFoundError ako ne postoji.

    Relativni path-ovi u peer entries (socket, audit_db, workdir) se resolves
    protiv peers.yaml dir-a (kao docker-compose volume mapping). Time wizard
    moze generisati portable bundle gde su path-ovi `./workdirs/<peer>` —
    radi bez obzira gde scp-ujes bundle."""
    p = Path(path).expanduser().resolve()
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and "version" not in data:
        data["version"] = SUPPORTED_SCHEMA_VERSION

    # Resolve relativne path-ove protiv peers.yaml lokacije
    parent_dir = p.parent
    for peer_name, peer_data in (data.get("peers") or {}).items():
        if not isinstance(peer_data, dict):
            continue
        for key in ("socket", "audit_db", "workdir"):
            v = peer_data.get(key)
            if v and isinstance(v, str) and not v.startswith(("/", "~")):
                peer_data[key] = str((parent_dir / v).resolve())

    return PeersConfig(**data)

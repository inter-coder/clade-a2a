"""Minimalan peers.yaml loader za PR#2. Full schema iz §2.4 dolazi u PR#4.

Trenutno podrzavamo samo ono sto `clade serve` treba:
- `self`: koji peer si TI na ovoj masini
- `peers[name].socket`: unix socket path za peer-to-peer (lokalan)
- `peers[name].http_port`: 127.0.0.1 port za MCP klijent / health
- `peers[name].audit_db`: SQLite audit DB path
- `peers[name].workdir`: (opciono) workdir za daemon-spawn Claude proces

Tilde-expand na load-u (`~/...` → `/home/user/...`). Pydantic validacija sa
strict mode (extra='forbid') — nepoznata polja → ValidationError, ne tihi drop."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class PeerEntry(BaseModel):
    """Konfiguracija jednog peer-a (sebe ili drugog)."""

    model_config = ConfigDict(extra="forbid")

    socket: str | None = None         # unix:///... (samo ako transport=unix, default)
    http_port: int | None = None      # 127.0.0.1:<port> za MCP klijent
    audit_db: str | None = None       # SQLite path (samo za self)
    workdir: str | None = None        # za spawn `claude --print`, opciono

    @field_validator("socket", "audit_db", "workdir", mode="before")
    @classmethod
    def _expand_tilde(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return str(Path(str(v)).expanduser())


class PeersConfig(BaseModel):
    """Top-level peers.yaml schema (PR#2 minimal)."""

    model_config = ConfigDict(extra="forbid")

    self: str
    peers: dict[str, PeerEntry]

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


def load(path: str | Path) -> PeersConfig:
    """Ucitaj peers.yaml. Baca FileNotFoundError ako ne postoji."""
    p = Path(path).expanduser()
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    return PeersConfig(**data)

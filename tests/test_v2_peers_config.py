"""PR#2 testovi za clade.peers_config."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clade.peers_config import PeerEntry, PeersConfig, load


def test_load_basic(tmp_path: Path):
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "self": "katana",
        "peers": {
            "katana": {
                "socket": "/run/user/1000/clade/katana.sock",
                "http_port": 9001,
                "audit_db": "~/.local/state/clade/katana/audit.db",
            },
            "dusan": {
                "socket": "/run/user/1000/clade/dusan.sock",
                "http_port": 9002,
            },
        },
    }))
    cfg = load(cfg_path)
    assert cfg.self == "katana"
    assert set(cfg.peers) == {"katana", "dusan"}
    me = cfg.me()
    assert me.http_port == 9001
    # Tilde expanded
    assert "~" not in me.audit_db  # type: ignore[operator]


def test_extra_field_rejected(tmp_path: Path):
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "self": "katana",
        "peers": {"katana": {"socket": "/tmp/x.sock", "evil_field": 1}},
    }))
    with pytest.raises(ValidationError):
        load(cfg_path)


def test_me_raises_when_self_not_in_peers(tmp_path: Path):
    cfg_path = tmp_path / "peers.yaml"
    cfg_path.write_text(yaml.dump({
        "self": "ghost",
        "peers": {"katana": {"socket": "/tmp/x.sock"}},
    }))
    cfg = load(cfg_path)
    with pytest.raises(ValueError, match="nije u peers"):
        cfg.me()


def test_other_validates_allowlist():
    cfg = PeersConfig(
        self="katana",
        peers={
            "katana": PeerEntry(socket="/tmp/k.sock"),
            "dusan": PeerEntry(socket="/tmp/d.sock"),
        },
    )
    assert cfg.other("dusan").socket == "/tmp/d.sock"
    with pytest.raises(ValueError, match="unknown peer"):
        cfg.other("predrag")
    with pytest.raises(ValueError, match="is self"):
        cfg.other("katana")

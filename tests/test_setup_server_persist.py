"""v1.4.2 — proveri da _save_setup + _load_setups cuvaju bearer-e/yaml-ove
preko restart-a setup-server-a. Razlog: bug iz v1.4.1 gde se token mismatch
desavao kad relay subprocess prezivi a setup-server izgubi in-memory state."""
from __future__ import annotations

import json
from pathlib import Path

from clade_cli.setup_server import (
    PeerInput,
    SetupForm,
    _generate_setup,
    _load_setups,
    _save_setup,
)


def _make_form() -> SetupForm:
    return SetupForm(
        project_name="persist-test",
        relay_host="0.0.0.0",
        relay_url_host="192.168.1.91",
        relay_port=7777,
        start_relay=True,
        peers=[
            PeerInput(peer_id="frontend", display_name="Front", role="UI dev"),
            PeerInput(peer_id="backend", display_name="Back", role="DB dev"),
        ],
    )


def test_save_then_load_round_trip(tmp_path: Path):
    """Generisi setup, save-uj, simuliraj restart load-om — bearer-i i yaml
    moraju biti identicni (peer install ostaje validni)."""
    form = _make_form()
    original = _generate_setup(form, tmp_path)
    _save_setup(original, tmp_path)

    loaded = _load_setups(tmp_path)
    assert original.project_token in loaded
    s = loaded[original.project_token]

    assert s.project_name == original.project_name
    assert s.relay_url == original.relay_url
    assert s.relay_port == original.relay_port
    assert s.tokens_path == original.tokens_path
    assert set(s.peers.keys()) == set(original.peers.keys())

    for peer_id, orig_peer in original.peers.items():
        loaded_peer = s.peers[peer_id]
        # KLJUCNO: bearer i download_token MORAJU biti isti — od ovoga zavisi
        # da li peer install URL i daemon poll prolaze auth posle restart-a.
        assert loaded_peer.bearer_token == orig_peer.bearer_token, \
            f"bearer_token differs for {peer_id}"
        assert loaded_peer.download_token == orig_peer.download_token, \
            f"download_token differs for {peer_id}"
        assert loaded_peer.yaml_content == orig_peer.yaml_content
        # download_token_to_peer mora biti rehidriran
        assert s.download_token_to_peer[orig_peer.download_token] == peer_id


def test_save_setup_file_layout(tmp_path: Path):
    """setup.json pisan atomically, mode 0600, predvidiv layout."""
    form = _make_form()
    setup = _generate_setup(form, tmp_path)
    _save_setup(setup, tmp_path)

    state_file = tmp_path / setup.project_token / "setup.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["version"] == 1
    assert data["project_token"] == setup.project_token
    assert len(data["peers"]) == 2
    # Tmp file ne sme ostati posle rename-a
    assert not state_file.with_suffix(".json.tmp").exists()
    # Sigurnost: tokens su senzitivni, hocemo 0600
    assert oct(state_file.stat().st_mode)[-3:] == "600"


def test_load_setups_skips_corrupt_or_unknown_version(tmp_path: Path):
    """Ne treba da pukne ako neko podigne stari/cudan setup.json — log warning."""
    p1 = tmp_path / "bad-version"
    p1.mkdir()
    (p1 / "setup.json").write_text(json.dumps({"version": 999}))

    p2 = tmp_path / "corrupt"
    p2.mkdir()
    (p2 / "setup.json").write_text("not json at all{{")

    p3 = tmp_path / "no-setup-file"
    p3.mkdir()  # nista unutra

    loaded = _load_setups(tmp_path)
    assert loaded == {}


def test_load_setups_empty_data_dir(tmp_path: Path):
    """Prvi start (data_dir prazan ili ne postoji) ne sme da pukne."""
    nonexistent = tmp_path / "nope"
    assert _load_setups(nonexistent) == {}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _load_setups(empty) == {}

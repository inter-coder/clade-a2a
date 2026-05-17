"""`clade init` — bootstrap helper.

Generise:
1. `~/.config/clade/peers.yaml` (ako ne postoji)
2. `~/.config/systemd/user/clade-<peer>.service`
3. `<peer.workdir>/.mcp.json` za peer-ove sa role=interactive

Dry-run default — bez `--apply` samo prikaze sta bi generisao. Sa `--apply`
upisuje fajlove. Idempotent: re-run ne kvari postojece resurse.

Po samozapazanja §2.4."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from clade.peers_config import PeersConfig, load
from clade.templates import (
    default_paths,
    render_default_peers_yaml,
    render_mcp_json,
    render_systemd_unit,
)

LOG = logging.getLogger("clade.init")


@dataclass
class InitPlan:
    """Sta init bi uradio. Vraca se i prikaze pre nego sto se primeni."""
    files_to_create: list[tuple[Path, str]] = field(default_factory=list)
    files_to_skip: list[Path] = field(default_factory=list)
    dirs_to_create: list[Path] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.files_to_create or self.dirs_to_create)


def plan(
    self_id: str,
    peer_ids: list[str] | None = None,
    *,
    peers_yaml_path: Path | None = None,
    systemd_dir: Path | None = None,
) -> InitPlan:
    """Planiraj sta init treba da uradi — bez side effect-ova.

    `peer_ids` je opcioni: ako fali i peers.yaml jos ne postoji, vraca plan
    bez peers.yaml (korisnik mora dati listu). Ako peers.yaml POSTOJI, ucitamo
    ga i ignorisemo peer_ids.

    `self_id` MORA biti dat — ime peer-a na ovoj masini."""
    paths = default_paths()
    peers_yaml_path = peers_yaml_path or paths["peers_yaml"]
    systemd_dir = systemd_dir or paths["systemd_user_dir"]

    out = InitPlan()

    # 1) peers.yaml
    if peers_yaml_path.exists():
        out.files_to_skip.append(peers_yaml_path)
        try:
            cfg = load(peers_yaml_path)
        except Exception as e:
            raise RuntimeError(
                f"peers.yaml postoji ali ima invalid schema: {e}. Popravi ga pa rerun."
            ) from e
    else:
        if not peer_ids:
            raise ValueError(
                "peers.yaml jos ne postoji i nije dat --peer; daj listu peer-ova "
                "(npr. `clade init --self katana --peer dusan --peer predrag`) "
                "ili rucno kreiraj peers.yaml prvo"
            )
        yaml_content = render_default_peers_yaml(self_id, peer_ids)
        out.dirs_to_create.append(peers_yaml_path.parent)
        out.files_to_create.append((peers_yaml_path, yaml_content))
        # Parsiraj sopstveni generisan content za downstream logiku
        cfg = PeersConfig.model_validate({
            "version": 2,
            "self": self_id,
            "peers": _parse_peers_block(yaml_content),
        })

    if cfg.self != self_id:
        raise ValueError(
            f"peers.yaml self='{cfg.self}' ne odgovara --self '{self_id}'. "
            f"Ako menjas self, edituj peers.yaml manuelno."
        )

    me = cfg.me()

    # 2) systemd unit
    unit_path = systemd_dir / f"clade-{self_id}.service"
    if unit_path.exists():
        existing = unit_path.read_text()
        new = render_systemd_unit(self_id, peers_yaml_path)
        if existing == new:
            out.files_to_skip.append(unit_path)
        else:
            # Postojeci unit razlikuje se — ostavi staru, korisnik regulise rucno
            out.files_to_skip.append(unit_path)
            LOG.warning("systemd unit %s postoji i razlikuje se od template-a — preskocen. "
                        "Obrisi rucno za regenerate.", unit_path)
    else:
        out.dirs_to_create.append(systemd_dir)
        out.files_to_create.append(
            (unit_path, render_systemd_unit(self_id, peers_yaml_path))
        )

    # 3) .mcp.json za interactive peer-ove (najcesce samo self)
    for peer_name, peer_entry in cfg.interactive_peers().items():
        if peer_entry.http_port is None:
            LOG.warning("peer %s ima role=interactive ali fali http_port — skip .mcp.json",
                        peer_name)
            continue
        if peer_entry.workdir is None:
            LOG.warning("peer %s ima role=interactive ali fali workdir — skip .mcp.json",
                        peer_name)
            continue
        workdir = Path(peer_entry.workdir)
        mcp_path = workdir / ".mcp.json"
        new_content = render_mcp_json(peer_entry)
        if mcp_path.exists():
            if mcp_path.read_text() == new_content:
                out.files_to_skip.append(mcp_path)
            else:
                out.files_to_skip.append(mcp_path)
                LOG.warning(".mcp.json u %s razlikuje se — preskocen.", workdir)
        else:
            out.dirs_to_create.append(workdir)
            out.files_to_create.append((mcp_path, new_content))

    # Korisnicke instrukcije
    if any(p.name.startswith("clade-") and p.suffix == ".service" for p, _ in out.files_to_create):
        out.next_steps.append(
            f"systemctl --user daemon-reload && systemctl --user enable --now clade-{self_id}"
        )
        out.next_steps.append(
            f"journalctl --user -u clade-{self_id} -f   # tail log"
        )
    if me.workdir:
        out.next_steps.append(
            f"cd {me.workdir} && claude   # Claude Code ce auto-discover-ovati .mcp.json"
        )
    return out


def apply(plan_obj: InitPlan) -> int:
    """Primeni plan: kreiraj dirove i upisi fajlove. Vraca broj kreiranih fajlova."""
    for d in plan_obj.dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)
    count = 0
    for path, content in plan_obj.files_to_create:
        path.write_text(content)
        # peers.yaml + systemd unit + .mcp.json drze tajne / configs — 0600
        path.chmod(0o600)
        count += 1
    return count


def format_plan_for_display(plan_obj: InitPlan) -> str:
    """Citljiv prikaz plana — koristi se u dry-run mode-u."""
    lines = []
    if plan_obj.dirs_to_create:
        lines.append("Direktorijumi koje bi kreirao:")
        for d in dict.fromkeys(plan_obj.dirs_to_create):  # dedup, preserve order
            lines.append(f"  mkdir -p {d}")
    if plan_obj.files_to_create:
        lines.append("")
        lines.append("Fajlovi koje bi kreirao:")
        for path, content in plan_obj.files_to_create:
            lines.append(f"")
            lines.append(f"  --- {path} ({len(content)} bytes) ---")
            for content_line in content.rstrip("\n").splitlines():
                lines.append(f"  | {content_line}")
    if plan_obj.files_to_skip:
        lines.append("")
        lines.append("Fajlovi koji vec postoje (preskoceni):")
        for path in plan_obj.files_to_skip:
            lines.append(f"  {path}")
    if plan_obj.next_steps:
        lines.append("")
        lines.append("Sledeci koraci (manuelno):")
        for step in plan_obj.next_steps:
            lines.append(f"  $ {step}")
    if plan_obj.is_empty():
        lines.append("(nista za kreirati — sve vec postoji)")
    return "\n".join(lines)


# ---- Helper za render_default_peers_yaml roundtrip ----

def _parse_peers_block(yaml_content: str) -> dict:
    """Parsira generisan peers.yaml string da bi PeersConfig.model_validate
    radio downstream. Koristi PyYAML — nije inverse od render-a, samo
    deserijalizacija."""
    import yaml as _yaml  # noqa: PLC0415
    data = _yaml.safe_load(yaml_content) or {}
    return data.get("peers", {})


# ---- CLI handler (poziva se iz clade/server.py main()) ----

def cli_init(args) -> int:  # type: ignore[no-untyped-def]
    """argparse Namespace handler za `clade init` subcommand."""
    try:
        p = plan(
            self_id=args.self,
            peer_ids=args.peer or None,
            peers_yaml_path=Path(args.config).expanduser() if args.config else None,
        )
    except (ValueError, RuntimeError) as e:
        print(f"clade init: {e}", file=sys.stderr)
        return 1

    print(format_plan_for_display(p))

    if args.apply:
        if p.is_empty():
            print("\nNothing to apply.")
            return 0
        count = apply(p)
        print(f"\n✓ Applied: {count} files written (mode 0600).")
        if p.next_steps:
            print("\nSledeci koraci:")
            for step in p.next_steps:
                print(f"  $ {step}")
    else:
        print("\n(dry-run — koristi --apply da stvarno zapises fajlove)")
    return 0

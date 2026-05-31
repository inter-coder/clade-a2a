"""Phase A (v1.13.0) — AITF (AI Team Framework) project importer.

Reads a project scaffolded by https://github.com/dusankrstic-cpu/ai-team-framework
(via its 21-question wizard) and maps the 3-4 roles defined in
`.ai-team-config.yml` + `docs/TEAM/*.md` to Clade A2A peers.

Mapping:
  Project Director         -> peer 'pd'
  Development Director     -> peer 'dd'
  Development Team         -> peer 'team'
  Documentation Optimizer  -> peer 'doc'  (only if `doc_optimizer_enabled: true`)

Teams produced:
  aitf_team   = [pd, dd, team, doc?]   -- everyone (for project-wide broadcasts)
  engineering = [dd, team]             -- dev side only (DD delegating to Team)

Each peer's `extra_add_dirs` includes the absolute project path so the
daemon-spawned Claude can read AND write the document substrate that
AITF roles depend on (docs/TEAM/DIRECTIVES, REPORTS, TODO.md, DECISIONS.md,
ARCHIVE, etc.).

Role definitions from AITF's templates are inlined verbatim into each peer's
`role` yaml field at import time. To pick up later edits in the AITF project,
re-run import or use the per-peer Edit UI on the result page.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


# (peer_id, default short display name, role filename under docs/TEAM/)
AITF_ROLES_REQUIRED: list[tuple[str, str, str]] = [
    ("pd",   "Project Director",     "PROJECT_DIRECTOR.md"),
    ("dd",   "Development Director", "DEVELOPMENT_DIRECTOR.md"),
    ("team", "Development Team",     "DEVELOPMENT_TEAM.md"),
]
AITF_ROLE_DO: tuple[str, str, str] = ("doc", "Documentation Optimizer", "DOC_OPTIMIZER.md")


@dataclass
class AitfPeerSpec:
    peer_id: str
    display_name: str
    role: str
    extra_add_dirs: list[str]


@dataclass
class AitfImport:
    project_name: str
    owner_name: str
    project_path: Path
    doc_optimizer_enabled: bool
    peers: list[AitfPeerSpec]
    teams: dict[str, list[str]]


def detect_aitf_project(project_path: Path) -> tuple[bool, str]:
    """Return (is_aitf, reason). reason is a human message describing why not."""
    if not project_path.is_dir():
        return False, f"{project_path} is not a directory"
    config = project_path / ".ai-team-config.yml"
    if not config.exists():
        return False, f"{config} not found — run the AITF wizard first"
    team_dir = project_path / "docs" / "TEAM"
    if not team_dir.is_dir():
        return False, f"{team_dir} not found — wizard did not finish generation"
    return True, "ok"


def parse_aitf_project(project_path: Path) -> AitfImport:
    """Read `.ai-team-config.yml` + `docs/TEAM/*.md`, return mapped AitfImport.

    Raises FileNotFoundError if the project is not an AITF project or if any
    required role file is missing.
    """
    project_path = project_path.resolve()
    ok, reason = detect_aitf_project(project_path)
    if not ok:
        raise FileNotFoundError(reason)

    config_path = project_path / ".ai-team-config.yml"
    config = yaml.safe_load(config_path.read_text()) or {}

    project = config.get("project") or {}
    project_name = str(project.get("name") or project_path.name).strip() or project_path.name
    owner_name = str(project.get("owner_name") or "").strip()
    do_enabled = bool(config.get("doc_optimizer_enabled", False))

    team_dir = project_path / "docs" / "TEAM"
    roles = list(AITF_ROLES_REQUIRED)
    if do_enabled:
        roles.append(AITF_ROLE_DO)

    peers: list[AitfPeerSpec] = []
    abs_project = str(project_path)
    for peer_id, default_name, role_filename in roles:
        role_path = team_dir / role_filename
        if not role_path.exists():
            raise FileNotFoundError(f"AITF role file missing: {role_path}")
        role_text = role_path.read_text().strip()
        if not role_text:
            raise ValueError(f"AITF role file is empty: {role_path}")
        display = f"{default_name} — {project_name}" if project_name else default_name
        peers.append(AitfPeerSpec(
            peer_id=peer_id,
            display_name=display,
            role=role_text,
            extra_add_dirs=[abs_project],
        ))

    peer_ids = [p.peer_id for p in peers]
    teams: dict[str, list[str]] = {
        "aitf_team": list(peer_ids),
    }
    engineering_members = [pid for pid in peer_ids if pid in ("dd", "team")]
    if len(engineering_members) >= 2:
        teams["engineering"] = engineering_members

    return AitfImport(
        project_name=project_name,
        owner_name=owner_name,
        project_path=project_path,
        doc_optimizer_enabled=do_enabled,
        peers=peers,
        teams=teams,
    )

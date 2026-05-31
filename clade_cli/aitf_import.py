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


# v1.15.0 (Phase C) — role-specific scribe round prompts. Each runs on a fresh
# `claude --print` session triggered by new commits in the AITF repo. The prompt
# tells the role exactly what to look for and what to do; if there's nothing for
# its role this round, the role is instructed to commit nothing and stop.
AITF_SCRIBE_DEFAULTS: dict[str, dict] = {
    "pd": {
        "interval_minutes": 30,
        "round_prompt": (
            "You are the Project Director. New commits arrived. Your job this round:\n"
            "1. Skim the new commits. Did DD verdict any REPORTS as accepted? If a phase's\n"
            "   acceptance criteria are met (PROJECT_STATUS.md vs latest REPORTS), update\n"
            "   PROJECT_STATUS.md and `git commit` the change.\n"
            "2. If the current phase is complete and a new directive is needed, write\n"
            "   docs/TEAM/DIRECTIVES/NNN-<slug>.md following DIRECTIVE_TEMPLATE.md.\n"
            "3. If nothing strategic is needed this round, do NOTHING — no commit, no\n"
            "   message. Silence is the right answer when there's no new verdict to react to."
        ),
    },
    "dd": {
        "interval_minutes": 15,
        "round_prompt": (
            "You are the Development Director. New commits arrived. Your job this round:\n"
            "1. Are there NEW DIRECTIVES from PD (open status, no TODO entries yet)? If yes,\n"
            "   break each into discrete TODO items in docs/TEAM/TODO.md, record any\n"
            "   architectural decisions in DECISIONS.md, and `git commit`.\n"
            "2. Are there NEW REPORTS from Team that have not been verdicted? If yes, review\n"
            "   each against its acceptance criteria. Append a `## DD verdict` section\n"
            "   (accepted / changes_requested / rejected) and `git commit`. For\n"
            "   changes_requested, also `clade_message(to='team', ...)` with the specifics.\n"
            "3. If neither (1) nor (2) applies, do nothing this round."
        ),
    },
    "team": {
        "interval_minutes": 30,
        "round_prompt": (
            "You are the Development Team. New commits arrived. Your job this round:\n"
            "1. Read docs/TEAM/TODO.md. Find the FIRST unchecked task in the current phase.\n"
            "2. If there's no unchecked task in the current phase, do nothing this round.\n"
            "3. If there is one, read DECISIONS.md and ARCHITECTURE_VISION.md, then implement\n"
            "   the task. Run `pytest`. Write a report at docs/TEAM/REPORTS/<phase>-<slug>.md\n"
            "   following REPORT_TEMPLATE.md. `git commit` the code AND the report on a feature branch.\n"
            "4. Do not check the TODO box yourself — DD does that after verdicting."
        ),
    },
    "doc": {
        "interval_minutes": 60,
        # doc keeps the default Phase B scribe prompt — leaving round_prompt=None
        # makes _scribe_tick use the original documentation-curator instructions.
        "round_prompt": None,
    },
}


@dataclass
class AitfPeerSpec:
    peer_id: str
    display_name: str
    role: str
    extra_add_dirs: list[str]
    scribe: dict | None = None  # v1.14.0 (Phase B): only set on 'doc' peer


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
        # v1.14.0 (Phase B) → v1.15.0 (Phase C): every AITF role now becomes a
        # self-driving peer. PD/DD/Team get role-specific round_prompt (defined
        # in AITF_SCRIBE_DEFAULTS above); doc keeps the default scribe prompt.
        # All four watch the AITF project repo; cheap-exit when HEAD unchanged.
        defaults = AITF_SCRIBE_DEFAULTS.get(peer_id)
        scribe_cfg: dict | None = None
        if defaults is not None:
            scribe_cfg = {
                "enabled": True,
                "interval_minutes": defaults["interval_minutes"],
                "max_rounds_per_day": 24,
                "docs_repo_path": abs_project,
            }
            if defaults.get("round_prompt"):
                scribe_cfg["round_prompt"] = defaults["round_prompt"]
        peers.append(AitfPeerSpec(
            peer_id=peer_id,
            display_name=display,
            role=role_text,
            extra_add_dirs=[abs_project],
            scribe=scribe_cfg,
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

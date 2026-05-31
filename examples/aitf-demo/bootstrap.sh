#!/usr/bin/env bash
# Build a synthetic AITF (AI Team Framework) project on disk that the Clade
# setup-server can import via POST /api/setup/import-aitf.
#
# Idempotent: deletes $TARGET first if it exists.
#
# Usage:
#   ./bootstrap.sh                       # uses /tmp/aitf-demo-e2e
#   TARGET=/some/path ./bootstrap.sh

set -euo pipefail

TARGET="${TARGET:-/tmp/aitf-demo-e2e}"

if [[ -d "$TARGET" ]]; then
  echo "[aitf-demo] removing existing $TARGET"
  rm -rf "$TARGET"
fi

echo "[aitf-demo] building AITF project at $TARGET"
mkdir -p "$TARGET/docs/TEAM/DIRECTIVES"
mkdir -p "$TARGET/docs/TEAM/REPORTS"
mkdir -p "$TARGET/docs/TEAM/ARCHIVE/DIRECTIVES"
mkdir -p "$TARGET/docs/TEAM/ARCHIVE/REPORTS"
mkdir -p "$TARGET/docs/TEAM/ARCHIVE/DECISIONS"

cat > "$TARGET/.ai-team-config.yml" <<'YAML'
framework_version: "2.0.0"

language: "English"
claude_flags: "--dangerously-skip-permissions"

project:
  name: "task-tracker"
  description: "A small CLI task tracker built to validate the Clade A2A + AITF integration"
  tech_stack: "Python 3.13, Typer, SQLite"
  repository_url: ""
  owner_name: "Dusan"

phases:
  - number: 1
    name: "MVP CLI"
    description: "Add/list/done commands backed by SQLite"
  - number: 2
    name: "Sync"
    description: "Push/pull via a tiny HTTP server"

autonomy_level: "moderate"
review_strictness: "moderate"
dispatcher_control: "pd_dd"

test_command: "pytest"
dependency_policy: "conservative"
architecture:
  concepts:
    - "Single-process CLI"
    - "Local SQLite store"

code_organization: "src/ for code, tests/ for tests"

naming_conventions:
  files: "snake_case.py"
  classes: "PascalCase"
  functions: "snake_case"
  constants: "UPPER_SNAKE_CASE"
  variables: "snake_case"

branch_strategy: "feature_branches"
commit_convention: "conventional commits"
special_rules: []
docker_isolation: false

backup_dir: ""
doc_optimizer_enabled: true
YAML

cat > "$TARGET/docs/TEAM/PROJECT_DIRECTOR.md" <<'MD'
# Project Director — task-tracker

## 1. Who You Are
You are the Project Director for task-tracker — owner of strategy, priorities, and scope.
You do NOT write code or make technical decisions.

Project Owner: Dusan

## 2. Communication
Document-driven via docs/TEAM/, plus clade_message to other peers (dd, team, doc).

## 3. Session Startup Protocol
1. Read this file.
2. Read docs/TEAM/PROJECT_STATUS.md.
3. Read latest docs/TEAM/DIRECTIVES/ you authored.
4. Read recent docs/TEAM/REPORTS/.
5. If a new strategic decision is needed, write a directive following DIRECTIVE_TEMPLATE.md.

## 4. What You Do
- Set phase priorities.
- Approve scope changes proposed by DD.
- Issue strategic directives.
- Update PROJECT_STATUS.md when a phase completes.

## 5. Discipline
When you have nothing to add, respond `[no strategic input needed]` and stop.
MD

cat > "$TARGET/docs/TEAM/DEVELOPMENT_DIRECTOR.md" <<'MD'
# Development Director — task-tracker

## 1. Who You Are
You are the Development Director for task-tracker — owner of architecture, the task backlog,
technical decisions, and code review. You do NOT write production code; the Team does.

Project Owner: Dusan

## 2. Communication
Document-driven via docs/TEAM/, plus clade_message to other peers (pd, team, doc).

## 3. Session Startup Protocol
1. Read this file.
2. Read latest docs/TEAM/DIRECTIVES/*.md from PD.
3. Read docs/TEAM/TODO.md and docs/TEAM/DECISIONS.md.
4. Read recent docs/TEAM/REPORTS/ for review.

## 4. What You Do
- Translate directives into discrete tasks in TODO.md.
- Record decisions in DECISIONS.md.
- Review Team REPORTS — verdict (accept / reject / changes-needed).

## 5. Conventions (from .ai-team-config.yml)
- Test command: pytest
- Files: snake_case.py, classes: PascalCase, funcs: snake_case, consts: UPPER_SNAKE_CASE.
- Branch strategy: feature branches. Conventional commits.
- Dependency policy: conservative — every new dep needs a one-line justification in DECISIONS.md.

## 6. Review Bar
Review strictness: moderate. Acceptance criteria must be met; minor issues can be follow-ups.
MD

cat > "$TARGET/docs/TEAM/DEVELOPMENT_TEAM.md" <<'MD'
# Development Team — task-tracker

## 1. Who You Are
You are the Development Team for task-tracker — you write all production code, all tests, and
all implementation reports. You follow TODO.md task-by-task and DECISIONS.md for technical
guidelines. You do NOT make architectural decisions and do NOT change scope.

Project Owner: Dusan

## 2. Session Startup Protocol
1. Read this file.
2. Read docs/TEAM/TODO.md — pick the next unchecked task.
3. Read docs/TEAM/DECISIONS.md.
4. Implement.
5. Run `pytest`.
6. Write a report in docs/TEAM/REPORTS/<phase>-<task>.md following REPORT_TEMPLATE.md.

## 3. Conventions
- Files: snake_case.py under src/.
- Classes: PascalCase. Functions: snake_case. Constants: UPPER_SNAKE_CASE.
- Conventional commits, one logical change per commit, feature branch per task.

## 4. What You Do NOT Decide
Architecture, dependencies, scope. If a task is ambiguous, ask DD via clade_message(to="dd", ...)
or write `[CLARIFY]` at the top of your report.

## 5. After Acceptance
When DD accepts your report, check the TODO.md box, push the branch, and pick the next task.
MD

cat > "$TARGET/docs/TEAM/DOC_OPTIMIZER.md" <<'MD'
# Documentation Optimizer — task-tracker

## 1. Who You Are
You are the Documentation Optimizer (scribe) for task-tracker — the knowledge curator.
You keep documentation lean, archive completed work, and serve as the knowledge retrieval service.
You do NOT write code or make strategic/technical decisions.

Project Owner: Dusan

## 2. Mode (Clade A2A v1.14.0+)
You run periodic self-driven rounds via the daemon's scribe_loop. Each round is triggered by
new git commits in this repo. You are NOT a request/reply role — you sense changes and act.

## 3. Round Protocol
1. Read the changed files in the most recent commits.
2. If a project-level summary (PROJECT_STATUS.md, OPTIMIZATION_LOG.md) is stale relative to
   the commits, update it and `git commit` the change with a clear message.
3. For each peer whose owned area looks neglected (no commits to their owned paths in > 24h):
   - PD owns DIRECTIVES/, PROJECT_STATUS.md
   - DD owns TODO.md, DECISIONS.md
   - Team owns source code + REPORTS/
   Send ONE short nudge via clade_message(to=<peer>, content="reminder: ..."). One per peer per round.
4. If enough has changed since the last review, ask `pd` for review via clade_message.
5. NEVER delete information — only archive to ARCHIVE/.

## 4. Discipline
- Terse. Short commits. One nudge per peer per round.
- Do NOT speculate about activity that isn't in the commits.
- If nothing meaningful changed, write nothing and stop.
MD

cat > "$TARGET/docs/TEAM/PROJECT_STATUS.md" <<'MD'
# task-tracker — Project Status

## Phases
- [ ] Phase 1: MVP CLI — add/list/done commands backed by SQLite
- [ ] Phase 2: Sync — push/pull via a tiny HTTP server

## Current Focus
Bootstrapping. No phase started yet.

## Last Updated
Initial — by PD at project init.
MD

cat > "$TARGET/docs/TEAM/TODO.md" <<'MD'
# TODO

Maintained by DD. Team picks the next unchecked item.

## Phase 1 — MVP CLI
- [ ] (waiting on PD's first directive)
MD

cat > "$TARGET/docs/TEAM/DECISIONS.md" <<'MD'
# Decisions Log

Maintained by DD. Newest at top. Never delete — only mark superseded.

(empty — first decision will be entered after PD issues the first directive)
MD

cat > "$TARGET/docs/TEAM/OPTIMIZATION_LOG.md" <<'MD'
# Optimization Log

Maintained by DO/scribe. Each entry: round date, files touched, what was archived.

(empty — first round runs after the first new commit since import)
MD

cat > "$TARGET/docs/TEAM/ARCHIVE_INDEX.md" <<'MD'
# Archive Index

Maintained by DO/scribe. One row per archived document.

| Date | Type | Source | Summary |
|---|---|---|---|
MD

cat > "$TARGET/docs/TEAM/ARCHITECTURE_VISION.md" <<'MD'
# Architecture Vision — task-tracker

## Goals
A small, dependency-light CLI for personal task tracking. Eventually syncable across machines.

## Principles
- Conservative deps (Typer + sqlite-stdlib is enough for Phase 1).
- Single-process, single-binary feel.
- Storage: a local SQLite file under ~/.task-tracker/tasks.db.

## Non-Goals
- Multi-user collaboration features.
- A web UI in Phase 1.
MD

cat > "$TARGET/docs/TEAM/DIRECTIVE_TEMPLATE.md" <<'MD'
# Directive [NNN] — [Title]

Date: [YYYY-MM-DD]
Phase: [N]
Issued by: Project Director
Status: open | in_progress | completed

## Strategic Intent
[Why this directive matters now.]

## Scope
[What's in. What's out.]

## Acceptance Criteria
- [Criterion 1]

## Handoff
DD should translate this into TODO items.
MD

cat > "$TARGET/docs/TEAM/REPORT_TEMPLATE.md" <<'MD'
# Report [NNN] — [Title]

Date: [YYYY-MM-DD]
TODO item: [link or text]
Author: Development Team
Status: submitted | accepted | changes_requested

## What Was Done
- [Bullet list of changes.]

## Tests
- [test names + pass/fail]

## Notes for Reviewer
[Anything DD should know.]
MD

cd "$TARGET"
git init -q -b main
git config user.email "test@example.com"
git config user.name "AITF Demo"
git config commit.gpgsign false
git add .
git commit -q -m "initial AITF scaffold (PD/DD/Team/DO + config)"

echo "[aitf-demo] done: $TARGET"
echo "[aitf-demo] HEAD: $(git -C "$TARGET" rev-parse --short HEAD)"

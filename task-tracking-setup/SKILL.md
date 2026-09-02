---
name: task-tracking-setup
description: Install, upgrade, migrate, repair, or validate the repository-local task-tracking workspace and its managed AGENTS.md or CLAUDE.md instructions. Use for tracker setup work only, not ordinary development, session task updates, feature planning, delegation, status changes, or handoffs.
compatibility: opencode
---

# Task tracking setup

This skill owns tracker installation and maintenance only. After installation, ordinary agents follow the managed task-tracking instructions in the target repository's `AGENTS.md` or `CLAUDE.md` and use the installed scripts directly. Do not invoke this setup skill merely because development work needs a task record or status update.

Keep `<repository-root>/.agents/project_management/tasks/task_tracking/` authoritative. Humans open `.agents/project_management/tasks/task_tracking/open_task_board.html`. Tracker machinery lives under `.agents/project_management/tasks/setup/`; do not mix scripts or templates with task data. Application development files may live under `app/` or an existing project-specific source root, but tracker files never do. Read [references/storage-schema.md](references/storage-schema.md) before installation, upgrades, migrations, or direct task-file edits.

The setup scripts require Python 3.10 or newer and use only the standard library. Runtime version and release provenance are parsed from the checksum-bound package manifest; stable packages reject malformed tag, commit, or source-checkout identity.

## Install or upgrade

Before inspecting a setup target, ask the user to confirm one exact absolute path using `Here: /absolute/path` or `Somewhere else: provide path`. Never infer the target from the current or previous working directory. The skill source and target must not overlap. After confirmation, inspect the target read-only.

Ask which instruction destination to use: existing `AGENTS.md`, existing `CLAUDE.md`, both, or no instruction change. If both files exist, ask every invocation which one or both to update. If no instruction file exists, default to no instruction change; create `AGENTS.md` only after the exact creation is previewed and explicitly approved.

Preview every tracker path and instruction block that will change, then wait for approval. Resolve the project root and run the read-only plan first:

```bash
python3 "$(dirname "<absolute path of this SKILL.md>")/scripts/setup_project.py" --root "<absolute repository root>" --dry-run --json
```

After the plan matches the preview, apply the exact same arguments with `--plan-token "<exact plan_token from dry-run>"`. Re-plan if the target, package manifest, source assets, destinations, operations, or candidate hashes change. Both forms leave instruction files untouched unless an approved `--instruction-file` is supplied. Add one explicit option per approved destination:

```bash
python3 "$(dirname "<absolute path of this SKILL.md>")/scripts/setup_project.py" --root "<absolute repository root>" --instruction-file AGENTS.md --plan-token "<exact plan_token>"
python3 "$(dirname "<absolute path of this SKILL.md>")/scripts/setup_project.py" --root "<absolute repository root>" --instruction-file AGENTS.md --instruction-file CLAUDE.md --plan-token "<exact plan_token>"
```

Installer creates missing v4 files, refreshes tracker-owned board and setup files, replaces bounded tracker instructions only in explicitly named instruction files, migrates v1/v2/v3 storage, archives eligible completed work, and renders board data. It removes old tracker-owned paths only after validation succeeds. Do not pass an `app/` or package subdirectory as `--root` when task management belongs to the whole project. Never initialize or mutate target Git or GitHub; read-only Git verification is allowed. Any release-source checkout must be separate from the target and obtained only with explicit approval outside this installer.

The installer stages and renders the complete candidate before target writes, requires a canonical approved plan token, uses a process-scoped OS advisory lock, backs up exact touched files, journals and fsyncs changes, and writes tracker install state last. A killed process releases the lock; the next apply recovers its journal. It never recursively deletes legacy roots. Unknown files and unknown JSON envelope/task fields are preserved. Actual conflicts retain only a report and candidate under `.agents/project_management/setup/.runtime/conflicts/`; dry-run writes nothing.

## Boundary and handoff

This skill may inspect tracker data only when needed to plan or verify installation, upgrade, migration, repair, or recovery. It must not take over routine task creation, prioritization, delegation, status transitions, archiving, or handoff reporting. Those behaviors belong to the managed repository instructions installed by this skill.

After apply, verify the installed managed instruction block, tracker state, task storage, board shell, and generated data. Report the exact changed paths and validation results, then stop using this setup skill unless another setup or maintenance request is made.

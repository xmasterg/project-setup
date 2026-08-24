---
name: track-project-tasks
description: Set up and maintain a human-readable project task workspace with active status files, urgency, weekly archives, ideation documents, validated task-to-plan links, and a generated board. Use when Codex needs to install or upgrade tracking, record tasks or bugs, plan a major feature, convert ideation into tasks, delegate work, change status, archive completed work, reconcile work, or regenerate the board.
---

# Track project tasks

Keep `<repository-root>/.agents/project_management/tasks/task_tracking/` authoritative. Humans open `.agents/project_management/tasks/task_tracking/open_task_board.html`. Tracker machinery lives under `.agents/project_management/tasks/setup/`; do not mix scripts or templates with task data. Application development files may live under `app/` or an existing project-specific source root, but tracker files never do. Read [references/storage-schema.md](references/storage-schema.md) before installation, upgrades, migrations, or direct task-file edits.

## Install or upgrade

Resolve the repository root first. From that root, run:

```bash
python3 "$(dirname "<absolute path of this SKILL.md>")/scripts/setup_project.py" --root "<absolute repository root>"
```

Installer creates missing v4 files, refreshes tracker-owned setup files, replaces bounded tracker instructions in root `AGENTS.md`, migrates v1/v2/v3 storage, archives eligible completed work, and renders board. It removes old tracker-owned paths only after validation succeeds. Do not pass an `app/` or package subdirectory as `--root` when task management belongs to the whole repository.

## Read scope

For ordinary work, read `ready.json`, `in-progress.json`, and `blocked.json`. Read `backlog.json` when planning or prioritizing. Do not read weekly archives unless historical evidence is needed. Do not read `.agents/project_management/tasks/setup/` unless installing, upgrading, debugging, or changing tracker itself.

## Status meaning

- `backlog`: valid work not selected for immediate execution.
- `ready`: executable now; acceptance is clear, dependencies are done, and no blocker or user decision remains.
- `in_progress`: actively owned work. This file also keeps done children whose parent feature remains active.
- `blocked`: cannot proceed now; include `#blocked` and concrete blocker in `notes`.
- `done`: temporary state inside `in-progress.json`; archive script moves eligible standalone tasks or complete feature trees into weekly archives.

## Work protocol

1. Read smallest relevant active files before planning or changing code.
2. Add or update smallest useful record before material work. Keep at most one `in_progress` task per owner.
3. For major feature, create one parent and independently verifiable children. Keep completed children in `in-progress.json` until whole feature tree is done.
4. Before delegation, create child, set owner and `in_progress`, and give sub-agent task ID. Sub-agents update only their task and children they create.
5. Mark `done` only after acceptance passes. Add `completed_at` and verification evidence. Parent becomes done only when every descendant is done.
6. After status changes run `python3 .agents/project_management/tasks/setup/scripts/archive_tasks.py`. For non-status edits run `python3 .agents/project_management/tasks/setup/scripts/render_tasks.py`.
7. Before handoff, reconcile stale in-progress work and report remaining blocked, ready, and backlog IDs.

## Ideation and planning

Store major-feature brainstorming and planning documents under `.agents/project_management/tasks/ideation/`. Human or AI may create them; after a brainstorming or planning session, create one only when user requested durable output or task conversion.

Every task contains `planning_docs`. Use an empty array when unrelated. When task derives from or changes an ideation document, list exact project-relative path such as `.agents/project_management/tasks/ideation/2026-08-25-whatsapp-backend.md`. Read linked documents before implementation. Add corrections or decision notes to source document when scope includes maintaining it; otherwise record discrepancy in task notes.

## Record rules

Use monotonically increasing `TASK-####` and `BUG-####` IDs. Every record requires:

- `id`, `type`, `title`, `section`, `status`, `priority`, `urgency`
- `owner`, `parent_id`, `depends_on`, `planning_docs`, `tags`
- `description`, `acceptance`, `notes`
- `reproduction`, `expected`, `actual`
- `created_at`, `updated_at`, `completed_at`

Allowed values:

- `type`: `feature`, `task`, `bug`, `chore`, `research`
- `status`: `backlog`, `ready`, `in_progress`, `blocked`, `done`
- `priority`: `P0`, `P1`, `P2`, `P3`
- `urgency`: `urgent`, `high`, `normal`, `low`

Priority is importance; urgency is time pressure. For bugs populate reproduction, expected, and actual; otherwise keep them empty. Use ISO-8601 timestamps and `owner: "unassigned"` when nobody owns item.

## Safe editing

Preserve unknown envelope keys and task fields. Move whole records; never duplicate IDs. Never edit generated board or weekly archives during ordinary work. Installer-owned `AGENTS.md` block is delimited by `<!-- task-tracker:start -->` and `<!-- task-tracker:end -->`.

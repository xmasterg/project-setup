# Storage schema

## Project layout

This tree is rooted at the repository or selected project-management root, not at `app/` or another application source folder. Application files are outside this tracker tree.

```text
.agents/
`-- project_management/
    `-- tasks/
        |-- task_tracking/
        |   |-- open_task_board.html
        |   |-- backlog.json
        |   |-- blocked.json
        |   |-- in-progress.json
        |   |-- ready.json
        |   `-- completed/YYYY/MM/week-NN/tasks.json
        |-- ideation/
        |   |-- README.md
        |   `-- feature-plan.template.md
        `-- setup/
            |-- tracker.json
            |-- scripts/
            |   |-- archive_tasks.py
            |   |-- render_tasks.py
            |   `-- task_store.py
            `-- task_board/
                `-- task_board.template.html
```

Humans normally use only `task_tracking/`, `ideation/`, and generated `open_task_board.html`. `setup/` is tracker-owned machinery.

## File routing

| Record state | Location |
|---|---|
| `backlog` | `task_tracking/backlog.json` |
| `ready` | `task_tracking/ready.json` |
| `in_progress` | `task_tracking/in-progress.json` |
| `blocked` | `task_tracking/blocked.json` |
| done child of active feature | `task_tracking/in-progress.json` |
| eligible done standalone or complete tree | `task_tracking/completed/YYYY/MM/week-NN/tasks.json` |

Every JSON task file uses `{ "tasks": [] }`. `.agents/project_management/tasks/setup/tracker.json` contains schema version, project name, and board timestamp.

## Ready criteria

A ready task must have clear non-empty acceptance, no `#blocked` or `#needs-user` tag, and every `depends_on` record must be done. Ready means next-startable, not merely important.

## Planning document links

Every task has `planning_docs`, an array of project-relative files under `.agents/project_management/tasks/ideation/`. Paths must remain inside that folder and point to existing files. Use `[]` for unrelated work. A task derived from ideation must list every source document needed to understand or verify it.

## Archive eligibility

- Standalone done task archives immediately.
- Done child remains in `in-progress.json` while any ancestor feature remains active.
- Feature root and all descendants archive together only when complete tree is done.
- Child is never archived without parent in same weekly file.
- Root `completed_at` selects `completed/YYYY/MM/week-NN/tasks.json`.
- Reopen archived bundle by moving required records to matching active files, clearing `completed_at` when no longer done, and explaining reason in `notes`.

## Migration

Installer supports v1 `.agents/tasks.json`, v2 `.agents/tasks/`, and v3 root `tasks/`. Missing `urgency` becomes `normal`; missing `planning_docs` becomes `[]`. During v3 migration, ideation files move into `.agents/project_management/tasks/ideation/` and task links are rebased to that project-relative path. Existing weekly archives retain their folder date. Old tracker-owned paths are removed only after v4 validation and board rendering succeed; unrelated `.agents` content remains.

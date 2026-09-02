# Vendored tracker storage schema

This copy is synchronized with the tracker package embedded in `assets/vendor/task-tracking-setup.zip`.

## Project layout

The tracker is rooted at the repository or selected project-management root, never at `app/` or another application source folder.

```text
.agents/project_management/tasks/
|-- task_tracking/
|   |-- open_task_board.html
|   |-- task_board.data.js
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
    |   |-- install_transaction.py
    |   |-- render_tasks.py
    |   `-- task_store.py
    `-- task_board/
        |-- task_board.css
        `-- task_board.js
```

Humans use `task_tracking/`, `ideation/`, and `open_task_board.html`. Rendering validates the whole store and atomically updates only `task_board.data.js`. The classic HTML/CSS/JavaScript board opens directly with `file://`; it needs no server, package manager, framework, or build step.

## Routing and validation

- Backlog, ready, in-progress, and blocked records live in their matching active JSON files.
- Done children remain in `in-progress.json` while an ancestor feature is active.
- Eligible done standalone tasks and complete trees move together into `completed/YYYY/MM/week-NN/tasks.json` selected from the root completion time.
- Every task has an exact project-relative `planning_docs` array under `.agents/project_management/tasks/ideation/`.
- Ready tasks require acceptance criteria, completed dependencies, and no blocker tags.
- A child is never archived separately from its parent; archive updates are one recoverable multi-file transaction.

## Migration and preservation

The installer supports v1 `.agents/tasks.json`, v2 `.agents/tasks/`, and v3 root `tasks/`. Missing `urgency` becomes `normal`; missing `planning_docs` becomes `[]`. Unknown task fields and active/archive envelope fields remain on their records. Unknown v2 index fields are preserved under `tracker.json` → `migration_metadata.legacy_v2_index`; unknown v3 tracker fields are preserved under `migration_metadata.legacy_v3_tracker`. Conflicting migration metadata aborts the entire operation.

V3 ideation files move into `.agents/project_management/tasks/ideation/`, with links rebased to that path. Unknown metadata from an empty v1 task envelope is preserved deterministically in `tracker.json` under `migration_metadata.legacy_v1_empty_envelope`. Existing archive dates remain unchanged. The installer retires only exact positively owned legacy files after candidate validation and rendering. Unknown files and directories remain untouched.

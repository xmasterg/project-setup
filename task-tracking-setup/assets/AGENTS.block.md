<!-- task-tracker:start -->
## Local task tracking

- Follow this block directly during ordinary development; `task-tracking-setup` is only for installing, upgrading, migrating, repairing, or validating the tracker.
- `.agents/project_management/tasks/task_tracking/` is authoritative. Read `ready.json`, `in-progress.json`, and `blocked.json` first; read `backlog.json` when planning or prioritizing, and archives only for historical evidence.
- Read every linked `planning_docs` file before related work.
- Before material work, create or update the smallest useful record. Keep at most one `in_progress` task per owner; major features use one parent with independently verifiable children.
- Before delegation, create an owned `in_progress` child and give the delegate its task ID. Mark work `done` only after acceptance passes, with completion time and verification evidence.
- Preserve unknown fields, move whole records without duplicate IDs, and use `#blocked` plus a concrete note when work cannot proceed.
- After status changes run `python3 .agents/project_management/tasks/setup/scripts/archive_tasks.py`; after other task edits run `python3 .agents/project_management/tasks/setup/scripts/render_tasks.py`.
- Before handoff, reconcile stale `in_progress` work and report remaining blocked, ready, and backlog IDs.
- Humans open `.agents/project_management/tasks/task_tracking/open_task_board.html`. Never edit generated `task_board.data.js` directly.
<!-- task-tracker:end -->

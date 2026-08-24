#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

from task_store import (
    ACTIVE_FILE_NAMES,
    envelope_with_tasks,
    layout_from_root,
    load_sources,
    read_json,
    tasks_from_source,
    write_json_atomic,
)

BLOCK_START = "<!-- task-tracker:start -->"
BLOCK_END = "<!-- task-tracker:end -->"
OLD_RUNTIME_NAMES = (
    "archive_tasks.py",
    "render_tasks.py",
    "task_store.py",
    "task-board.html",
    "task-board.template.html",
)


def replace_instruction_block(agents_md: Path, block: str) -> None:
    existing = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    start = existing.find(BLOCK_START)
    end = existing.find(BLOCK_END)
    if (start == -1) != (end == -1):
        raise SystemExit(f"Malformed task-tracker block in {agents_md}")
    if start == -1:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + block + "\n"
    else:
        if existing.find(BLOCK_START, start + len(BLOCK_START)) != -1:
            raise SystemExit(f"Multiple task-tracker blocks in {agents_md}")
        end += len(BLOCK_END)
        updated = existing[:start].rstrip() + "\n\n" + block + existing[end:]
        updated = updated.rstrip() + "\n"
    agents_md.write_text(updated, encoding="utf-8")


def ensure_project_files(skill: Path, project_root: Path) -> None:
    layout = layout_from_root(project_root)
    asset_root = skill / "assets" / "project" / "tasks"
    for relative in (
        Path("task_tracking/backlog.json"),
        Path("task_tracking/blocked.json"),
        Path("task_tracking/in-progress.json"),
        Path("task_tracking/ready.json"),
        Path("setup/tracker.json"),
        Path("ideation/README.md"),
        Path("ideation/feature-plan.template.md"),
    ):
        source = asset_root / relative
        target = layout.tasks_root / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    layout.scripts_root.mkdir(parents=True, exist_ok=True)
    for name in ("task_store.py", "archive_tasks.py", "render_tasks.py"):
        shutil.copy2(skill / "scripts" / name, layout.scripts_root / name)
    layout.template_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        asset_root / "setup" / "task_board" / "task_board.template.html",
        layout.template_path,
    )

    tracker = read_json(layout.tracker_path)
    if tracker.get("project") == "Project tasks":
        tracker["project"] = project_root.name
        write_json_atomic(layout.tracker_path, tracker)


def normalize_task(raw_task: dict, source: Path, *, rebase_v3_paths: bool = False) -> dict:
    if not isinstance(raw_task, dict):
        raise SystemExit(f"Every task in {source} must be an object")
    task = dict(raw_task)
    task.setdefault("urgency", "normal")
    task.setdefault("planning_docs", [])
    if rebase_v3_paths:
        rebased = []
        for path in task["planning_docs"]:
            if isinstance(path, str) and path.startswith("tasks/ideation/"):
                path = f".agents/project_management/tasks/{path[len('tasks/') :]}"
            rebased.append(path)
        task["planning_docs"] = rebased
    return task


def collect_v1(project_root: Path) -> tuple[list[tuple[Path, list[dict]]], str | None, Path | None]:
    legacy_path = project_root / ".agents" / "tasks.json"
    if not legacy_path.exists():
        return [], None, None
    data = read_json(legacy_path)
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise SystemExit(f"{legacy_path} must contain a tasks array")
    layout = layout_from_root(project_root)
    grouped: dict[Path, list[dict]] = defaultdict(list)
    for raw_task in tasks:
        task = normalize_task(raw_task, legacy_path)
        status = task.get("status")
        target_status = "in_progress" if status in {"in_progress", "done"} else status
        if target_status not in layout.active_paths:
            raise SystemExit(f"Legacy task {task.get('id')} has unsupported status: {status}")
        grouped[layout.active_paths[target_status]].append(task)
    return list(grouped.items()), data.get("project"), legacy_path


def collect_v2(project_root: Path) -> tuple[list[tuple[Path, list[dict]]], str | None, Path | None]:
    old_root = project_root / ".agents" / "tasks"
    index_path = old_root / "index.json"
    if not index_path.exists():
        return [], None, None
    index = read_json(index_path)
    if index.get("schema_version") != 2:
        raise SystemExit(f"Refusing to remove unrecognized old tracker at {old_root}")
    files = index.get("files")
    if not isinstance(files, dict):
        raise SystemExit(f"{index_path} must contain a files object")
    layout = layout_from_root(project_root)
    grouped: dict[Path, list[dict]] = defaultdict(list)
    for old_key in ("backlog", "ready", "in_progress", "blocked", "completed"):
        relative = files.get(old_key)
        if not isinstance(relative, str):
            raise SystemExit(f"{index_path} is missing file mapping: {old_key}")
        source = old_root / relative
        envelope = read_json(source)
        target_status = "in_progress" if old_key in {"in_progress", "completed"} else old_key
        target = layout.active_paths[target_status]
        grouped[target].extend(normalize_task(task, source) for task in tasks_from_source(source, envelope))

    archive_relative = index.get("completed_archive", "completed")
    old_archive = old_root / archive_relative
    if old_archive.exists():
        for source in sorted(old_archive.glob("*/*/week-*/tasks.json")):
            relative = source.relative_to(old_archive)
            target = layout.archive_root / relative
            envelope = read_json(source)
            grouped[target].extend(normalize_task(task, source) for task in tasks_from_source(source, envelope))
    return list(grouped.items()), index.get("project"), old_root


def copy_v3_ideation(project_root: Path, old_root: Path) -> None:
    source_root = old_root / "ideation"
    if not source_root.exists():
        return
    target_root = layout_from_root(project_root).ideation_root
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        target = target_root / source.relative_to(source_root)
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise SystemExit(f"V3 ideation file conflicts with destination: {source}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def collect_v3(project_root: Path) -> tuple[list[tuple[Path, list[dict]]], str | None, Path | None]:
    old_root = project_root / "tasks"
    tracker_path = old_root / "setup" / "tracker.json"
    if not tracker_path.exists():
        return [], None, None
    tracker = read_json(tracker_path)
    if tracker.get("schema_version") != 3:
        raise SystemExit(f"Refusing to remove unrecognized old tracker at {old_root}")

    layout = layout_from_root(project_root)
    old_tracking = old_root / "task_tracking"
    grouped: dict[Path, list[dict]] = defaultdict(list)
    for status, filename in ACTIVE_FILE_NAMES.items():
        source = old_tracking / filename
        envelope = read_json(source)
        target = layout.active_paths[status]
        grouped[target].extend(
            normalize_task(task, source, rebase_v3_paths=True)
            for task in tasks_from_source(source, envelope)
        )
    old_archive = old_tracking / "completed"
    if old_archive.exists():
        for source in sorted(old_archive.glob("*/*/week-*/tasks.json")):
            target = layout.archive_root / source.relative_to(old_archive)
            envelope = read_json(source)
            grouped[target].extend(
                normalize_task(task, source, rebase_v3_paths=True)
                for task in tasks_from_source(source, envelope)
            )
    copy_v3_ideation(project_root, old_root)
    return list(grouped.items()), tracker.get("project"), old_root


def merge_migrations(project_root: Path, migrations: list[tuple[Path, list[dict]]]) -> int:
    layout = layout_from_root(project_root)
    _, sources = load_sources(layout)
    current: dict[str, dict] = {}
    envelopes: dict[Path, dict] = {}
    for path, envelope in sources:
        envelopes[path.resolve()] = envelope
        for task in tasks_from_source(path, envelope):
            current[task["id"]] = task

    migrated = 0
    additions: dict[Path, list[dict]] = defaultdict(list)
    for target, tasks in migrations:
        for task in tasks:
            task_id = task.get("id")
            existing = current.get(task_id)
            if existing:
                if existing != task:
                    raise SystemExit(f"Legacy task {task_id} conflicts with migrated task data")
                continue
            additions[target.resolve()].append(task)
            current[task_id] = task
            migrated += 1

    for target, tasks in additions.items():
        envelope = envelopes.get(target)
        if envelope is None:
            envelope = read_json(target) if target.exists() else {"tasks": []}
        existing = tasks_from_source(target, envelope)
        write_json_atomic(target, envelope_with_tasks(envelope, existing + tasks))
    return migrated


def cleanup_legacy(
    project_root: Path,
    v1_path: Path | None,
    v2_root: Path | None,
    v3_root: Path | None,
) -> None:
    if v1_path and v1_path.exists():
        v1_path.unlink()
    if v2_root and v2_root.exists():
        index = read_json(v2_root / "index.json")
        if index.get("schema_version") != 2:
            raise SystemExit(f"Refusing to remove unrecognized old tracker at {v2_root}")
        shutil.rmtree(v2_root)
    if v3_root and v3_root.exists():
        tracker = read_json(v3_root / "setup" / "tracker.json")
        if tracker.get("schema_version") != 3:
            raise SystemExit(f"Refusing to remove unrecognized old tracker at {v3_root}")
        shutil.rmtree(v3_root)
    old_agents = project_root / ".agents"
    for name in OLD_RUNTIME_NAMES:
        path = old_agents / name
        if path.exists() and path.is_file():
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or upgrade local task tracker")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    project_root = Path(args.root).resolve()
    if not project_root.is_dir():
        raise SystemExit(f"Project root does not exist: {project_root}")

    skill = Path(__file__).resolve().parent.parent
    ensure_project_files(skill, project_root)
    layout = layout_from_root(project_root)
    block = (skill / "assets" / "AGENTS.block.md").read_text(encoding="utf-8").strip()
    replace_instruction_block(project_root / "AGENTS.md", block)

    v1_migrations, v1_project, v1_path = collect_v1(project_root)
    v2_migrations, v2_project, v2_root = collect_v2(project_root)
    v3_migrations, v3_project, v3_root = collect_v3(project_root)
    migrated = merge_migrations(
        project_root,
        v1_migrations + v2_migrations + v3_migrations,
    )
    tracker = read_json(layout.tracker_path)
    legacy_project = v3_project or v2_project or v1_project
    if legacy_project:
        tracker["project"] = legacy_project
        write_json_atomic(layout.tracker_path, tracker)

    subprocess.run(
        ["python3", str(layout.scripts_root / "archive_tasks.py"), "--no-render"],
        cwd=project_root,
        check=True,
    )
    subprocess.run(
        ["python3", str(layout.scripts_root / "render_tasks.py")],
        cwd=project_root,
        check=True,
    )
    cleanup_legacy(project_root, v1_path, v2_root, v3_root)
    if migrated:
        print(f"Migrated {migrated} tasks into v4 storage")
    print(f"Task tracker ready: {layout.board_path}")


if __name__ == "__main__":
    main()

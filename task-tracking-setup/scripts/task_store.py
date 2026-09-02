#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED = {
    "id", "type", "title", "section", "status", "priority", "urgency",
    "owner", "parent_id", "depends_on", "planning_docs", "tags",
    "description", "acceptance", "notes", "reproduction", "expected", "actual",
    "created_at", "updated_at", "completed_at",
}
TYPES = {"feature", "task", "bug", "chore", "research"}
STATUSES = {"backlog", "ready", "in_progress", "blocked", "done"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
URGENCIES = {"urgent", "high", "normal", "low"}
DISPLAY_STRING_FIELDS = {
    "id", "type", "title", "section", "status", "priority", "urgency",
    "owner", "parent_id", "description", "acceptance", "notes", "reproduction",
    "expected", "actual", "created_at", "updated_at", "completed_at",
}
NON_EMPTY_DISPLAY_STRING_FIELDS = {
    "id", "type", "title", "section", "status", "priority", "urgency", "owner",
    "created_at", "updated_at",
}
ACTIVE_FILE_NAMES = {
    "backlog": "backlog.json",
    "ready": "ready.json",
    "in_progress": "in-progress.json",
    "blocked": "blocked.json",
}


@dataclass(frozen=True)
class Layout:
    project_root: Path
    tasks_root: Path
    tracking_root: Path
    ideation_root: Path
    setup_root: Path
    scripts_root: Path
    tracker_path: Path
    board_assets_root: Path
    board_path: Path
    board_data_path: Path
    board_css_path: Path
    board_js_path: Path
    legacy_template_path: Path
    archive_root: Path
    active_paths: dict[str, Path]


def layout_from_root(project_root: Path) -> Layout:
    project_root = project_root.resolve()
    tasks_root = project_root / ".agents" / "project_management" / "tasks"
    tracking_root = tasks_root / "task_tracking"
    setup_root = tasks_root / "setup"
    return Layout(
        project_root=project_root,
        tasks_root=tasks_root,
        tracking_root=tracking_root,
        ideation_root=tasks_root / "ideation",
        setup_root=setup_root,
        scripts_root=setup_root / "scripts",
        tracker_path=setup_root / "tracker.json",
        board_assets_root=setup_root / "task_board",
        board_path=tracking_root / "open_task_board.html",
        board_data_path=tracking_root / "task_board.data.js",
        board_css_path=setup_root / "task_board" / "task_board.css",
        board_js_path=setup_root / "task_board" / "task_board.js",
        legacy_template_path=setup_root / "task_board" / "task_board.template.html",
        archive_root=tracking_root / "completed",
        active_paths={
            status: tracking_root / filename for status, filename in ACTIVE_FILE_NAMES.items()
        },
    )


def layout_from_script(script_path: Path) -> Layout:
    script = script_path.resolve()
    expected = ("scripts", "setup", "tasks", "project_management", ".agents")
    if tuple(parent.name for parent in script.parents[:5]) != expected:
        raise SystemExit(
            f"Tracker script is not under .agents/project_management/tasks/setup/scripts: {script}"
        )
    return layout_from_root(script.parents[5])


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Task file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Task file must contain an object: {path}")
    return data


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def tasks_from_source(path: Path, envelope: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = envelope.get("tasks")
    if not isinstance(tasks, list):
        raise SystemExit(f"{path} must contain a tasks array")
    for task in tasks:
        if not isinstance(task, dict):
            raise SystemExit(f"Every task in {path} must be an object")
    return tasks


def load_sources(layout: Layout) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    tracker = read_json(layout.tracker_path)
    sources = [(layout.active_paths[status], read_json(layout.active_paths[status])) for status in ACTIVE_FILE_NAMES]
    if layout.archive_root.exists():
        for path in sorted(layout.archive_root.glob("*/*/week-*/tasks.json")):
            sources.append((path, read_json(path)))
    return tracker, sources


def parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"{label} is not a valid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise SystemExit(f"{label} must include a timezone: {value}")
    return parsed


def expected_statuses(path: Path, layout: Layout) -> set[str]:
    resolved = path.resolve()
    for status, active_path in layout.active_paths.items():
        if resolved == active_path.resolve():
            return {"in_progress", "done"} if status == "in_progress" else {status}
    return {"done"}


def validate_planning_doc(layout: Layout, task_id: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"Task {task_id} planning_docs entries must be non-empty strings")
    relative = Path(value)
    prefix = (".agents", "project_management", "tasks", "ideation")
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:4] != prefix:
        raise SystemExit(
            f"Task {task_id} planning document must be under "
            f".agents/project_management/tasks/ideation: {value}"
        )
    resolved = (layout.project_root / relative).resolve()
    ideation = layout.ideation_root.resolve()
    if resolved == ideation or ideation not in resolved.parents:
        raise SystemExit(
            f"Task {task_id} planning document escapes "
            f".agents/project_management/tasks/ideation: {value}"
        )
    if not resolved.is_file():
        raise SystemExit(f"Task {task_id} planning document does not exist: {value}")


def validate_store(
    layout: Layout,
    tracker: dict[str, Any],
    sources: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    if tracker.get("schema_version") != 4:
        raise SystemExit(
            ".agents/project_management/tasks/setup/tracker.json must use schema_version 4"
        )

    tasks: list[dict[str, Any]] = []
    source_by_id: dict[str, Path] = {}
    for path, envelope in sources:
        allowed_statuses = expected_statuses(path, layout)
        for position, task in enumerate(tasks_from_source(path, envelope), start=1):
            missing = REQUIRED - set(task)
            if missing:
                raise SystemExit(
                    f"Task {position} in {path} is missing: {', '.join(sorted(missing))}"
                )
            for field in DISPLAY_STRING_FIELDS:
                if not isinstance(task[field], str):
                    raise SystemExit(f"Task {position} in {path} {field} must be a string")
            for field in NON_EMPTY_DISPLAY_STRING_FIELDS:
                if not task[field].strip():
                    raise SystemExit(f"Task {position} in {path} {field} must be non-empty")
            task_id = task["id"]
            if not isinstance(task_id, str) or not task_id:
                raise SystemExit(f"Task {position} in {path} has invalid id")
            if task_id in source_by_id:
                raise SystemExit(f"Task ID {task_id} is duplicated in {source_by_id[task_id]} and {path}")
            if task["type"] not in TYPES:
                raise SystemExit(f"Task {task_id} has invalid type: {task['type']}")
            if task["status"] not in STATUSES or task["status"] not in allowed_statuses:
                raise SystemExit(f"Task {task_id} has status {task['status']} but lives in {path.name}")
            if task["priority"] not in PRIORITIES:
                raise SystemExit(f"Task {task_id} has invalid priority: {task['priority']}")
            if task["urgency"] not in URGENCIES:
                raise SystemExit(f"Task {task_id} has invalid urgency: {task['urgency']}")
            for field in ("depends_on", "planning_docs", "tags"):
                if not isinstance(task[field], list):
                    raise SystemExit(f"Task {task_id} {field} must be an array")
                if any(not isinstance(value, str) or not value for value in task[field]):
                    raise SystemExit(
                        f"Task {task_id} {field} entries must be non-empty strings"
                    )
            for planning_doc in task["planning_docs"]:
                validate_planning_doc(layout, task_id, planning_doc)
            parse_timestamp(task["created_at"], f"Task {task_id} created_at")
            parse_timestamp(task["updated_at"], f"Task {task_id} updated_at")
            if task["status"] == "done":
                parse_timestamp(task["completed_at"], f"Task {task_id} completed_at")
            elif task["completed_at"]:
                raise SystemExit(f"Task {task_id} is not done but has completed_at")
            tasks.append(task)
            source_by_id[task_id] = path

    known = set(source_by_id)
    by_id = {task["id"]: task for task in tasks}
    children: dict[str, list[str]] = {task_id: [] for task_id in known}
    in_progress_owners: dict[str, str] = {}
    for task in tasks:
        task_id = task["id"]
        references = ([task["parent_id"]] if task["parent_id"] else []) + task["depends_on"]
        unknown = [item for item in references if item not in known]
        if unknown:
            raise SystemExit(f"Task {task_id} references unknown IDs: {', '.join(unknown)}")
        if task["parent_id"]:
            children[task["parent_id"]].append(task_id)
        if task["status"] == "in_progress":
            if task["owner"] == "unassigned":
                raise SystemExit(f"In-progress task {task_id} must have an owner")
            previous = in_progress_owners.get(task["owner"])
            if previous:
                raise SystemExit(
                    f"Owner {task['owner']} has multiple in-progress tasks: {previous}, {task_id}"
                )
            in_progress_owners[task["owner"]] = task_id
        if task["status"] == "ready":
            if not isinstance(task["acceptance"], str) or not task["acceptance"].strip():
                raise SystemExit(f"Ready task {task_id} must have acceptance criteria")
            forbidden = {"#blocked", "#needs-user"} & set(task["tags"])
            if forbidden:
                raise SystemExit(f"Ready task {task_id} has non-ready tags: {', '.join(sorted(forbidden))}")
        if task["status"] == "blocked":
            if "#blocked" not in task["tags"] or not str(task["notes"]).strip():
                raise SystemExit(f"Blocked task {task_id} requires #blocked and concrete notes")

    for task in tasks:
        task_id = task["id"]
        seen: set[str] = set()
        parent = task["parent_id"]
        while parent:
            if parent in seen or parent == task_id:
                raise SystemExit(f"Task {task_id} has a parent cycle")
            seen.add(parent)
            parent = by_id[parent]["parent_id"]
        if task["status"] == "ready":
            unfinished = [dep for dep in task["depends_on"] if by_id[dep]["status"] != "done"]
            if unfinished:
                raise SystemExit(f"Ready task {task_id} has unfinished dependencies: {', '.join(unfinished)}")
        if task["status"] == "done":
            unfinished = [child for child in children[task_id] if by_id[child]["status"] != "done"]
            if unfinished:
                raise SystemExit(f"Completed parent {task_id} has unfinished children: {', '.join(unfinished)}")

    active_paths = {path.resolve() for path in layout.active_paths.values()}
    archive_paths = {path.resolve() for path, _ in sources if path.resolve() not in active_paths}

    def descendants(task_id: str) -> set[str]:
        result: set[str] = set()
        pending = list(children[task_id])
        while pending:
            child = pending.pop()
            if child in result:
                continue
            result.add(child)
            pending.extend(children[child])
        return result

    for task in tasks:
        path = source_by_id[task["id"]].resolve()
        if path not in archive_paths:
            continue
        if task["parent_id"] and source_by_id[task["parent_id"]].resolve() != path:
            raise SystemExit(f"Archived child {task['id']} is separated from parent {task['parent_id']}")
        if not task["parent_id"]:
            separated = [item for item in descendants(task["id"]) if source_by_id[item].resolve() != path]
            if separated:
                raise SystemExit(
                    f"Archived root {task['id']} is separated from descendants: {', '.join(sorted(separated))}"
                )
    return tasks


def envelope_with_tasks(envelope: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    updated = dict(envelope)
    updated["tasks"] = tasks
    return updated

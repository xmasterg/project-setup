#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.dont_write_bytecode = True

from task_store import Layout, layout_from_script, load_sources, parse_timestamp, validate_store


BOARD_DATA_VERSION = 1
TRACKER_SCHEMA_VERSION = 4
STATUS_ORDER = {"backlog": 0, "ready": 1, "in_progress": 2, "blocked": 3, "done": 4}


def validate_board_config(tracker: dict[str, Any]) -> dict[str, Any]:
    project = tracker.get("project", "Project tasks")
    if not isinstance(project, str) or not project.strip():
        raise SystemExit("Tracker project must be a non-empty string")
    updated_at = tracker.get("updated_at", "")
    if not isinstance(updated_at, str):
        raise SystemExit("Tracker updated_at must be a string")
    if updated_at:
        parse_timestamp(updated_at, "Tracker updated_at")
    board = tracker.get("board", {})
    if not isinstance(board, dict):
        raise SystemExit("Tracker board configuration must be an object")
    default_view = board.get("default_view", "kanban")
    if default_view not in {"kanban", "list"}:
        raise SystemExit("Tracker board.default_view must be kanban or list")
    show_archived = board.get("show_archived", False)
    if not isinstance(show_archived, bool):
        raise SystemExit("Tracker board.show_archived must be a boolean")
    return {
        "default_view": default_view,
        "project": project,
        "show_archived": show_archived,
        "tracker_updated_at": updated_at,
    }


def planning_document_link(path: str) -> dict[str, str]:
    parts = Path(path).parts
    relative_parts = parts[4:]
    encoded = "/".join(quote(part, safe="") for part in relative_parts)
    return {
        "href": f"../ideation/{encoded}",
        "label": relative_parts[-1],
        "path": path,
    }


def board_task(task: dict[str, Any]) -> dict[str, Any]:
    rendered = dict(task)
    rendered["planning_doc_links"] = [
        planning_document_link(path) for path in task["planning_docs"]
    ]
    return rendered


def task_sort_key(task: dict[str, Any]) -> tuple[Any, ...]:
    return (
        STATUS_ORDER[task["status"]],
        task["section"].casefold(),
        task["title"].casefold(),
        task["id"],
    )


def build_board_payload(
    layout: Layout,
    tracker: dict[str, Any],
    sources: list[tuple[Path, dict[str, Any]]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    config = validate_board_config(tracker)
    archived_paths = {
        path.resolve()
        for path, _ in sources
        if layout.archive_root.resolve() in path.resolve().parents
    }
    source_by_task_id: dict[str, Path] = {}
    source_metadata: list[dict[str, Any]] = []
    for path, envelope in sources:
        source_tasks = envelope["tasks"]
        source_kind = "archive" if path.resolve() in archived_paths else "active"
        relative_path = path.relative_to(layout.project_root).as_posix()
        source_metadata.append(
            {
                "kind": source_kind,
                "path": relative_path,
                "task_count": len(source_tasks),
            }
        )
        for task in source_tasks:
            source_by_task_id[task["id"]] = path.resolve()

    active_tasks: list[dict[str, Any]] = []
    archived_tasks: list[dict[str, Any]] = []
    for task in tasks:
        rendered = board_task(task)
        if source_by_task_id[task["id"]] in archived_paths:
            archived_tasks.append(rendered)
        else:
            active_tasks.append(rendered)
    active_tasks.sort(key=task_sort_key)
    archived_tasks.sort(key=task_sort_key)
    source_metadata.sort(key=lambda item: (item["kind"], item["path"]))
    return {
        "active_tasks": active_tasks,
        "archived_tasks": archived_tasks,
        "board_data_version": BOARD_DATA_VERSION,
        "config": config,
        "sources": source_metadata,
        "tracker_schema_version": TRACKER_SCHEMA_VERSION,
    }


def serialize_board_payload(payload: dict[str, Any]) -> bytes:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    serialized = (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"globalThis.__TASK_BOARD_DATA__ = {serialized};\n".encode("utf-8")


def replace_payload_if_changed(path: Path, payload: bytes) -> bool:
    if path.exists() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return True


def render_board(layout: Layout) -> tuple[int, int, bool]:
    tracker, sources = load_sources(layout)
    tasks = validate_store(layout, tracker, sources)
    payload = build_board_payload(layout, tracker, sources, tasks)
    changed = replace_payload_if_changed(layout.board_data_path, serialize_board_payload(payload))
    return len(payload["active_tasks"]), len(payload["archived_tasks"]), changed


def main() -> None:
    layout = layout_from_script(Path(__file__))
    active_count, archived_count, changed = render_board(layout)
    action = "Rendered" if changed else "Unchanged"
    print(
        f"{action} {active_count} active tasks and {archived_count} archived tasks "
        f"at {layout.board_data_path}"
    )


if __name__ == "__main__":
    main()

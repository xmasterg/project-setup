#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

from task_store import (
    envelope_with_tasks,
    layout_from_script,
    load_sources,
    parse_timestamp,
    read_json,
    tasks_from_source,
    validate_store,
    write_json_atomic,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive eligible completed project tasks")
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    layout = layout_from_script(Path(__file__))
    tracker, sources = load_sources(layout)
    tasks = validate_store(layout, tracker, sources)
    in_progress_path = layout.active_paths["in_progress"]
    in_progress_envelope = next(
        envelope for path, envelope in sources if path.resolve() == in_progress_path.resolve()
    )
    in_progress_tasks = tasks_from_source(in_progress_path, in_progress_envelope)
    in_progress_ids = {task["id"] for task in in_progress_tasks}
    by_id = {task["id"]: task for task in tasks}
    children: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        if task["parent_id"]:
            children[task["parent_id"]].append(task["id"])

    def bundle(root_id: str) -> list[str]:
        result = [root_id]
        position = 0
        while position < len(result):
            result.extend(children[result[position]])
            position += 1
        return result

    archive_groups: dict[Path, list[dict]] = defaultdict(list)
    archived_ids: set[str] = set()
    for root in in_progress_tasks:
        if root["status"] != "done" or root["parent_id"]:
            continue
        bundle_ids = bundle(root["id"])
        if not all(
            item in in_progress_ids and by_id[item]["status"] == "done" for item in bundle_ids
        ):
            continue
        completed_at = parse_timestamp(root["completed_at"], f"Task {root['id']} completed_at")
        week = completed_at.isocalendar().week
        archive_path = (
            layout.archive_root
            / f"{completed_at.year:04d}"
            / f"{completed_at.month:02d}"
            / f"week-{week:02d}"
            / "tasks.json"
        )
        archive_groups[archive_path].extend(by_id[item] for item in bundle_ids)
        archived_ids.update(bundle_ids)

    if archive_groups:
        replacements: dict[Path, dict] = {}
        for path, records in archive_groups.items():
            envelope = read_json(path) if path.exists() else {"tasks": []}
            existing = tasks_from_source(path, envelope)
            duplicates = {task["id"] for task in existing} & {task["id"] for task in records}
            if duplicates:
                raise SystemExit(f"Archive already contains IDs: {', '.join(sorted(duplicates))}")
            replacements[path.resolve()] = envelope_with_tasks(envelope, existing + records)
        replacements[in_progress_path.resolve()] = envelope_with_tasks(
            in_progress_envelope,
            [task for task in in_progress_tasks if task["id"] not in archived_ids],
        )
        candidate_sources = [
            (path, replacements.get(path.resolve(), envelope)) for path, envelope in sources
        ]
        known_paths = {path.resolve() for path, _ in candidate_sources}
        candidate_sources.extend(
            (path, envelope) for path, envelope in replacements.items() if path not in known_paths
        )
        validate_store(layout, tracker, candidate_sources)
        for path, envelope in replacements.items():
            if path != in_progress_path.resolve():
                write_json_atomic(path, envelope)
        write_json_atomic(in_progress_path, replacements[in_progress_path.resolve()])

    if not args.no_render:
        subprocess.run(["python3", str(layout.scripts_root / "render_tasks.py")], cwd=layout.project_root, check=True)
    print(f"Archived {len(archived_ids)} tasks across {len(archive_groups)} weekly files")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

from task_store import layout_from_script, load_sources, validate_store, write_json_atomic


def main() -> None:
    layout = layout_from_script(Path(__file__))
    tracker, sources = load_sources(layout)
    tasks = validate_store(layout, tracker, sources)
    tracker["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json_atomic(layout.tracker_path, tracker)

    payload_data = {
        "schema_version": tracker["schema_version"],
        "project": tracker.get("project", "Project tasks"),
        "updated_at": tracker["updated_at"],
        "tasks": tasks,
    }
    payload = json.dumps(payload_data, ensure_ascii=False).replace("</", "<\\/")
    template = layout.template_path.read_text(encoding="utf-8")
    if "__TASK_DATA__" not in template:
        raise SystemExit("Task board template is missing __TASK_DATA__")
    layout.board_path.write_text(template.replace("__TASK_DATA__", payload), encoding="utf-8")
    archived = sum(
        len(envelope["tasks"])
        for path, envelope in sources
        if layout.archive_root.resolve() in path.resolve().parents
    )
    print(f"Rendered {len(tasks)} tasks ({archived} archived) to {layout.board_path}")


if __name__ == "__main__":
    main()

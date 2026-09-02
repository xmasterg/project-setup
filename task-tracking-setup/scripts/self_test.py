#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def record(
    task_id: str,
    status: str,
    *,
    parent_id: str = "",
    task_type: str = "task",
) -> dict:
    completed_at = "2026-08-25T10:00:00Z" if status == "done" else ""
    return {
        "id": task_id,
        "type": task_type,
        "title": f"Observable outcome for {task_id}",
        "section": "Self test",
        "status": status,
        "priority": "P2",
        "owner": "self-test" if status == "in_progress" else "unassigned",
        "parent_id": parent_id,
        "depends_on": [],
        "tags": ["#self-test"],
        "description": "Exercise tracker migration and archival.",
        "acceptance": "Self-test assertions pass.",
        "notes": "Temporary fixture.",
        "reproduction": "",
        "expected": "",
        "actual": "",
        "created_at": "2026-08-25T09:00:00Z",
        "updated_at": "2026-08-25T10:00:00Z",
        "completed_at": completed_at,
        "urgency": "normal",
    }


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_board_data(path: Path) -> dict:
    prefix = "globalThis.__TASK_BOARD_DATA__ = "
    payload = path.read_text(encoding="utf-8")
    assert payload.startswith(prefix) and payload.endswith(";\n")
    return json.loads(payload[len(prefix) : -2])


def run(*command: str, cwd: Path, expect_success: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if expect_success and result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def install(setup: Path, target: Path) -> None:
    preview = run(
        sys.executable,
        str(setup),
        "--root",
        str(target),
        "--dry-run",
        "--json",
        cwd=target,
    )
    plan_token = json.loads(preview.stdout)["plan_token"]
    run(
        sys.executable,
        str(setup),
        "--root",
        str(target),
        "--plan-token",
        plan_token,
        "--json",
        cwd=target,
    )


def main() -> None:
    skill = Path(__file__).resolve().parent.parent
    setup = skill / "scripts" / "setup_project.py"
    with tempfile.TemporaryDirectory(prefix="task-tracking-setup-v4-") as temporary:
        base = Path(temporary)

        fresh = base / "fresh-project"
        fresh.mkdir()
        (fresh / "app").mkdir()
        install(setup, fresh)
        fresh_tasks = fresh / ".agents/project_management/tasks"
        fresh_board = fresh_tasks / "task_tracking/open_task_board.html"
        assert fresh_board.is_file()
        fresh_board_html = fresh_board.read_text(encoding="utf-8")
        assert 'id="labelFilters"' in fresh_board_html
        assert 'id="tagSearch"' in fresh_board_html
        assert 'placeholder="Search hashtags"' in fresh_board_html
        assert 'id="tagSuggestions"' in fresh_board_html
        assert 'id="tagFilters"' in fresh_board_html
        assert 'id="sectionFilterCount"' in fresh_board_html
        assert 'id="sectionFilters"' in fresh_board_html
        assert 'href="../setup/task_board/task_board.css"' in fresh_board_html
        assert 'src="task_board.data.js"' in fresh_board_html
        assert 'src="../setup/task_board/task_board.js"' in fresh_board_html
        assert "<style" not in fresh_board_html and "<script>" not in fresh_board_html
        assert (fresh_tasks / "setup/scripts/archive_tasks.py").is_file()
        assert (fresh_tasks / "setup/task_board/task_board.css").is_file()
        assert (fresh_tasks / "setup/task_board/task_board.js").is_file()
        assert not (fresh_tasks / "setup/task_board/task_board.template.html").exists()
        fresh_data = read_board_data(fresh_tasks / "task_tracking/task_board.data.js")
        assert fresh_data["board_data_version"] == 1
        assert fresh_data["tracker_schema_version"] == 4
        assert fresh_data["active_tasks"] == [] and fresh_data["archived_tasks"] == []
        assert (fresh_tasks / "ideation/feature-plan.template.md").is_file()
        assert not (fresh / ".agents/task-board.html").exists()
        assert not (fresh / "app/.agents").exists()

        v2 = base / "v2-project"
        old_tasks = v2 / ".agents" / "tasks"
        old_tasks.mkdir(parents=True)
        (v2 / "AGENTS.md").write_text(
            "# Existing instructions\n\n<!-- task-tracker:start -->\nold\n<!-- task-tracker:end -->\n",
            encoding="utf-8",
        )
        write(
            old_tasks / "index.json",
            {
                "schema_version": 2,
                "project": "V2 project",
                "updated_at": "",
                "files": {
                    "backlog": "backlog.json",
                    "ready": "ready.json",
                    "in_progress": "in-progress.json",
                    "blocked": "blocked.json",
                    "completed": "completed.json",
                },
                "completed_archive": "completed",
            },
        )
        write(old_tasks / "backlog.json", {"tasks": [record("TASK-0003", "backlog")]})
        write(old_tasks / "ready.json", {"tasks": []})
        write(old_tasks / "in-progress.json", {"tasks": [record("TASK-0001", "in_progress", task_type="feature")]})
        write(old_tasks / "blocked.json", {"tasks": []})
        write(old_tasks / "completed.json", {"tasks": [record("TASK-0002", "done", parent_id="TASK-0001")]})
        old_archive = old_tasks / "completed/2026/08/week-35/tasks.json"
        write(old_archive, {"tasks": [record("TASK-0004", "done")]})
        for name in ("archive_tasks.py", "render_tasks.py", "task_store.py", "task-board.html", "task-board.template.html"):
            (v2 / ".agents" / name).write_text("old tracker file\n", encoding="utf-8")
        unknown_v2 = old_tasks / "keep-me.txt"
        unknown_v2.write_text("unknown user file\n", encoding="utf-8")

        install(setup, v2)
        v2_tasks = v2 / ".agents/project_management/tasks"
        tracking = v2_tasks / "task_tracking"
        scripts = v2_tasks / "setup" / "scripts"
        assert unknown_v2.read_text(encoding="utf-8") == "unknown user file\n"
        assert not (old_tasks / "index.json").exists()
        assert all(
            (v2 / ".agents" / name).read_text(encoding="utf-8") == "old tracker file\n"
            for name in ("archive_tasks.py", "render_tasks.py", "task_store.py", "task-board.html", "task-board.template.html")
        )
        assert [task["id"] for task in read(tracking / "in-progress.json")["tasks"]] == ["TASK-0001", "TASK-0002"]
        assert all("planning_docs" in task for task in read(tracking / "in-progress.json")["tasks"])
        archive = tracking / "completed/2026/08/week-35/tasks.json"
        assert [task["id"] for task in read(archive)["tasks"]] == ["TASK-0004"]

        plan = v2_tasks / "ideation/2026-08-25-self-test.md"
        plan.write_text("# Self-test plan\n", encoding="utf-8")
        backlog = read(tracking / "backlog.json")
        backlog["tasks"][0]["planning_docs"] = [
            ".agents/project_management/tasks/ideation/2026-08-25-self-test.md"
        ]
        write(tracking / "backlog.json", backlog)
        run(sys.executable, str(scripts / "render_tasks.py"), cwd=v2)
        assert "2026-08-25-self-test.md" in (tracking / "task_board.data.js").read_text()

        backlog["tasks"][0]["planning_docs"] = [
            ".agents/project_management/tasks/ideation/missing.md"
        ]
        write(tracking / "backlog.json", backlog)
        previous_payload = (tracking / "task_board.data.js").read_bytes()
        failed = run(sys.executable, str(scripts / "render_tasks.py"), cwd=v2, expect_success=False)
        assert failed.returncode != 0 and "does not exist" in failed.stderr
        assert (tracking / "task_board.data.js").read_bytes() == previous_payload
        backlog["tasks"][0]["planning_docs"] = [
            ".agents/project_management/tasks/ideation/2026-08-25-self-test.md"
        ]
        write(tracking / "backlog.json", backlog)

        in_progress = read(tracking / "in-progress.json")
        root = in_progress["tasks"][0]
        root.update(
            status="done",
            owner="unassigned",
            updated_at="2026-08-25T11:00:00Z",
            completed_at="2026-08-25T11:00:00Z",
        )
        write(tracking / "in-progress.json", in_progress)
        run(sys.executable, str(scripts / "archive_tasks.py"), cwd=v2)
        assert read(tracking / "in-progress.json")["tasks"] == []
        assert {task["id"] for task in read(archive)["tasks"]} == {"TASK-0001", "TASK-0002", "TASK-0004"}
        install(setup, v2)
        assert (v2 / "AGENTS.md").read_text().count("<!-- task-tracker:start -->") == 1

        v3 = base / "v3-project"
        old_v3 = v3 / "tasks"
        old_tracking = old_v3 / "task_tracking"
        write(
            old_v3 / "setup/tracker.json",
            {"schema_version": 3, "project": "V3 project", "updated_at": ""},
        )
        for filename in ("backlog.json", "blocked.json", "in-progress.json", "ready.json"):
            write(old_tracking / filename, {"tasks": []})
        old_plan = old_v3 / "ideation/2026-08-25-v3-plan.md"
        old_plan.parent.mkdir(parents=True, exist_ok=True)
        old_plan.write_text("# V3 migration plan\n", encoding="utf-8")
        unknown_v3 = old_v3 / "keep-me.txt"
        unknown_v3.write_text("unknown user file\n", encoding="utf-8")
        v3_task = record("TASK-0001", "backlog")
        v3_task["planning_docs"] = ["tasks/ideation/2026-08-25-v3-plan.md"]
        write(old_tracking / "backlog.json", {"tasks": [v3_task]})
        write(
            old_tracking / "completed/2026/08/week-35/tasks.json",
            {"tasks": [record("TASK-0002", "done")]},
        )

        install(setup, v3)
        new_v3 = v3 / ".agents/project_management/tasks"
        assert unknown_v3.read_text(encoding="utf-8") == "unknown user file\n"
        assert not (old_v3 / "setup/tracker.json").exists()
        assert (new_v3 / "ideation/2026-08-25-v3-plan.md").read_text() == "# V3 migration plan\n"
        migrated_v3 = read(new_v3 / "task_tracking/backlog.json")["tasks"]
        assert migrated_v3[0]["planning_docs"] == [
            ".agents/project_management/tasks/ideation/2026-08-25-v3-plan.md"
        ]
        assert [
            task["id"]
            for task in read(new_v3 / "task_tracking/completed/2026/08/week-35/tasks.json")["tasks"]
        ] == ["TASK-0002"]

        v1 = base / "v1-project"
        (v1 / ".agents").mkdir(parents=True)
        write(
            v1 / ".agents/tasks.json",
            {
                "schema_version": 1,
                "project": "V1 project",
                "updated_at": "",
                "custom_envelope": {"preserve": True},
                "tasks": [{**record("TASK-0001", "ready"), "custom_task_field": "preserve"}],
            },
        )
        install(setup, v1)
        assert not (v1 / ".agents/tasks.json").exists()
        migrated_envelope = read(v1 / ".agents/project_management/tasks/task_tracking/ready.json")
        migrated = migrated_envelope["tasks"]
        assert migrated_envelope["custom_envelope"] == {"preserve": True}
        assert migrated[0]["planning_docs"] == []
        assert migrated[0]["custom_task_field"] == "preserve"
    print("task-tracking-setup v4 self-test passed")


if __name__ == "__main__":
    main()

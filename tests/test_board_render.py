from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRACKER_ROOT = REPOSITORY_ROOT / "task-tracking-setup"
sys.path.insert(0, str(TRACKER_ROOT / "scripts"))

from setup_project import execute_install  # noqa: E402
from install_transaction import transaction_id  # noqa: E402


DATA_PREFIX = "globalThis.__TASK_BOARD_DATA__ = "


def task_record(task_id: str, status: str = "backlog", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": task_id,
        "type": "task",
        "title": f"Outcome for {task_id}",
        "section": "Board tests",
        "status": status,
        "priority": "P2",
        "urgency": "normal",
        "owner": "unassigned",
        "parent_id": "",
        "depends_on": [],
        "planning_docs": [],
        "tags": ["#board"],
        "description": "Detailed description.",
        "acceptance": "Observable acceptance.",
        "notes": "",
        "reproduction": "",
        "expected": "",
        "actual": "",
        "created_at": "2026-08-27T10:00:00Z",
        "updated_at": "2026-08-27T10:00:00Z",
        "completed_at": "2026-08-27T11:00:00Z" if status == "done" else "",
    }
    record.update(overrides)
    return record


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_payload(path: Path) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    if not source.startswith(DATA_PREFIX) or not source.endswith(";\n"):
        raise AssertionError("Board data is not a single classic-script assignment")
    return json.loads(source[len(DATA_PREFIX) : -2])


def install_tracker(skill: Path, root: Path) -> tuple[int, dict]:
    preview_code, preview = execute_install(skill, root, (), dry_run=True)
    if preview_code not in {0, 1}:
        return preview_code, preview
    return execute_install(
        skill,
        root,
        (),
        dry_run=False,
        plan_token=preview["plan_token"],
    )


def write_receipt_backed_tracker_state(root: Path, artifacts: list[dict]) -> None:
    state = {
        "state_schema_version": 1,
        "bundle_version": "0.1.0",
        "component": "task-tracking-setup",
        "component_version": "0.1.0",
        "manifest_schema_version": 1,
        "tracker_data_schema_version": 3,
        "board_data_version": 0,
        "artifact_policies": {},
        "artifacts": artifacts,
        "migrations": [],
        "source_identity": {"kind": "legacy-receipt-fixture"},
        "approved_plan_token": "legacy-receipt-fixture",
    }
    transaction = transaction_id([], state["component_version"], state)
    state["last_successful_transaction"] = transaction
    write_json(
        root / ".agents/project_management/setup/tracker-install-state.json",
        state,
    )
    write_json(
        root / f".agents/project_management/setup/receipts/{transaction}.json",
        {
            "receipt_schema_version": 1,
            "transaction_id": transaction,
            "component": state["component"],
            "component_version": state["component_version"],
            "bundle_version": state["bundle_version"],
            "manifest_schema_version": state["manifest_schema_version"],
            "tracker_data_schema_version": state["tracker_data_schema_version"],
            "board_data_version": state["board_data_version"],
            "artifact_policies": state["artifact_policies"],
            "source_identity": state["source_identity"],
            "artifacts": state["artifacts"],
            "migrations": state["migrations"],
            "approved_plan_token": state["approved_plan_token"],
            "operations": [],
        },
    )


class BoardRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="board-render-tests-")
        self.root = Path(self.temporary.name) / "target"
        self.root.mkdir()
        code, payload = install_tracker(TRACKER_ROOT, self.root)
        self.assertEqual(0, code, payload)
        self.tasks_root = self.root / ".agents/project_management/tasks"
        self.tracking = self.tasks_root / "task_tracking"
        self.setup = self.tasks_root / "setup"
        self.renderer = self.setup / "scripts/render_tasks.py"
        self.data_path = self.tracking / "task_board.data.js"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_renderer(self, *, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(self.renderer)],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if expect_success and result.returncode:
            self.fail(f"Renderer failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result

    def test_fresh_split_layout_and_payload_shape(self) -> None:
        shell = self.tracking / "open_task_board.html"
        css = self.setup / "task_board/task_board.css"
        javascript = self.setup / "task_board/task_board.js"
        self.assertTrue(shell.is_file())
        self.assertTrue(css.is_file())
        self.assertTrue(javascript.is_file())
        self.assertTrue(self.data_path.is_file())
        self.assertFalse((self.setup / "task_board/task_board.template.html").exists())
        html = shell.read_text(encoding="utf-8")
        self.assertIn('href="../setup/task_board/task_board.css"', html)
        self.assertIn('src="task_board.data.js"', html)
        self.assertIn('src="../setup/task_board/task_board.js"', html)
        self.assertIn('id="activeFilters"', html)
        self.assertIn('id="usedHashtagCount"', html)
        self.assertNotIn("<style", html)
        payload = read_payload(self.data_path)
        self.assertEqual(1, payload["board_data_version"])
        self.assertEqual(4, payload["tracker_schema_version"])
        self.assertEqual([], payload["active_tasks"])
        self.assertEqual([], payload["archived_tasks"])
        self.assertIsInstance(payload["sources"], list)
        install_state = json.loads(
            (self.root / ".agents/project_management/setup/tracker-install-state.json").read_text()
        )
        self.assertEqual(1, install_state["board_data_version"])
        board_data_state = next(
            item for item in install_state["artifacts"] if item["id"] == "board-data"
        )
        self.assertEqual("0.2.0-dev.0", board_data_state["component_version"])
        self.assertEqual(board_data_state["installed_sha256"], board_data_state["source_sha256"])

    def test_render_changes_only_payload_and_repeated_render_preserves_mtime(self) -> None:
        shell = self.tracking / "open_task_board.html"
        css = self.setup / "task_board/task_board.css"
        javascript = self.setup / "task_board/task_board.js"
        tracker = self.setup / "tracker.json"
        stable_files = {path: path.read_bytes() for path in (shell, css, javascript, tracker)}
        backlog = self.tracking / "backlog.json"
        write_json(backlog, {"tasks": [task_record("TASK-0001")]})
        before_payload = self.data_path.read_bytes()
        self.run_renderer()
        self.assertNotEqual(before_payload, self.data_path.read_bytes())
        for path, expected in stable_files.items():
            self.assertEqual(expected, path.read_bytes(), path)
        first_bytes = self.data_path.read_bytes()
        first_mtime = self.data_path.stat().st_mtime_ns
        result = self.run_renderer()
        self.assertIn("Unchanged", result.stdout)
        self.assertEqual(first_bytes, self.data_path.read_bytes())
        self.assertEqual(first_mtime, self.data_path.stat().st_mtime_ns)

    def test_failed_render_preserves_previous_payload_and_rejects_bad_display_fields(self) -> None:
        backlog = self.tracking / "backlog.json"
        write_json(backlog, {"tasks": [task_record("TASK-0001")]})
        self.run_renderer()
        baseline = self.data_path.read_bytes()

        malformed_tag = task_record("TASK-0001", tags=["#valid", 7])
        write_json(backlog, {"tasks": [malformed_tag]})
        failed = self.run_renderer(expect_success=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("tags entries must be non-empty strings", failed.stderr)
        self.assertEqual(baseline, self.data_path.read_bytes())

        malformed_title = task_record("TASK-0001", title="  ")
        write_json(backlog, {"tasks": [malformed_title]})
        failed = self.run_renderer(expect_success=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("title must be non-empty", failed.stderr)
        self.assertEqual(baseline, self.data_path.read_bytes())

    def test_special_planning_paths_and_script_safe_serialization(self) -> None:
        filename = "road map ü % # ? \" ' &.md"
        planning_document = self.tasks_root / "ideation" / filename
        planning_document.write_text("# Plan\n", encoding="utf-8")
        planning_path = f".agents/project_management/tasks/ideation/{filename}"
        dangerous_title = "</ScRiPt><tag>&\u2028\u2029\"\\ control\u0001"
        record = task_record(
            "TASK-0001",
            title=dangerous_title,
            tags=["#safe", "</SCRIPT>", "amp&tag"],
            planning_docs=[planning_path],
        )
        write_json(self.tracking / "backlog.json", {"tasks": [record]})
        self.run_renderer()
        source = self.data_path.read_text(encoding="utf-8")
        self.assertNotIn("</ScRiPt", source)
        self.assertNotIn("</SCRIPT", source)
        self.assertNotIn("\u2028", source.replace("\\u2028", ""))
        self.assertNotIn("\u2029", source.replace("\\u2029", ""))
        self.assertIn("\\u003c", source)
        self.assertIn("\\u003e", source)
        self.assertIn("\\u0026", source)
        payload = read_payload(self.data_path)
        task = payload["active_tasks"][0]
        self.assertEqual(dangerous_title, task["title"])
        link = task["planning_doc_links"][0]
        self.assertEqual(
            "../ideation/road%20map%20%C3%BC%20%25%20%23%20%3F%20%22%20%27%20%26.md",
            link["href"],
        )

    def test_active_and_large_archive_data_are_separated(self) -> None:
        write_json(self.tracking / "ready.json", {"tasks": [task_record("TASK-0001", "ready")]})
        archived = [task_record(f"TASK-{index:04d}", "done") for index in range(2, 1502)]
        archive = self.tracking / "completed/2026/08/week-35/tasks.json"
        write_json(archive, {"tasks": archived})
        self.run_renderer()
        payload = read_payload(self.data_path)
        self.assertEqual(1, len(payload["active_tasks"]))
        self.assertEqual(1500, len(payload["archived_tasks"]))
        archive_sources = [source for source in payload["sources"] if source["kind"] == "archive"]
        self.assertEqual(1500, archive_sources[0]["task_count"])

    def test_archive_transaction_recovers_every_write_boundary_without_duplicates(self) -> None:
        child_script = """
import pathlib, sys, time
scripts = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(scripts))
from archive_tasks import archive_completed, recover_archive_transaction
from install_transaction import InstallLock
from task_store import layout_from_root
root = pathlib.Path(sys.argv[2])
layout = layout_from_root(root)
def stop(point):
    if point == sys.argv[3]:
        pathlib.Path(sys.argv[4]).write_text('ready')
        while True: time.sleep(1)
with InstallLock(root):
    recover_archive_transaction(layout)
    archive_completed(layout, fault_injector=stop)
"""
        scripts = self.setup / "scripts"
        for index, boundary in enumerate(("after_journal", "after_write_0", "after_write_1")):
            with self.subTest(boundary=boundary):
                task_id = f"ARCHIVE-{index}"
                write_json(
                    self.tracking / "in-progress.json",
                    {"tasks": [task_record(task_id, "done")]},
                )
                marker = Path(self.temporary.name) / f"archive-{index}.ready"
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_script,
                        str(scripts),
                        str(self.root),
                        boundary,
                        str(marker),
                    ]
                )
                for _ in range(100):
                    if marker.exists():
                        break
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                os.kill(child.pid, 9)
                child.wait(timeout=5)
                result = subprocess.run(
                    [sys.executable, str(scripts / "archive_tasks.py"), "--no-render"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr or result.stdout)
                archived_ids: list[str] = []
                for path in self.tracking.glob("completed/*/*/week-*/tasks.json"):
                    archived_ids.extend(task["id"] for task in json.loads(path.read_text())["tasks"])
                self.assertEqual(1, archived_ids.count(task_id))
                self.assertFalse(
                    (
                        self.tasks_root
                        / "setup/.runtime/archive-journal.json"
                    ).exists()
                )

    def test_archive_recovery_refuses_post_crash_target_edit(self) -> None:
        write_json(
            self.tracking / "in-progress.json",
            {"tasks": [task_record("ARCHIVE-DRIFT", "done")]},
        )
        marker = Path(self.temporary.name) / "archive-drift.ready"
        child_script = """
import pathlib, sys, time
scripts = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(scripts))
from archive_tasks import archive_completed
from install_transaction import InstallLock
from task_store import layout_from_root
root = pathlib.Path(sys.argv[2])
def stop(point):
    if point == 'after_write_0':
        pathlib.Path(sys.argv[3]).write_text('ready')
        while True: time.sleep(1)
with InstallLock(root):
    archive_completed(layout_from_root(root), fault_injector=stop)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_script, str(self.setup / "scripts"), str(self.root), str(marker)]
        )
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        os.kill(child.pid, 9)
        child.wait(timeout=5)

        journal_path = self.tasks_root / "setup/.runtime/archive-journal.json"
        journal = json.loads(journal_path.read_text())
        edited = self.root / journal["operations"][0]["target"]
        edited.write_text("post-crash archive edit\n")
        result = subprocess.run(
            [sys.executable, str(self.setup / "scripts/archive_tasks.py"), "--no-render"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("post-crash edits", result.stderr)
        self.assertEqual("post-crash archive edit\n", edited.read_text())
        self.assertTrue(journal_path.exists())
        for operation in journal["operations"]:
            self.assertTrue((self.root / operation["candidate"]).exists())
            if operation["backup"]:
                self.assertTrue((self.root / operation["backup"]).exists())

    def test_archive_recovery_refuses_corrupt_candidate_without_mutation(self) -> None:
        write_json(
            self.tracking / "in-progress.json",
            {"tasks": [task_record("ARCHIVE-CORRUPT", "done")]},
        )
        scripts = self.setup / "scripts"
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, pathlib, sys
scripts=pathlib.Path(sys.argv[1]); sys.path.insert(0, str(scripts))
from archive_tasks import archive_completed
from install_transaction import InstallLock
from task_store import layout_from_root
def crash(point):
    if point == 'after_journal': os._exit(91)
root=pathlib.Path(sys.argv[2])
with InstallLock(root): archive_completed(layout_from_root(root), fault_injector=crash)
""",
                str(scripts),
                str(self.root),
            ],
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        journal_path = self.tasks_root / "setup/.runtime/archive-journal.json"
        journal = json.loads(journal_path.read_text())
        before = {
            item["target"]: (self.root / item["target"]).read_bytes()
            if (self.root / item["target"]).exists()
            else None
            for item in journal["operations"]
        }
        (self.root / journal["operations"][0]["candidate"]).write_text("corrupt\n")
        result = subprocess.run(
            [sys.executable, str(scripts / "archive_tasks.py"), "--no-render"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("candidate is missing or corrupt", result.stderr)
        for target, payload in before.items():
            path = self.root / target
            self.assertEqual(payload, path.read_bytes() if path.exists() else None)
        self.assertTrue(journal_path.exists())

    def test_static_shell_and_classic_js_expose_visible_data_errors(self) -> None:
        shell = (self.tracking / "open_task_board.html").read_text(encoding="utf-8")
        javascript_path = self.setup / "task_board/task_board.js"
        javascript = javascript_path.read_text(encoding="utf-8")
        self.assertIn('id="boardError"', shell)
        self.assertNotIn('id="boardError" class="error-panel" hidden', shell)
        self.assertIn("Task board data payload is missing or corrupt", javascript)
        self.assertIn("Unsupported board_data_version", javascript)
        self.assertIn("Required board element is missing", javascript)
        subprocess.run(["node", "--check", str(javascript_path)], check=True)

        harness = r'''
const fs = require("fs");
const nodes = {
  boardError: {hidden: true},
  boardErrorMessage: {textContent: ""},
  boardErrorDetail: {textContent: ""},
  boardApplication: {hidden: false},
};
global.document = {
  body: {textContent: ""},
  getElementById(id) { return nodes[id] || null; },
};
global.__TASK_BOARD_DATA__ = JSON.parse(process.argv[2]);
eval(fs.readFileSync(process.argv[1], "utf8"));
process.stdout.write(JSON.stringify(nodes));
'''
        cases = (
            (None, "missing or corrupt"),
            ({"board_data_version": 99}, "Unsupported board_data_version"),
            ({"board_data_version": 1, "tracker_schema_version": 99, "config": {}}, "Unsupported tracker_schema_version"),
        )
        for payload, expected in cases:
            result = subprocess.run(
                ["node", "-e", harness, str(javascript_path), json.dumps(payload)],
                check=True,
                capture_output=True,
                text=True,
            )
            nodes = json.loads(result.stdout)
            self.assertFalse(nodes["boardError"]["hidden"])
            self.assertTrue(nodes["boardApplication"]["hidden"])
            self.assertIn(expected, nodes["boardErrorDetail"]["textContent"])

    def test_view_tabs_declare_and_implement_roving_keyboard_focus(self) -> None:
        shell = (self.tracking / "open_task_board.html").read_text(encoding="utf-8")
        javascript = (
            self.setup / "task_board/task_board.js"
        ).read_text(encoding="utf-8")
        self.assertIn('role="tablist"', shell)
        self.assertIn('aria-selected="true" tabindex="0"', shell)
        self.assertIn('aria-selected="false" tabindex="-1"', shell)
        self.assertIn("kanbanButton.tabIndex = showKanban ? 0 : -1", javascript)
        self.assertIn("listButton.tabIndex = showKanban ? -1 : 0", javascript)
        self.assertIn("Home: 0", javascript)
        self.assertIn("End: buttons.length - 1", javascript)
        self.assertIn("setView(next === 0 ? \"kanban\" : \"list\", true)", javascript)
        self.assertIn("(showKanban ? kanbanButton : listButton).focus()", javascript)

    def test_board_exposes_clickable_multi_filters_details_and_column_toggles(self) -> None:
        javascript = (self.setup / "task_board/task_board.js").read_text(encoding="utf-8")
        css = (self.setup / "task_board/task_board.css").read_text(encoding="utf-8")
        self.assertIn('data-task-filter-group="${escapeHtml(group)}"', javascript)
        self.assertIn('data-filter-remove-group="${escapeHtml(group)}"', javascript)
        self.assertIn('data-column-toggle="${status}"', javascript)
        self.assertIn('<details class="task-details"><summary>Details</summary>', javascript)
        self.assertIn("selected.tags.size && !task.tags.some", javascript)
        self.assertIn("selected.urgencies.size", javascript)
        self.assertIn("selected.priorities.size", javascript)
        self.assertIn("selected.sections.size", javascript)
        self.assertIn(".column-toggle", css)
        self.assertIn(".active-filter-chip", css)


class BoardInstallerUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="board-installer-tests-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "target"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reinstall_preserves_tracker_config_and_refreshes_managed_assets(self) -> None:
        skill = self.base / "task-tracking-setup"
        shutil.copytree(TRACKER_ROOT, skill)
        self.assertEqual(0, install_tracker(skill, self.root)[0])
        tracker_path = self.root / ".agents/project_management/tasks/setup/tracker.json"
        tracker = json.loads(tracker_path.read_text())
        tracker["project"] = "Configured project"
        tracker["custom_setting"] = {"preserve": True}
        write_json(tracker_path, tracker)
        source_css = skill / "assets/project/tasks/setup/task_board/task_board.css"
        source_css.write_text(source_css.read_text() + "\n/* managed refresh */\n")
        manifest_path = skill / "assets/install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        board_styles = next(item for item in manifest["artifacts"] if item["id"] == "board-styles")
        board_styles["source_sha256"] = hashlib.sha256(source_css.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        code, payload = install_tracker(skill, self.root)
        self.assertEqual(0, code, payload)
        installed_css = self.root / ".agents/project_management/tasks/setup/task_board/task_board.css"
        self.assertIn("managed refresh", installed_css.read_text())
        self.assertEqual(tracker, json.loads(tracker_path.read_text()))

    def test_unowned_custom_legacy_template_conflicts_without_partial_update(self) -> None:
        legacy = self.root / ".agents/project_management/tasks/setup/task_board/task_board.template.html"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("custom legacy template\n")
        code, payload = install_tracker(TRACKER_ROOT, self.root)
        self.assertEqual(1, code)
        self.assertEqual("custom legacy template\n", legacy.read_text())
        self.assertFalse(
            (self.root / ".agents/project_management/tasks/task_tracking/open_task_board.html").exists()
        )
        self.assertTrue((self.root / payload["conflict_report"]).is_file())

    def test_receipt_owned_generated_board_transitions_to_managed_shell(self) -> None:
        board_relative = ".agents/project_management/tasks/task_tracking/open_task_board.html"
        board = self.root / board_relative
        board.parent.mkdir(parents=True)
        generated_payload = b"<!doctype html><title>previous generated board</title>\n"
        board.write_bytes(generated_payload)
        write_receipt_backed_tracker_state(
            self.root,
            [
                {
                    "id": "generated-board",
                    "policy": "generated",
                    "target": board_relative,
                    "installed_sha256": hashlib.sha256(generated_payload).hexdigest(),
                }
            ],
        )
        code, payload = install_tracker(TRACKER_ROOT, self.root)
        self.assertEqual(0, code, payload)
        self.assertIn("task_board.data.js", board.read_text())
        state = json.loads(
            (self.root / ".agents/project_management/setup/tracker-install-state.json").read_text()
        )
        artifact_ids = {item["id"] for item in state["artifacts"]}
        self.assertIn("board-shell", artifact_ids)
        self.assertNotIn("generated-board", artifact_ids)

    def test_receipt_owned_legacy_template_is_retired_exactly(self) -> None:
        legacy_relative = ".agents/project_management/tasks/setup/task_board/task_board.template.html"
        legacy = self.root / legacy_relative
        legacy.parent.mkdir(parents=True)
        legacy_payload = b"receipt-owned legacy template\n"
        legacy.write_bytes(legacy_payload)
        write_receipt_backed_tracker_state(
            self.root,
            [
                {
                    "id": "board-template",
                    "policy": "managed",
                    "target": legacy_relative,
                    "installed_sha256": hashlib.sha256(legacy_payload).hexdigest(),
                }
            ],
        )
        unknown = legacy.parent / "unknown.keep"
        unknown.write_text("preserve\n")
        code, payload = install_tracker(TRACKER_ROOT, self.root)
        self.assertEqual(0, code, payload)
        self.assertFalse(legacy.exists())
        self.assertEqual("preserve\n", unknown.read_text())


if __name__ == "__main__":
    unittest.main()

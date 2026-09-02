from __future__ import annotations

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
RUNTIME_ROOT = REPOSITORY_ROOT / "project-setup/runtime"
SCRIPT = RUNTIME_ROOT / "integrate_instructions.py"
CATALOG_PATH = REPOSITORY_ROOT / "project-setup/assets/instructions/catalog.json"
CAPABILITY_PATH = REPOSITORY_ROOT / "capabilities/catalog.yaml"
sys.path.insert(0, str(RUNTIME_ROOT))

from coordinator import Coordinator, EXIT_CONFLICT, EXIT_SUCCESS  # noqa: E402
from integrate_instructions import (  # noqa: E402
    InstructionError,
    apply_plan,
    build_plan,
    load_catalog,
    render_block,
    validate_selection,
)


class InstructionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="instruction-integration-tests-")
        self.root = Path(self.temporary.name) / "target"
        self.root.mkdir()
        self.catalog = load_catalog(CATALOG_PATH)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(
        self,
        action: str,
        *,
        destinations: tuple[str, ...] = ("AGENTS.md",),
        bundles: tuple[str, ...] = (),
        auto: bool = False,
        capabilities: tuple[str, ...] = (),
        plan_token: str | None = None,
        expect: int = 0,
    ) -> dict:
        command = [sys.executable, str(SCRIPT), action, "--root", str(self.root), "--json"]
        for destination in destinations:
            command.extend(("--destination", destination))
        if auto:
            command.append("--auto")
        else:
            for bundle in bundles:
                command.extend(("--bundle", bundle))
        for capability in capabilities:
            command.extend(("--capability", capability))
        if action == "apply" and not auto and plan_token is None:
            preview = self.execute(
                "plan",
                destinations=destinations,
                bundles=bundles,
                capabilities=capabilities,
                expect=1 if expect == 1 else 0,
            )
            plan_token = preview["plan_token"]
        if plan_token is not None:
            command.extend(("--plan-token", plan_token))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(expect, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def direct_plan(
        self,
        bundles: tuple[str, ...],
        *,
        catalog=None,
        destinations: tuple[str, ...] = ("AGENTS.md",),
        capabilities: tuple[str, ...] = (),
        auto: bool = False,
    ):
        return build_plan(
            self.root,
            catalog or self.catalog,
            CAPABILITY_PATH,
            destinations,
            bundles,
            capabilities,
            auto=auto,
        )

    def test_catalog_has_versioned_coherent_bundle_contracts(self) -> None:
        self.assertEqual("1.0.0", self.catalog.version)
        expected = {
            "project-identity-map",
            "documentation-discipline",
            "task-tracking",
            "tools-capability-inventory",
            "testing-verification",
            "development-guidance",
        }
        self.assertEqual(expected, {bundle.bundle_id for bundle in self.catalog.bundles})
        for bundle in self.catalog.bundles:
            self.assertTrue(bundle.version)
            self.assertTrue(bundle.purpose)
            self.assertEqual({"AGENTS.md", "CLAUDE.md"}, set(bundle.destinations))
            self.assertTrue(bundle.source_asset.is_file())
            self.assertEqual(set(bundle.destinations), set(bundle.markers))

    def test_apply_requires_exact_preview_token_and_rejects_target_drift(self) -> None:
        rejected = self.execute(
            "apply",
            bundles=("development-guidance",),
            plan_token="",
            expect=2,
        )
        self.assertEqual("invalid", rejected["status"])
        preview = self.execute("plan", bundles=("development-guidance",))
        (self.root / "AGENTS.md").write_text("changed after preview\n")
        rejected = self.execute(
            "apply",
            bundles=("development-guidance",),
            plan_token=preview["plan_token"],
            expect=1,
        )
        self.assertEqual("conflict", rejected["status"])
        self.assertFalse(
            (self.root / ".agents/project_management/setup/instruction-state.json").exists()
        )

    def test_apply_rejects_catalog_change_after_preview(self) -> None:
        package = Path(self.temporary.name) / "project-setup"
        shutil.copytree(REPOSITORY_ROOT / "project-setup", package)
        script = package / "runtime/integrate_instructions.py"
        command = [
            sys.executable,
            str(script),
            "plan",
            "--root",
            str(self.root),
            "--destination",
            "AGENTS.md",
            "--bundle",
            "development-guidance",
            "--json",
        ]
        preview_result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, preview_result.returncode, preview_result.stderr)
        preview = json.loads(preview_result.stdout)
        catalog_path = package / "assets/instructions/catalog.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["catalog_version"] = "1.0.1"
        catalog_path.write_text(json.dumps(catalog))
        apply_command = command.copy()
        apply_command[2] = "apply"
        apply_command.extend(("--plan-token", preview["plan_token"]))
        result = subprocess.run(apply_command, capture_output=True, text=True)
        self.assertEqual(1, result.returncode, result.stderr or result.stdout)
        self.assertEqual("conflict", json.loads(result.stdout)["status"])

    def test_hard_crash_recovers_each_instruction_transaction_boundary(self) -> None:
        child_script = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from integrate_instructions import apply_plan, build_plan, load_catalog, source_catalog_paths
from update_core import CoordinatorLock
root = pathlib.Path(sys.argv[2])
script = pathlib.Path(sys.argv[1]) / 'integrate_instructions.py'
catalog_path, capability_path = source_catalog_paths(script)
plan = build_plan(root, load_catalog(catalog_path), capability_path, ('AGENTS.md',), ('development-guidance',), (), auto=False)
def stop(point):
    if point == sys.argv[3]:
        pathlib.Path(sys.argv[4]).write_text('ready')
        while True: time.sleep(1)
with CoordinatorLock(root, '.agents/project_management/setup/.runtime/coordinator.lock'):
    apply_plan(root, plan, fault_injector=stop)
"""
        for boundary in ("after_journal", "after_write_0", "after_write_1", "after_write_2"):
            with self.subTest(boundary=boundary):
                root = Path(self.temporary.name) / boundary
                root.mkdir()
                self.root = root
                preview = self.execute("plan", bundles=("development-guidance",))
                marker = Path(self.temporary.name) / f"{boundary}.ready"
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_script,
                        str(RUNTIME_ROOT),
                        str(root),
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
                applied = self.execute(
                    "apply",
                    bundles=("development-guidance",),
                    plan_token=preview["plan_token"],
                )
                self.assertIn(applied["status"], {"applied", "current"})
                self.assertIn(applied["recovery"], {"rolled_back", "completed"})
                self.assertTrue((root / "AGENTS.md").is_file())
                self.assertTrue(
                    (root / ".agents/project_management/setup/instruction-state.json").is_file()
                )
                self.assertFalse(
                    (root / ".agents/project_management/setup/.runtime/instruction-journal.json").exists()
                )

    def test_instruction_recovery_preserves_post_crash_edit_and_materials(self) -> None:
        preview = self.execute("plan", bundles=("development-guidance",))
        child_script = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from integrate_instructions import apply_plan, build_plan, load_catalog, source_catalog_paths
from update_core import CoordinatorLock
root = pathlib.Path(sys.argv[2])
script = pathlib.Path(sys.argv[1]) / 'integrate_instructions.py'
catalog_path, capability_path = source_catalog_paths(script)
plan = build_plan(root, load_catalog(catalog_path), capability_path, ('AGENTS.md',), ('development-guidance',), (), auto=False)
def crash(point):
    if point == 'after_write_0': os._exit(91)
with CoordinatorLock(root, '.agents/project_management/setup/.runtime/coordinator.lock'):
    apply_plan(root, plan, fault_injector=crash)
"""
        crashed = subprocess.run(
            [sys.executable, "-c", child_script, str(RUNTIME_ROOT), str(self.root)],
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        journal_path = (
            self.root
            / ".agents/project_management/setup/.runtime/instruction-journal.json"
        )
        journal = json.loads(journal_path.read_text())
        edited = self.root / journal["entries"][0]["destination"]
        edited.write_text("post-crash instruction edit\n")
        rejected = self.execute(
            "apply",
            bundles=("development-guidance",),
            plan_token=preview["plan_token"],
            expect=2,
        )
        self.assertEqual("invalid", rejected["status"])
        self.assertIn("post-crash edits", rejected["error"])
        self.assertEqual("post-crash instruction edit\n", edited.read_text())
        self.assertTrue(journal_path.exists())
        for entry in journal["entries"]:
            self.assertTrue((self.root / entry["candidate_path"]).is_file())
            if entry["backup_path"]:
                self.assertTrue((self.root / entry["backup_path"]).is_file())

    def test_instruction_recovery_refuses_corrupt_candidate_without_mutation(self) -> None:
        preview = self.execute("plan", bundles=("development-guidance",))
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from integrate_instructions import apply_plan, build_plan, load_catalog, source_catalog_paths
from update_core import CoordinatorLock
root=pathlib.Path(sys.argv[2]); script=pathlib.Path(sys.argv[1])/'integrate_instructions.py'
catalog_path, capability_path=source_catalog_paths(script)
plan=build_plan(root, load_catalog(catalog_path), capability_path, ('AGENTS.md',), ('development-guidance',), (), auto=False)
def crash(point):
    if point == 'after_journal': os._exit(91)
with CoordinatorLock(root, '.agents/project_management/setup/.runtime/coordinator.lock'):
    apply_plan(root, plan, fault_injector=crash)
""",
                str(RUNTIME_ROOT),
                str(self.root),
            ],
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        journal_path = self.root / ".agents/project_management/setup/.runtime/instruction-journal.json"
        journal = json.loads(journal_path.read_text())
        (self.root / journal["entries"][0]["candidate_path"]).write_text("corrupt\n")
        rejected = self.execute(
            "apply",
            bundles=("development-guidance",),
            plan_token=preview["plan_token"],
            expect=2,
        )
        self.assertIn("candidate is missing or corrupt", rejected["error"])
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertTrue(journal_path.exists())

    def test_manual_bundles_create_explicit_destinations_with_destination_wording(self) -> None:
        payload = self.execute(
            "apply",
            destinations=("AGENTS.md", "CLAUDE.md"),
            bundles=("project-identity-map", "development-guidance"),
        )
        self.assertEqual("applied", payload["status"])
        agents = (self.root / "AGENTS.md").read_text()
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertIn("first navigation layer", agents)
        self.assertIn("Claude's first navigation layer", claude)
        self.assertNotIn("Claude's first navigation layer", agents)
        state = json.loads(
            (self.root / ".agents/project_management/setup/instruction-state.json").read_text()
        )
        self.assertEqual(4, len(state["artifacts"]))
        self.assertTrue(all(item["policy"] == "managed_block" for item in state["artifacts"]))
        self.assertTrue(all(item["bundle_version"] == "1.0.0" for item in state["artifacts"]))

    def test_plan_is_read_only_and_auto_avoids_evidence_free_false_positives(self) -> None:
        before = list(self.root.rglob("*"))
        payload = self.execute("plan", auto=True)
        self.assertEqual("suggestions", payload["status"])
        self.assertEqual([], payload["bundle_ids"])
        self.assertEqual("unchanged", payload["destinations"][0]["action"])
        self.assertEqual(before, list(self.root.rglob("*")))

        (self.root / "README.md").write_text("# Notes only\n")
        payload = self.execute("plan", auto=True)
        self.assertEqual([], payload["bundle_ids"])

    def test_auto_suggestions_use_project_tracker_tests_and_existing_coverage(self) -> None:
        (self.root / "package.json").write_text('{"scripts":{"build":"vite build"}}\n')
        (self.root / "tests").mkdir()
        tracker = self.root / ".agents/project_management/tasks/setup/tracker.json"
        tracker.parent.mkdir(parents=True)
        tracker.write_text('{"schema_version":4}\n')
        (self.root / "AGENTS.md").write_text(
            "We prefer the simplest design that satisfies current requirements.\n"
        )
        payload = self.execute("plan", auto=True)
        suggested = set(payload["bundle_ids"])
        self.assertIn("project-identity-map", suggested)
        self.assertIn("task-tracking", suggested)
        self.assertIn("testing-verification", suggested)
        self.assertNotIn("development-guidance", suggested)
        self.assertNotIn("tools-capability-inventory", suggested)
        self.assertTrue(all(item["evidence"] for item in payload["suggestions"]))

    def test_auto_preview_changes_only_destinations_with_a_real_gap(self) -> None:
        (self.root / "package.json").write_text("{}\n")
        (self.root / "AGENTS.md").write_text(
            "Application development root: `src/`.\n"
            "Prefer the simplest design that satisfies current requirements.\n"
        )
        (self.root / "CLAUDE.md").write_text("# Claude\n")
        payload = self.execute(
            "plan", destinations=("AGENTS.md", "CLAUDE.md"), auto=True
        )
        by_destination = {item["path"]: item for item in payload["destinations"]}
        self.assertEqual("unchanged", by_destination["AGENTS.md"]["action"])
        self.assertEqual("update", by_destination["CLAUDE.md"]["action"])

    def test_unmanaged_bytes_are_preserved_and_apply_is_idempotent(self) -> None:
        unmanaged = b"# Existing\r\n\r\nKeep  trailing spaces.  \r\n"
        (self.root / "AGENTS.md").write_bytes(unmanaged)
        first = self.execute("apply", bundles=("development-guidance",))
        result = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(result.startswith(unmanaged))
        first_state = (
            self.root / ".agents/project_management/setup/instruction-state.json"
        ).read_bytes()
        second = self.execute("apply", bundles=("development-guidance",))
        self.assertEqual("applied", first["status"])
        self.assertEqual("current", second["status"])
        self.assertEqual(result, (self.root / "AGENTS.md").read_bytes())
        self.assertEqual(
            first_state,
            (self.root / ".agents/project_management/setup/instruction-state.json").read_bytes(),
        )

    def test_current_and_coordinator_health_reject_instruction_receipt_damage(self) -> None:
        self.execute("apply", bundles=("development-guidance",))
        instruction_copy = Path(self.temporary.name) / "receipt-update-instructions"
        shutil.copytree(CATALOG_PATH.parent, instruction_copy)
        catalog_raw = json.loads((instruction_copy / "catalog.json").read_text())
        bundle_raw = next(
            item
            for item in catalog_raw["bundles"]
            if item["id"] == "development-guidance"
        )
        bundle_raw["version"] = "1.1.0"
        asset_path = instruction_copy / bundle_raw["source_asset"]
        asset = json.loads(asset_path.read_text())
        asset["version"] = "1.1.0"
        asset["content"]["AGENTS.md"] += "\n- Receipt-gated replacement."
        asset["content"]["CLAUDE.md"] += "\n- Receipt-gated replacement."
        asset_path.write_text(json.dumps(asset, indent=2) + "\n")
        (instruction_copy / "catalog.json").write_text(
            json.dumps(catalog_raw, indent=2) + "\n"
        )
        replacement_catalog = load_catalog(instruction_copy / "catalog.json")
        state = json.loads(
            (self.root / ".agents/project_management/setup/instruction-state.json").read_text()
        )
        receipt = (
            self.root
            / f".agents/project_management/setup/instruction-receipts/{state['last_successful_transaction']}.json"
        )
        original = receipt.read_bytes()
        mutations = {
            "deleted": None,
            "replaced": b'{"transaction_id":"replacement"}\n',
            "tampered": original.replace(b'"catalog_version": "1.0.0"', b'"catalog_version": "9.9.9"'),
        }
        for name, replacement in mutations.items():
            with self.subTest(name=name):
                if replacement is None:
                    receipt.unlink()
                else:
                    receipt.write_bytes(replacement)
                before = {
                    path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                rejected = self.execute(
                    "apply",
                    bundles=("development-guidance",),
                    expect=1,
                )
                self.assertEqual("conflict", rejected["status"])
                replacement_plan = self.direct_plan(
                    ("development-guidance",), catalog=replacement_catalog
                )
                self.assertTrue(replacement_plan.conflicts)
                self.assertEqual(("conflict", None), apply_plan(self.root, replacement_plan))
                after = {
                    path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)
                status_code, status = Coordinator(self.root).status()
                self.assertEqual(EXIT_CONFLICT, status_code)
                self.assertEqual("modified", status["status"])
                self.assertNotEqual(EXIT_SUCCESS, Coordinator(self.root).doctor()[0])
                receipt.write_bytes(original)

    def test_owned_valid_block_updates_without_touching_unmanaged_content(self) -> None:
        (self.root / "AGENTS.md").write_text("before\n\nafter-anchor\n")
        self.execute("apply", bundles=("development-guidance",))
        instruction_copy = Path(self.temporary.name) / "instructions"
        shutil.copytree(CATALOG_PATH.parent, instruction_copy)
        catalog_raw = json.loads((instruction_copy / "catalog.json").read_text())
        bundle_raw = next(
            item for item in catalog_raw["bundles"] if item["id"] == "development-guidance"
        )
        bundle_raw["version"] = "1.1.0"
        (instruction_copy / "catalog.json").write_text(
            json.dumps(catalog_raw, indent=2) + "\n"
        )
        asset_path = instruction_copy / bundle_raw["source_asset"]
        asset = json.loads(asset_path.read_text())
        asset["version"] = "1.1.0"
        asset["content"]["AGENTS.md"] += "\n- Updated managed guidance."
        asset["content"]["CLAUDE.md"] += "\n- Updated managed guidance for Claude."
        asset_path.write_text(json.dumps(asset, indent=2) + "\n")
        updated_catalog = load_catalog(instruction_copy / "catalog.json")
        plan = self.direct_plan(("development-guidance",), catalog=updated_catalog)
        self.assertFalse(plan.conflicts)
        self.assertEqual("applied", apply_plan(self.root, plan)[0])
        content = (self.root / "AGENTS.md").read_text()
        self.assertTrue(content.startswith("before\n\nafter-anchor\n"))
        self.assertIn("Updated managed guidance", content)

    def test_customized_block_aborts_all_destinations_and_writes_report(self) -> None:
        self.execute(
            "apply",
            destinations=("AGENTS.md", "CLAUDE.md"),
            bundles=("development-guidance",),
        )
        agents_path = self.root / "AGENTS.md"
        claude_path = self.root / "CLAUDE.md"
        agents_path.write_text(agents_path.read_text().replace("secure defaults", "custom defaults"))
        agents_before = agents_path.read_bytes()
        claude_before = claude_path.read_bytes()
        payload = self.execute(
            "apply",
            destinations=("AGENTS.md", "CLAUDE.md"),
            bundles=("development-guidance",),
            expect=1,
        )
        self.assertEqual("conflict", payload["status"])
        self.assertEqual(agents_before, agents_path.read_bytes())
        self.assertEqual(claude_before, claude_path.read_bytes())
        self.assertTrue((self.root / payload["conflict_report"]).is_file())
        report_root = (self.root / payload["conflict_report"]).parent
        self.assertTrue(
            (report_root / "candidate-blocks/AGENTS.md/development-guidance.md").is_file()
        )

    def test_malformed_duplicate_reversed_and_nested_markers_fail_loudly(self) -> None:
        cases = {
            "malformed": "<!-- project-setup:development-guidance:start\n",
            "duplicate": (
                "<!-- project-setup:development-guidance:start -->\n"
                "<!-- project-setup:development-guidance:start -->\n"
                "<!-- project-setup:development-guidance:end -->\n"
            ),
            "reversed": (
                "<!-- project-setup:development-guidance:end -->\n"
                "<!-- project-setup:development-guidance:start -->\n"
            ),
            "nested": (
                "<!-- project-setup:development-guidance:start -->\n"
                "<!-- project-setup:testing-verification:start -->\n"
                "<!-- project-setup:testing-verification:end -->\n"
                "<!-- project-setup:development-guidance:end -->\n"
            ),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                (self.root / "AGENTS.md").write_text(content)
                payload = self.execute(
                    "plan", bundles=("development-guidance",), expect=1
                )
                self.assertEqual("conflict", payload["status"])
                self.assertIn(name if name != "reversed" else "end marker", json.dumps(payload).lower())

    def test_dependency_and_conflict_selection_are_validated(self) -> None:
        instruction_copy = Path(self.temporary.name) / "conflict-catalog"
        shutil.copytree(CATALOG_PATH.parent, instruction_copy)
        raw = json.loads((instruction_copy / "catalog.json").read_text())
        documentation = next(
            item for item in raw["bundles"] if item["id"] == "documentation-discipline"
        )
        documentation["dependencies"] = ["project-identity-map"]
        development = next(item for item in raw["bundles"] if item["id"] == "development-guidance")
        development["conflicts"] = ["testing-verification"]
        (instruction_copy / "catalog.json").write_text(json.dumps(raw, indent=2) + "\n")
        conflict_catalog = load_catalog(instruction_copy / "catalog.json")
        with self.assertRaises(InstructionError):
            validate_selection(conflict_catalog, ("documentation-discipline",))
        selected = validate_selection(
            conflict_catalog,
            ("documentation-discipline", "project-identity-map"),
        )
        self.assertEqual(
            ("project-identity-map", "documentation-discipline"), selected
        )
        with self.assertRaises(InstructionError):
            validate_selection(
                conflict_catalog, ("development-guidance", "testing-verification")
            )

    def test_auto_requires_separate_explicit_bundle_apply(self) -> None:
        (self.root / "package.json").write_text("{}\n")
        preview = self.execute("plan", auto=True)
        self.assertIn("project-identity-map", preview["bundle_ids"])
        rejected = self.execute("apply", auto=True, expect=2)
        self.assertEqual("invalid", rejected["status"])
        self.assertFalse((self.root / "AGENTS.md").exists())
        (self.root / "AGENTS.md").write_text("Added after preview.\n")
        applied = self.execute(
            "apply", bundles=tuple(preview["bundle_ids"])
        )
        self.assertEqual("applied", applied["status"])
        self.assertTrue((self.root / "AGENTS.md").read_text().startswith("Added after preview.\n"))

    def test_plan_json_is_canonical_and_deterministic(self) -> None:
        first = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                "--root",
                str(self.root),
                "--destination",
                "AGENTS.md",
                "--bundle",
                "development-guidance",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        second = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                "--root",
                str(self.root),
                "--destination",
                "AGENTS.md",
                "--bundle",
                "development-guidance",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(json.loads(first), sort_keys=True, separators=(",", ":")) + "\n", first)

    def test_capability_inventory_contains_only_explicit_complete_records(self) -> None:
        payload = self.execute(
            "apply",
            bundles=("tools-capability-inventory",),
            capabilities=("project-setup", "task-tracking-setup"),
        )
        self.assertEqual("applied", payload["status"])
        self.execute(
            "apply",
            bundles=("tools-capability-inventory",),
            capabilities=("project-setup",),
        )
        content = (self.root / "AGENTS.md").read_text()
        self.assertIn("Project Setup", content)
        self.assertIn("scope `user_skill`", content)
        self.assertNotIn("Track Project Tasks", content)
        self.assertNotIn("Caveman", content)
        invalid = self.execute(
            "plan",
            bundles=("tools-capability-inventory",),
            capabilities=("caveman",),
            expect=2,
        )
        self.assertEqual("invalid", invalid["status"])

    def test_coordinator_status_and_doctor_understand_instruction_state(self) -> None:
        self.execute("apply", bundles=("development-guidance",))
        status_code, status = Coordinator(self.root).status()
        self.assertEqual(EXIT_SUCCESS, status_code)
        self.assertEqual("not_installed", status["status"])
        self.assertEqual("development-guidance", status["instruction_bundles"][0]["bundle_id"])
        doctor_code, doctor = Coordinator(self.root).doctor()
        self.assertEqual(EXIT_SUCCESS, doctor_code)
        self.assertEqual([], doctor["findings"])

        path = self.root / "AGENTS.md"
        path.write_text(path.read_text().replace("secure defaults", "unsafe defaults"))
        status_code, status = Coordinator(self.root).status()
        self.assertEqual(EXIT_CONFLICT, status_code)
        self.assertEqual("modified", status["status"])

    def test_release_update_aborts_when_instruction_block_has_drifted(self) -> None:
        self.execute("apply", bundles=("development-guidance",))
        agents = self.root / "AGENTS.md"
        agents.write_text(agents.read_text().replace("secure defaults", "custom defaults"))
        before = agents.read_bytes()
        index = json.loads((REPOSITORY_ROOT / "releases/index.json").read_text())
        release = index["releases"][0]
        version = release["version"]
        manifest_sha = release["manifest_sha256"]
        coordinator = Coordinator(self.root)
        plan_code, plan = coordinator.plan_update(
            REPOSITORY_ROOT, version, manifest_sha
        )
        self.assertEqual(EXIT_CONFLICT, plan_code)
        self.assertTrue(
            any(item["artifact_id"].startswith("instruction:") for item in plan["conflicts"])
        )
        update_code, update = coordinator.update(
            REPOSITORY_ROOT, version, manifest_sha, plan["plan_token"]
        )
        self.assertEqual(EXIT_CONFLICT, update_code)
        self.assertEqual(before, agents.read_bytes())
        self.assertFalse(
            (self.root / ".agents/project_management/setup/coordinator.py").exists()
        )
        self.assertTrue((self.root / update["conflict_report"]).is_file())

    def test_coordinator_understands_tracker_installed_instruction_block(self) -> None:
        command = [
            sys.executable,
            str(REPOSITORY_ROOT / "task-tracking-setup/scripts/setup_project.py"),
            "--root",
            str(self.root),
            "--instruction-file",
            "AGENTS.md",
            "--json",
        ]
        preview_result = subprocess.run(
            [*command, "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, preview_result.returncode, preview_result.stderr or preview_result.stdout)
        tracker_plan_token = json.loads(preview_result.stdout)["plan_token"]
        result = subprocess.run(
            [*command, "--plan-token", tracker_plan_token],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        status_code, status = Coordinator(self.root).status()
        self.assertEqual(EXIT_SUCCESS, status_code)
        self.assertEqual("task-tracking", status["instruction_bundles"][0]["bundle_id"])
        self.assertEqual(EXIT_SUCCESS, Coordinator(self.root).doctor()[0])

    def test_symlinked_destination_and_root_are_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside.md"
        outside.write_text("outside\n")
        (self.root / "AGENTS.md").symlink_to(outside)
        payload = self.execute(
            "plan", bundles=("development-guidance",), expect=2
        )
        self.assertEqual("invalid", payload["status"])
        self.assertEqual("outside\n", outside.read_text())

        linked_root = Path(self.temporary.name) / "linked-root"
        linked_root.symlink_to(self.root, target_is_directory=True)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                "--root",
                str(linked_root),
                "--destination",
                "CLAUDE.md",
                "--bundle",
                "development-guidance",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)

    def test_tracker_compatibility_block_matches_canonical_task_bundle(self) -> None:
        task_bundle = next(
            bundle for bundle in self.catalog.bundles if bundle.bundle_id == "task-tracking"
        )
        self.assertEqual(
            render_block(task_bundle, "AGENTS.md", ()) + b"\n",
            (REPOSITORY_ROOT / "task-tracking-setup/assets/AGENTS.block.md").read_bytes(),
        )
        self.assertEqual(
            render_block(task_bundle, "CLAUDE.md", ()) + b"\n",
            (REPOSITORY_ROOT / "task-tracking-setup/assets/CLAUDE.block.md").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()

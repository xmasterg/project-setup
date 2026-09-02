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

from install_transaction import (  # noqa: E402
    InjectedInstallFault,
    InstallLock,
    InstallLockBusy,
    InstallSafetyError,
)
from setup_project import execute_install, plan_install  # noqa: E402


def snapshot(root: Path, *, ignore_runtime: bool = False) -> dict[str, bytes]:
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(root).as_posix()
        if ignore_runtime and relative.startswith(".agents/project_management/setup/.runtime/"):
            continue
        result[relative] = path.read_bytes()
    return result


class TrackerInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tracker-installer-tests-")
        self.root = Path(self.temporary.name) / "target"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def preview(self, instruction_files: tuple[str, ...] = ()) -> dict:
        code, payload = execute_install(
            TRACKER_ROOT, self.root, instruction_files, dry_run=True
        )
        self.assertIn(code, {0, 1})
        return payload

    def apply(self, instruction_files: tuple[str, ...] = ()) -> tuple[int, dict]:
        preview = self.preview(instruction_files)
        return execute_install(
            TRACKER_ROOT,
            self.root,
            instruction_files,
            dry_run=False,
            plan_token=preview["plan_token"],
        )

    def apply_with_archive(self, archive_checksum: str) -> tuple[int, dict]:
        code, preview = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=True,
            archive_checksum=archive_checksum,
        )
        self.assertEqual(0, code)
        return execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
            archive_checksum=archive_checksum,
        )

    def crash_apply(self, point: str, plan_token: str) -> subprocess.CompletedProcess:
        script = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from setup_project import execute_install
def crash(point):
    if point == sys.argv[4]:
        os._exit(91)
execute_install(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), (), dry_run=False, plan_token=sys.argv[5], fault_injector=crash)
"""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(TRACKER_ROOT / "scripts"),
                str(TRACKER_ROOT),
                str(self.root),
                point,
                plan_token,
            ],
            check=False,
        )

    def test_dry_run_is_read_only_and_json_plan_is_deterministic(self) -> None:
        before = snapshot(self.root)
        first_code, first = execute_install(
            TRACKER_ROOT, self.root, (), dry_run=True
        )
        second_code, second = execute_install(
            TRACKER_ROOT, self.root, (), dry_run=True
        )
        self.assertEqual(0, first_code)
        self.assertEqual(first_code, second_code)
        self.assertEqual(first, second)
        self.assertEqual(before, snapshot(self.root))

    def test_success_idempotence_and_unknown_file_preservation(self) -> None:
        unknown = self.root / "unknown.txt"
        unknown.write_text("preserve\n")
        code, payload = self.apply(("AGENTS.md",))
        self.assertEqual(0, code)
        self.assertEqual("installed", payload["status"])
        installed = snapshot(self.root)
        code, payload = self.apply(("AGENTS.md",))
        self.assertEqual(0, code)
        self.assertEqual("current", payload["status"])
        self.assertEqual(installed, snapshot(self.root))
        self.assertEqual("preserve\n", unknown.read_text())

    def test_current_and_coordinator_health_reject_tracker_receipt_damage(self) -> None:
        self.assertEqual(0, self.apply()[0])
        copied_skill = Path(self.temporary.name) / "receipt-replacement-tracker"
        shutil.copytree(TRACKER_ROOT, copied_skill)
        replacement_source = (
            copied_skill / "assets/project/tasks/setup/task_board/task_board.js"
        )
        replacement_source.write_text(
            replacement_source.read_text() + "\n/* receipt-gated replacement */\n"
        )
        replacement_manifest_path = copied_skill / "assets/install-manifest.json"
        replacement_manifest = json.loads(replacement_manifest_path.read_text())
        replacement_artifact = next(
            item
            for item in replacement_manifest["artifacts"]
            if item["id"] == "board-application"
        )
        replacement_artifact["source_sha256"] = hashlib.sha256(
            replacement_source.read_bytes()
        ).hexdigest()
        replacement_manifest_path.write_text(
            json.dumps(replacement_manifest, indent=2) + "\n"
        )
        legacy = self.root / ".agents/tasks.json"
        legacy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": "Receipt retirement",
                    "updated_at": "",
                    "tasks": [],
                }
            )
            + "\n"
        )
        with tempfile.TemporaryDirectory(prefix="tracker-receipt-control-") as temporary:
            control = plan_install(
                copied_skill,
                self.root,
                (),
                Path(temporary),
            )
        self.assertIn("write", {operation.action for operation in control.operations})
        self.assertIn("delete", {operation.action for operation in control.operations})
        state = json.loads(
            (self.root / ".agents/project_management/setup/tracker-install-state.json").read_text()
        )
        receipt = (
            self.root
            / f".agents/project_management/setup/receipts/{state['last_successful_transaction']}.json"
        )
        original = receipt.read_bytes()
        receipt_payload = json.loads(original)
        tampered_payload = dict(receipt_payload)
        tampered_payload["operations"] = [*receipt_payload["operations"]]
        tampered_payload["operations"][0] = dict(tampered_payload["operations"][0])
        tampered_payload["operations"][0]["after_sha256"] = "0" * 64
        mutations = {
            "deleted": None,
            "replaced": b'{"transaction_id":"replacement"}\n',
            "tampered": (json.dumps(tampered_payload, indent=2, sort_keys=True) + "\n").encode(),
        }
        runtime_root = REPOSITORY_ROOT / "project-setup/runtime"
        sys.path.insert(0, str(runtime_root))
        from coordinator import Coordinator, EXIT_CONFLICT, EXIT_SUCCESS

        for name, replacement in mutations.items():
            with self.subTest(name=name):
                if replacement is None:
                    receipt.unlink()
                else:
                    receipt.write_bytes(replacement)
                before = snapshot(self.root)
                with tempfile.TemporaryDirectory(
                    prefix="tracker-receipt-blocked-"
                ) as temporary:
                    blocked = plan_install(
                        copied_skill,
                        self.root,
                        (),
                        Path(temporary),
                    )
                self.assertTrue(blocked.conflicts)
                code, payload = execute_install(
                    copied_skill,
                    self.root,
                    (),
                    dry_run=False,
                    plan_token=blocked.plan_token,
                )
                self.assertEqual(1, code)
                self.assertEqual("conflict", payload["status"])
                self.assertEqual(before, snapshot(self.root))
                status_code, status = Coordinator(self.root).status()
                self.assertEqual(EXIT_CONFLICT, status_code)
                self.assertEqual("modified", status["tracker"]["status"])
                self.assertNotEqual(EXIT_SUCCESS, Coordinator(self.root).doctor()[0])
                receipt.write_bytes(original)

    def test_malformed_block_conflict_preserves_project_files(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text("keep\n<!-- task-tracker:start -->\nbroken\n")
        before = snapshot(self.root)
        code, payload = self.apply(("AGENTS.md",))
        self.assertEqual(1, code)
        self.assertEqual("conflict", payload["status"])
        self.assertEqual(before, snapshot(self.root, ignore_runtime=True))
        self.assertTrue((self.root / payload["conflict_report"]).is_file())

    def test_customized_managed_runtime_aborts_whole_install(self) -> None:
        self.apply()
        runtime = self.root / ".agents/project_management/tasks/setup/scripts/render_tasks.py"
        runtime.write_text("custom\n")
        board = self.root / ".agents/project_management/tasks/task_tracking/open_task_board.html"
        board_before = board.read_bytes()
        code, payload = self.apply()
        self.assertEqual(1, code)
        self.assertEqual(board_before, board.read_bytes())
        self.assertEqual("custom\n", runtime.read_text())
        self.assertTrue((self.root / payload["conflict_report"]).is_file())

    def test_symlinked_destination_is_rejected(self) -> None:
        target = self.root / ".agents/project_management/tasks/setup/scripts"
        target.mkdir(parents=True)
        (target / "render_tasks.py").symlink_to(self.root / "outside.py")
        with self.assertRaises(InstallSafetyError):
            execute_install(TRACKER_ROOT, self.root, (), dry_run=True)

    def test_invalid_candidate_failure_leaves_target_unchanged(self) -> None:
        backlog = self.root / ".agents/project_management/tasks/task_tracking/backlog.json"
        backlog.parent.mkdir(parents=True)
        backlog.write_text("{invalid")
        before = snapshot(self.root)
        with self.assertRaises(InstallSafetyError):
            self.apply()
        self.assertEqual(before, snapshot(self.root))

    def test_fault_recovery(self) -> None:
        def interrupt(point: str) -> None:
            if point == "after_operation:1":
                raise InjectedInstallFault(point)

        preview = self.preview()
        with self.assertRaises(InjectedInstallFault):
            execute_install(
                TRACKER_ROOT,
                self.root,
                (),
                dry_run=False,
                plan_token=preview["plan_token"],
                fault_injector=interrupt,
            )
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(0, code)
        self.assertEqual("restored", payload["recovery"]["status"])
        self.assertTrue(
            (self.root / ".agents/project_management/tasks/task_tracking/open_task_board.html").is_file()
        )

    def test_concurrent_lock_is_rejected(self) -> None:
        preview = self.preview()
        with InstallLock(self.root):
            with self.assertRaises(InstallLockBusy):
                execute_install(
                    TRACKER_ROOT,
                    self.root,
                    (),
                    dry_run=False,
                    plan_token=preview["plan_token"],
                )

    def test_apply_requires_preview_token_and_rejects_target_drift(self) -> None:
        with self.assertRaises(InstallSafetyError):
            execute_install(TRACKER_ROOT, self.root, (), dry_run=False)
        preview = self.preview()
        changed_target = (
            self.root / ".agents/project_management/tasks/task_tracking/open_task_board.html"
        )
        changed_target.parent.mkdir(parents=True)
        changed_target.write_text("changed after preview\n")
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(1, code)
        self.assertEqual("conflict", payload["status"])
        self.assertFalse(
            (self.root / ".agents/project_management/tasks/setup/tracker.json").exists()
        )

    def test_manifest_source_checksum_and_unknown_field_drift_are_rejected(self) -> None:
        copied_skill = Path(self.temporary.name) / "copied-tracker"
        shutil.copytree(TRACKER_ROOT, copied_skill)
        (copied_skill / "scripts/render_tasks.py").write_text("changed\n")
        with self.assertRaisesRegex(InstallSafetyError, "source checksum drifted"):
            execute_install(copied_skill, self.root, (), dry_run=True)
        shutil.copy2(TRACKER_ROOT / "scripts/render_tasks.py", copied_skill / "scripts/render_tasks.py")
        manifest_path = copied_skill / "assets/install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"][0]["unrecognized"] = True
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(InstallSafetyError, "unknown fields"):
            execute_install(copied_skill, self.root, (), dry_run=True)

    def test_manifest_rejects_missing_or_drifted_mapping_policy_block_and_dynamic_rules(self) -> None:
        def mutate_missing_checksum(manifest: dict) -> None:
            manifest["artifacts"][0].pop("checksum")

        def mutate_policy(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "runtime-render")
            artifact["policy"] = "seed"

        def mutate_checksum_mode(manifest: dict) -> None:
            manifest["artifacts"][0]["checksum"]["mode"] = "managed_block"

        def mutate_block(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "instruction-block")
            artifact["block"]["end_marker"] = "<!-- changed:end -->"

        def mutate_mapping(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "instruction-block")
            artifact["destination_sources"].pop("CLAUDE.md")

        def mutate_dynamic_rules(manifest: dict) -> None:
            manifest["dynamic_artifact_rules"].pop()

        def mutate_missing_artifact(manifest: dict) -> None:
            manifest["artifacts"].pop()

        def mutate_additional_artifact(manifest: dict) -> None:
            artifact = dict(manifest["artifacts"][0])
            artifact["id"] = "unsupported-artifact"
            artifact["target"] = ".agents/unsupported.json"
            manifest["artifacts"].append(artifact)

        def mutate_relabel(manifest: dict) -> None:
            manifest["artifacts"][0]["id"] = "task-data-renamed"

        def mutate_source(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "board-styles")
            artifact["source"] = "assets/project/tasks/setup/task_board/task_board.js"
            artifact["source_sha256"] = next(
                item for item in manifest["artifacts"] if item["id"] == "board-application"
            )["source_sha256"]

        def mutate_target(manifest: dict) -> None:
            manifest["artifacts"][0]["target"] = ".agents/remapped-backlog.json"

        def mutate_action(manifest: dict) -> None:
            manifest["artifacts"][0]["action"] = "retire"
            manifest["artifacts"][0]["source"] = None
            manifest["artifacts"][0].pop("source_sha256")
            manifest["artifacts"][0]["checksum"]["mode"] = "recorded_or_accepted_legacy"

        def mutate_generator(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "board-data")
            artifact["generator"] = "runtime-task-store"

        def mutate_replacement(manifest: dict) -> None:
            artifact = next(item for item in manifest["artifacts"] if item["id"] == "board-shell")
            artifact["replaces_artifact_id"] = "old-board-shell"

        cases = {
            "missing checksum": mutate_missing_checksum,
            "policy": mutate_policy,
            "checksum mode": mutate_checksum_mode,
            "block": mutate_block,
            "mapping": mutate_mapping,
            "dynamic rules": mutate_dynamic_rules,
            "missing artifact": mutate_missing_artifact,
            "additional artifact": mutate_additional_artifact,
            "relabeled artifact": mutate_relabel,
            "source mapping": mutate_source,
            "target mapping": mutate_target,
            "action": mutate_action,
            "generator": mutate_generator,
            "replacement": mutate_replacement,
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                copied_skill = Path(self.temporary.name) / f"manifest-{name.replace(' ', '-')}"
                shutil.copytree(TRACKER_ROOT, copied_skill)
                manifest_path = copied_skill / "assets/install-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                mutation(manifest)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(InstallSafetyError):
                    execute_install(copied_skill, self.root, (), dry_run=True)

    def test_manifest_binds_exact_migration_rollback_and_retirement_contracts(self) -> None:
        def remove_migration(manifest: dict) -> None:
            manifest["migrations"].pop()

        def add_migration(manifest: dict) -> None:
            manifest["migrations"].append(
                {"id": "invented-migration", "rollback": "restore_backup"}
            )

        def rename_migration(manifest: dict) -> None:
            manifest["migrations"][0]["id"] = "renamed-migration"

        def duplicate_migration(manifest: dict) -> None:
            manifest["migrations"][1] = dict(manifest["migrations"][0])

        def reorder_migrations(manifest: dict) -> None:
            manifest["migrations"][0], manifest["migrations"][1] = (
                manifest["migrations"][1],
                manifest["migrations"][0],
            )

        def mutate_migration_rollback(manifest: dict) -> None:
            manifest["migrations"][0]["rollback"] = "not_applicable"

        def mutate_rollback_declaration(manifest: dict) -> None:
            manifest["rollback"]["declaration"] = "Almost the same."

        def mutate_retirement(manifest: dict) -> None:
            manifest["retirement_policy"] += " Changed."

        for name, mutation in {
            "missing": remove_migration,
            "added": add_migration,
            "renamed": rename_migration,
            "duplicated": duplicate_migration,
            "reordered": reorder_migrations,
            "migration-rollback": mutate_migration_rollback,
            "rollback-declaration": mutate_rollback_declaration,
            "retirement": mutate_retirement,
        }.items():
            with self.subTest(name=name):
                copied_skill = Path(self.temporary.name) / f"contract-{name}"
                shutil.copytree(TRACKER_ROOT, copied_skill)
                manifest_path = copied_skill / "assets/install-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                mutation(manifest)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(InstallSafetyError):
                    execute_install(copied_skill, self.root, (), dry_run=True)

    def test_state_only_provenance_adoption_has_actual_receipt_and_is_idempotent(self) -> None:
        self.assertEqual(0, self.apply()[0])
        original_state_path = (
            self.root / ".agents/project_management/setup/tracker-install-state.json"
        )
        original_transaction = json.loads(original_state_path.read_text())[
            "last_successful_transaction"
        ]

        vendored_checksum = "a" * 64
        code, preview = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=True,
            archive_checksum=vendored_checksum,
        )
        self.assertEqual(0, code)
        self.assertEqual([], preview["operations"])
        code, applied = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
            archive_checksum=vendored_checksum,
        )
        self.assertEqual(0, code)
        self.assertEqual("recorded", applied["status"])
        state = json.loads(original_state_path.read_text())
        transaction = state["last_successful_transaction"]
        self.assertNotEqual(original_transaction, transaction)
        self.assertEqual(transaction, applied["transaction_id"])
        receipt = json.loads(
            (
                self.root
                / f".agents/project_management/setup/receipts/{transaction}.json"
            ).read_text()
        )
        self.assertEqual(transaction, receipt["transaction_id"])
        follow_up = self.apply_with_archive(vendored_checksum)
        self.assertEqual("current", follow_up[1]["status"])
        self.assertEqual(transaction, json.loads(original_state_path.read_text())["last_successful_transaction"])

    def test_missing_state_adoption_records_new_transaction_and_follow_up_is_current(self) -> None:
        self.assertEqual(0, self.apply()[0])
        state_path = self.root / ".agents/project_management/setup/tracker-install-state.json"
        state_path.unlink()
        code, payload = self.apply()
        self.assertEqual(0, code)
        self.assertEqual("recorded", payload["status"])
        state = json.loads(state_path.read_text())
        self.assertEqual(payload["transaction_id"], state["last_successful_transaction"])
        self.assertTrue(
            (
                self.root
                / f".agents/project_management/setup/receipts/{payload['transaction_id']}.json"
            ).is_file()
        )
        self.assertEqual("current", self.apply()[1]["status"])

    def test_apply_rejects_source_change_after_approved_preview(self) -> None:
        copied_skill = Path(self.temporary.name) / "source-drift-tracker"
        shutil.copytree(TRACKER_ROOT, copied_skill)
        code, preview = execute_install(copied_skill, self.root, (), dry_run=True)
        self.assertEqual(0, code)
        source = copied_skill / "assets/project/tasks/setup/task_board/task_board.js"
        source.write_text(source.read_text() + "\n/* source changed after preview */\n")
        manifest_path = copied_skill / "assets/install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        artifact = next(item for item in manifest["artifacts"] if item["id"] == "board-application")
        artifact["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        code, payload = execute_install(
            copied_skill,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(1, code)
        self.assertEqual("conflict", payload["status"])

    def test_v2_and_v3_unknown_metadata_is_namespaced(self) -> None:
        v2_root = self.root / ".agents/tasks"
        v2_root.mkdir(parents=True)
        files = {}
        for status in ("backlog", "ready", "in_progress", "blocked", "completed"):
            filename = f"{status}.json"
            files[status] = filename
            (v2_root / filename).write_text('{"tasks": []}\n')
        (v2_root / "index.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project": "Legacy V2",
                    "files": files,
                    "custom_owner": {"team": "platform"},
                }
            )
        )
        self.assertEqual(0, self.apply()[0])
        tracker_path = self.root / ".agents/project_management/tasks/setup/tracker.json"
        tracker = json.loads(tracker_path.read_text())
        self.assertEqual(
            {"custom_owner": {"team": "platform"}},
            tracker["migration_metadata"]["legacy_v2_index"],
        )

        other = Path(self.temporary.name) / "v3-target"
        other.mkdir()
        self.root = other
        tracking = other / "tasks/task_tracking"
        tracking.mkdir(parents=True)
        for filename in ("backlog.json", "ready.json", "in-progress.json", "blocked.json"):
            (tracking / filename).write_text('{"tasks": []}\n')
        setup = other / "tasks/setup"
        setup.mkdir(parents=True)
        (setup / "tracker.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "project": "Legacy V3",
                    "updated_at": "",
                    "custom_labels": ["alpha", "beta"],
                }
            )
        )
        self.assertEqual(0, self.apply()[0])
        migrated = json.loads(
            (other / ".agents/project_management/tasks/setup/tracker.json").read_text()
        )
        self.assertEqual(
            {"custom_labels": ["alpha", "beta"]},
            migrated["migration_metadata"]["legacy_v3_tracker"],
        )

    def test_empty_v1_unknown_envelope_metadata_is_namespaced(self) -> None:
        legacy = self.root / ".agents/tasks.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": "Empty legacy tracker",
                    "updated_at": "",
                    "tasks": [],
                    "custom_owner": {"team": "platform"},
                }
            )
        )
        self.assertEqual(0, self.apply()[0])
        tracker = json.loads(
            (
                self.root
                / ".agents/project_management/tasks/setup/tracker.json"
            ).read_text()
        )
        self.assertEqual(
            {"custom_owner": {"team": "platform"}},
            tracker["migration_metadata"]["legacy_v1_empty_envelope"],
        )

    def test_hard_crash_before_state_write_rolls_back_and_retries(self) -> None:
        preview = self.preview()
        crashed = self.crash_apply("before_state", preview["plan_token"])
        self.assertEqual(91, crashed.returncode)
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(0, code)
        self.assertEqual("installed", payload["status"])
        self.assertEqual("restored", payload["recovery"]["status"])
        self.assertFalse(
            (self.root / ".agents/project_management/setup/.runtime/tracker-install-journal.json").exists()
        )

    def test_hard_crash_after_state_write_recognizes_committed_install(self) -> None:
        preview = self.preview()
        crashed = self.crash_apply("after_state", preview["plan_token"])
        self.assertEqual(91, crashed.returncode)
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(0, code)
        self.assertEqual("current", payload["status"])
        self.assertEqual("completed", payload["recovery"]["status"])
        self.assertFalse(
            (self.root / ".agents/project_management/setup/.runtime/tracker-install-journal.json").exists()
        )

    def test_hard_crash_after_transaction_cleanup_finishes_journal_cleanup(self) -> None:
        preview = self.preview()
        crashed = self.crash_apply("after_cleanup", preview["plan_token"])
        self.assertEqual(91, crashed.returncode)
        journal_path = (
            self.root / ".agents/project_management/setup/.runtime/tracker-install-journal.json"
        )
        self.assertFalse(journal_path.exists())
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(0, code)
        self.assertEqual("current", payload["status"])
        self.assertIsNone(payload["recovery"])
        self.assertFalse(journal_path.exists())

    def test_crash_recovery_refuses_to_overwrite_post_crash_edit(self) -> None:
        preview = self.preview()
        crashed = self.crash_apply("after_operation:1", preview["plan_token"])
        self.assertEqual(91, crashed.returncode)
        journal_path = (
            self.root / ".agents/project_management/setup/.runtime/tracker-install-journal.json"
        )
        journal = json.loads(journal_path.read_text())
        edited_target = self.root / journal["operations"][0]["target"]
        edited_target.write_text("post-crash user edit\n")
        with self.assertRaisesRegex(InstallSafetyError, "changed after the crash"):
            execute_install(
                TRACKER_ROOT,
                self.root,
                (),
                dry_run=False,
                plan_token=preview["plan_token"],
            )
        self.assertEqual("post-crash user edit\n", edited_target.read_text())
        self.assertTrue(journal_path.exists())

    def test_crash_recovery_refuses_corrupt_candidate_without_mutation(self) -> None:
        preview = self.preview()
        crashed = self.crash_apply("after_journal", preview["plan_token"])
        self.assertEqual(91, crashed.returncode)
        journal_path = (
            self.root / ".agents/project_management/setup/.runtime/tracker-install-journal.json"
        )
        journal = json.loads(journal_path.read_text())
        candidate = self.root / journal["operations"][0]["candidate"]
        candidate.write_text("corrupt\n")
        with self.assertRaisesRegex(InstallSafetyError, "candidate changed after the crash"):
            execute_install(
                TRACKER_ROOT,
                self.root,
                (),
                dry_run=False,
                plan_token=preview["plan_token"],
            )
        self.assertFalse((self.root / journal["operations"][0]["target"]).exists())
        self.assertTrue(journal_path.exists())

    def test_sigkill_after_journal_releases_lock_and_allows_recovery(self) -> None:
        preview = self.preview()
        marker = Path(self.temporary.name) / "tracker-journal-ready"
        script = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from setup_project import execute_install
def stop(point):
    if point == 'after_journal':
        pathlib.Path(sys.argv[4]).write_text('ready')
        while True: time.sleep(1)
execute_install(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), (), dry_run=False, plan_token=sys.argv[5], fault_injector=stop)
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(TRACKER_ROOT / "scripts"),
                str(TRACKER_ROOT),
                str(self.root),
                str(marker),
                preview["plan_token"],
            ]
        )
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        os.kill(child.pid, 9)
        child.wait(timeout=5)
        code, payload = execute_install(
            TRACKER_ROOT,
            self.root,
            (),
            dry_run=False,
            plan_token=preview["plan_token"],
        )
        self.assertEqual(0, code)
        self.assertEqual("restored", payload["recovery"]["status"])


if __name__ == "__main__":
    unittest.main()

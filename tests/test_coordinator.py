from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPOSITORY_ROOT / "project-setup/runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from coordinator import (  # noqa: E402
    Coordinator,
    EXIT_CONFLICT,
    EXIT_INVALID,
    EXIT_SUCCESS,
)
from update_core import (  # noqa: E402
    CoordinatorLock,
    FaultInjected,
    LockBusy,
    ValidationError,
    parse_project_relative_path,
    parse_release_manifest,
)


POLICIES = {
    "managed": "managed",
    "managed_block": "managed block",
    "seed": "seed",
    "user_data": "user data",
    "generated": "generated",
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot(root: Path, *, ignore_runtime: bool = False) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ignore_runtime and relative.startswith(".agents/project_management/setup/.runtime/"):
            continue
        if path.is_symlink():
            result[relative] = f"symlink:{path.readlink()}".encode()
        elif path.is_dir():
            result[f"{relative}/"] = b"directory"
        elif path.is_file():
            result[relative] = path.read_bytes()
    return result


class ReleaseFixture:
    def __init__(self, root: Path) -> None:
        self.root = root

    def add_release(
        self,
        version: str,
        files: dict[str, bytes],
        *,
        managed_block: bool = False,
    ) -> str:
        artifacts = []
        for artifact_id, payload in sorted(files.items()):
            source = f"artifacts/{version}/{artifact_id}.txt"
            source_path = self.root / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(payload)
            artifact = {
                "id": artifact_id,
                "component": "fixture",
                "source": source,
                "target": f"managed/{artifact_id}.txt",
                "policy": "managed",
                "sha256": digest(payload),
                "action": "install",
            }
            if managed_block and artifact_id == "block":
                artifact["target"] = "AGENTS.md"
                artifact["policy"] = "managed_block"
                artifact["block"] = {
                    "start_marker": "<!-- fixture:start -->",
                    "end_marker": "<!-- fixture:end -->",
                }
            artifacts.append(artifact)
        manifest = {
            "manifest_schema_version": 1,
            "bundle_version": version,
            "release_status": "unreleased",
            "source_identity": {"kind": "verified_local", "immutable_commit": None},
            "component_versions": {"fixture": version},
            "artifact_policies": POLICIES,
            "migrations": [
                {"id": f"fixture-{version}", "component": "fixture", "rollback": "restore_backup"}
            ],
            "rollback": {"mode": "one_release", "declaration": "Restore backup."},
            "artifacts": artifacts,
        }
        manifest_path = self.root / f"releases/{version}/install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(payload)
        return digest(payload)

    def add_retirement_release(self, version: str, artifact_id: str, target: str) -> str:
        manifest = {
            "manifest_schema_version": 1,
            "bundle_version": version,
            "release_status": "unreleased",
            "source_identity": {"kind": "verified_local", "immutable_commit": None},
            "component_versions": {"fixture": version},
            "artifact_policies": POLICIES,
            "migrations": [
                {"id": f"retire-{artifact_id}", "component": "fixture", "rollback": "restore_backup"}
            ],
            "rollback": {"mode": "one_release", "declaration": "Restore backup."},
            "artifacts": [
                {
                    "id": artifact_id,
                    "component": "fixture",
                    "source": None,
                    "target": target,
                    "policy": "managed",
                    "sha256": None,
                    "action": "retire",
                }
            ],
        }
        manifest_path = self.root / f"releases/{version}/install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(payload)
        return digest(payload)


class CoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coordinator-tests-")
        base = Path(self.temporary.name)
        self.target = base / "target"
        self.source = base / "source"
        self.target.mkdir()
        self.source.mkdir()
        self.releases = ReleaseFixture(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def apply_update(
        self,
        coordinator: Coordinator,
        version: str,
        manifest_sha256: str,
    ) -> tuple[int, dict]:
        _, preview = coordinator.plan_update(self.source, version, manifest_sha256)
        return coordinator.update(
            self.source,
            version,
            manifest_sha256,
            preview["plan_token"],
        )

    def test_manifest_and_path_validation(self) -> None:
        for invalid in ("", ".", "../x", "a/../b", "/tmp/x", "a//b", "a\x00b", "C:/x"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                parse_project_relative_path(invalid, label="test")
        checksum = "0" * 64
        manifest = {
            "manifest_schema_version": 1,
            "bundle_version": "1.0.0-dev.0",
            "release_status": "unreleased",
            "component_versions": {"fixture": "1"},
            "artifact_policies": POLICIES,
            "migrations": [],
            "rollback": {"mode": "one_release", "declaration": "restore"},
            "artifacts": [
                {
                    "id": "one",
                    "component": "fixture",
                    "source": "one",
                    "target": "Path.txt",
                    "policy": "managed",
                    "sha256": checksum,
                },
                {
                    "id": "two",
                    "component": "fixture",
                    "source": "two",
                    "target": "path.txt",
                    "policy": "managed",
                    "sha256": checksum,
                },
            ],
        }
        with self.assertRaises(ValidationError):
            parse_release_manifest(manifest)

    def test_runtime_rejects_malformed_stable_release_identity(self) -> None:
        commit = "a" * 40
        manifest = {
            "manifest_schema_version": 1,
            "bundle_version": "1.0.0",
            "release_status": "stable",
            "published_tag": "v1.0.0",
            "immutable_commit": commit,
            "source_identity": {
                "kind": "verified_local",
                "published_tag": "v1.0.0",
                "immutable_commit": commit,
                "source_checkout_commit": commit,
            },
            "component_versions": {"fixture": "1.0.0"},
            "artifact_policies": POLICIES,
            "migrations": [],
            "rollback": {"mode": "one_release", "declaration": "restore"},
            "artifacts": [],
        }
        parse_release_manifest(manifest)
        mutations = {
            "short commit": lambda value: value.update({"immutable_commit": "abc"}),
            "uppercase commit": lambda value: value.update(
                {"immutable_commit": commit.upper()}
            ),
            "placeholder tag": lambda value: value.update(
                {"published_tag": "RELEASE_TAG"}
            ),
            "source mismatch": lambda value: value["source_identity"].update(
                {"source_checkout_commit": "b" * 40}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = json.loads(json.dumps(manifest))
                mutate(changed)
                with self.assertRaises(ValidationError):
                    parse_release_manifest(changed)

    def test_release_index_rejects_every_manifest_identity_and_path_mismatch(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        manifest = json.loads(
            (self.source / f"releases/{version}/install-manifest.json").read_text()
        )
        source_identity = manifest["source_identity"]
        entry = {
            "version": version,
            "status": manifest["release_status"],
            "published_tag": manifest.get("published_tag"),
            "immutable_commit": manifest.get("immutable_commit"),
            "source_checkout_commit": source_identity.get("source_checkout_commit"),
            "manifest_sha256": manifest_sha,
            "manifest_path": f"releases/{version}/install-manifest.json",
        }
        index_path = self.source / "releases/index.json"

        def write_index(release: dict) -> None:
            index_path.write_text(
                json.dumps(
                    {
                        "release_index_schema_version": 1,
                        "current_source_version": version,
                        "stable_version": None,
                        "releases": [release],
                    }
                )
            )

        write_index(entry)
        self.assertEqual(
            EXIT_SUCCESS,
            Coordinator(self.target).plan_update(self.source, version, manifest_sha)[0],
        )
        mutations = {
            "version": "9.9.9",
            "status": "stable",
            "published_tag": "v1.0.0",
            "immutable_commit": "b" * 40,
            "source_checkout_commit": "b" * 40,
            "manifest_sha256": "b" * 64,
            "manifest_path": f"releases/{version}/other.json",
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                changed = dict(entry)
                changed[field] = replacement
                write_index(changed)
                with self.assertRaises(ValidationError):
                    Coordinator(self.target).plan_update(self.source, version, manifest_sha)

    def test_source_and_destination_symlinks_are_rejected(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        source_file = self.source / f"artifacts/{version}/one.txt"
        real_file = source_file.with_name("real.txt")
        source_file.replace(real_file)
        source_file.symlink_to(real_file)
        with self.assertRaises(ValidationError):
            Coordinator(self.target).plan_update(self.source, version, manifest_sha)

        source_file.unlink()
        source_file.write_bytes(b"one\n")
        source_directory = source_file.parent
        real_source_directory = source_directory.with_name(f"{version}-real")
        source_directory.replace(real_source_directory)
        source_directory.symlink_to(real_source_directory, target_is_directory=True)
        with self.assertRaises(ValidationError):
            Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        source_directory.unlink()
        real_source_directory.replace(source_directory)

        (self.target / "managed").mkdir()
        (self.target / "managed/one.txt").symlink_to(self.target / "elsewhere")
        exit_code, payload = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        self.assertEqual(EXIT_CONFLICT, exit_code)
        self.assertEqual("conflict", payload["status"])

        (self.target / "managed/one.txt").unlink()
        (self.target / "managed").rmdir()
        real_directory = self.target / "managed-real"
        real_directory.mkdir()
        (self.target / "managed").symlink_to(real_directory, target_is_directory=True)
        exit_code, payload = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        self.assertEqual(EXIT_CONFLICT, exit_code)
        self.assertIn("Symlinked path component", payload["conflicts"][0]["reason"])

    def test_malformed_managed_block_is_a_conflict(self) -> None:
        version = "1.0.0-dev.0"
        block = b"<!-- fixture:start -->\nnew\n<!-- fixture:end -->\n"
        manifest_sha = self.releases.add_release(version, {"block": block}, managed_block=True)
        (self.target / "AGENTS.md").write_text("<!-- fixture:start -->\nbroken\n")
        exit_code, payload = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        self.assertEqual(EXIT_CONFLICT, exit_code)
        self.assertIn("requires exactly one", payload["conflicts"][0]["reason"])

    def test_read_only_commands_leave_target_unchanged(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        (self.target / "keep.txt").write_text("keep\n")
        before = snapshot(self.target)
        commands = (
            ["status"],
            [
                "plan-update",
                "--source",
                str(self.source),
                "--version",
                version,
                "--manifest-sha256",
                manifest_sha,
            ],
            ["doctor"],
        )
        for arguments in commands:
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNTIME_ROOT / "coordinator.py"),
                    "--root",
                    str(self.target),
                    "--json",
                    *arguments,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(EXIT_SUCCESS, result.returncode, result.stderr or result.stdout)
            self.assertEqual(before, snapshot(self.target), arguments)
        self.assertFalse(any(path.name == "__pycache__" for path in self.target.rglob("*")))
        self.assertFalse((RUNTIME_ROOT / "__pycache__").exists())

    def test_success_unknown_preservation_idempotence_and_rollback(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"one-v1\n", "two": b"two-v1\n"})
        sha2 = self.releases.add_release(v2, {"one": b"one-v2\n", "two": b"two-v2\n"})
        unknown = self.target / "unknown.txt"
        unknown.write_text("preserve\n")
        coordinator = Coordinator(self.target)
        self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, v1, sha1)[0])
        status_code, status = coordinator.status()
        self.assertEqual(EXIT_SUCCESS, status_code)
        self.assertEqual("not_installed", status["tracker"]["status"])
        self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, v2, sha2)[0])
        installed = snapshot(self.target)
        exit_code, payload = self.apply_update(coordinator, v2, sha2)
        self.assertEqual(EXIT_SUCCESS, exit_code)
        self.assertEqual("current", payload["status"])
        self.assertEqual(installed, snapshot(self.target))
        self.assertEqual("preserve\n", unknown.read_text())
        self.assertEqual(EXIT_SUCCESS, coordinator.doctor()[0])
        self.assertEqual(EXIT_SUCCESS, coordinator.rollback()[0])
        self.assertEqual(b"one-v1\n", (self.target / "managed/one.txt").read_bytes())
        self.assertEqual("preserve\n", unknown.read_text())

    def test_current_status_and_doctor_reject_deleted_replaced_or_tampered_receipt(self) -> None:
        version = "1.0.0-dev.0"
        replacement_version = "1.1.0-dev.0"
        retirement_version = "1.2.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        replacement_sha = self.releases.add_release(
            replacement_version, {"one": b"replacement\n"}
        )
        retirement_sha = self.releases.add_retirement_release(
            retirement_version, "one", "managed/one.txt"
        )
        coordinator = Coordinator(self.target)
        self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, version, manifest_sha)[0])
        state = json.loads(
            (self.target / ".agents/project_management/setup/state.json").read_text()
        )
        receipt_path = (
            self.target
            / f".agents/project_management/setup/receipts/{state['last_successful_transaction']}.json"
        )
        original = receipt_path.read_bytes()
        mutations = {
            "deleted": None,
            "replaced": b'{"transaction_id":"replacement"}\n',
            "tampered": original.replace(b'"bundle_version": "1.0.0-dev.0"', b'"bundle_version": "9.9.9"'),
        }
        for name, replacement in mutations.items():
            with self.subTest(name=name):
                if replacement is None:
                    receipt_path.unlink()
                else:
                    receipt_path.write_bytes(replacement)
                status_code, status = coordinator.status()
                self.assertEqual(EXIT_CONFLICT, status_code)
                self.assertEqual("modified", status["status"])
                self.assertNotEqual(EXIT_SUCCESS, coordinator.doctor()[0])
                with self.assertRaisesRegex(Exception, "canonical receipt"):
                    coordinator.rollback()
                before = snapshot(self.target)
                for changed_version, changed_sha in (
                    (replacement_version, replacement_sha),
                    (retirement_version, retirement_sha),
                ):
                    plan_code, preview = coordinator.plan_update(
                        self.source, changed_version, changed_sha
                    )
                    self.assertEqual(EXIT_CONFLICT, plan_code)
                    self.assertTrue(
                        any(
                            item["artifact_id"] == "coordinator-receipt"
                            for item in preview["conflicts"]
                        )
                    )
                    update_code, update = coordinator.update(
                        self.source,
                        changed_version,
                        changed_sha,
                        preview["plan_token"],
                    )
                    self.assertEqual(EXIT_CONFLICT, update_code)
                    self.assertEqual("conflict", update["status"])
                    self.assertEqual(before, snapshot(self.target))
                receipt_path.write_bytes(original)

    def test_state_only_release_update_is_transactional_and_rolls_back_byte_identically(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"same\n"})
        sha2 = self.releases.add_release(v2, {"one": b"same\n"})
        coordinator = Coordinator(self.target)
        self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, v1, sha1)[0])
        state_path = self.target / ".agents/project_management/setup/state.json"
        prior_state = state_path.read_bytes()
        code, updated = self.apply_update(coordinator, v2, sha2)
        self.assertEqual(EXIT_SUCCESS, code)
        self.assertEqual("updated", updated["status"])
        self.assertEqual([], updated["operations"])
        state = json.loads(state_path.read_text())
        self.assertEqual(updated["transaction_id"], state["last_transaction"]["transaction_id"])
        self.assertEqual([], state["last_transaction"]["operations"])
        self.assertTrue(state["rollback_available"])
        self.assertEqual(EXIT_SUCCESS, coordinator.status()[0])
        rollback_code, rollback = coordinator.rollback()
        self.assertEqual(EXIT_SUCCESS, rollback_code)
        self.assertEqual([], rollback["operations"])
        self.assertEqual(prior_state, state_path.read_bytes())
        self.assertEqual(EXIT_SUCCESS, coordinator.status()[0])

    def test_state_only_release_crash_boundaries_recover_and_remain_rollbackable(self) -> None:
        for boundary in ("after_journal", "before_state", "after_state"):
            with self.subTest(boundary=boundary):
                target = Path(self.temporary.name) / f"state-only-{boundary}"
                target.mkdir()
                v1 = "2.0.0-dev.0"
                v2 = "2.1.0-dev.0"
                sha1 = self.releases.add_release(v1, {"one": b"same-boundary\n"})
                sha2 = self.releases.add_release(v2, {"one": b"same-boundary\n"})
                coordinator = Coordinator(target)
                self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, v1, sha1)[0])
                prior_state = (target / ".agents/project_management/setup/state.json").read_bytes()
                _, preview = coordinator.plan_update(self.source, v2, sha2)

                def interrupt(point: str) -> None:
                    if point == boundary:
                        raise FaultInjected(point)

                with self.assertRaises(FaultInjected):
                    Coordinator(target, fault_injector=interrupt).update(
                        self.source, v2, sha2, preview["plan_token"]
                    )
                code, recovered = Coordinator(target).update(
                    self.source, v2, sha2, preview["plan_token"]
                )
                self.assertEqual(EXIT_SUCCESS, code)
                self.assertIn(recovered["status"], {"updated", "current"})
                self.assertFalse(
                    (target / ".agents/project_management/setup/.runtime/journal.json").exists()
                )
                self.assertEqual(EXIT_SUCCESS, Coordinator(target).status()[0])
                self.assertEqual(EXIT_SUCCESS, Coordinator(target).rollback()[0])
                self.assertEqual(
                    prior_state,
                    (target / ".agents/project_management/setup/state.json").read_bytes(),
                )
    def test_customization_aborts_whole_update_and_writes_report(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"one-v1\n", "two": b"two-v1\n"})
        sha2 = self.releases.add_release(v2, {"one": b"one-v2\n", "two": b"two-v2\n"})
        coordinator = Coordinator(self.target)
        self.apply_update(coordinator, v1, sha1)
        (self.target / "managed/one.txt").write_text("custom\n")
        two_before = (self.target / "managed/two.txt").read_bytes()
        exit_code, payload = self.apply_update(coordinator, v2, sha2)
        self.assertEqual(EXIT_CONFLICT, exit_code)
        self.assertEqual(two_before, (self.target / "managed/two.txt").read_bytes())
        self.assertEqual(b"custom\n", (self.target / "managed/one.txt").read_bytes())
        self.assertTrue((self.target / payload["conflict_report"]).is_file())
        candidate = self.target / Path(payload["conflict_report"]).parent / "candidate/managed/one.txt"
        self.assertEqual(b"one-v2\n", candidate.read_bytes())

    def test_fault_recovery_completes_a_later_update(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n", "two": b"two\n"})

        def interrupt(point: str) -> None:
            if point == "after_operation:1":
                raise FaultInjected(point)

        _, preview = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        with self.assertRaises(FaultInjected):
            Coordinator(self.target, fault_injector=interrupt).update(
                self.source, version, manifest_sha, preview["plan_token"]
            )
        exit_code, payload = Coordinator(self.target).update(
            self.source, version, manifest_sha, preview["plan_token"]
        )
        self.assertEqual(EXIT_SUCCESS, exit_code)
        self.assertTrue(str(payload["recovery"]).startswith("restored:"))
        self.assertEqual(b"one\n", (self.target / "managed/one.txt").read_bytes())
        self.assertEqual(b"two\n", (self.target / "managed/two.txt").read_bytes())

    def test_concurrent_lock_is_rejected(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        _, preview = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        with CoordinatorLock(self.target, ".agents/project_management/setup/.runtime/coordinator.lock"):
            with self.assertRaises(LockBusy):
                Coordinator(self.target).update(
                    self.source, version, manifest_sha, preview["plan_token"]
                )

    def test_sigkill_after_journal_releases_lock_and_recovers(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n", "two": b"two\n"})
        _, preview = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        marker = Path(self.temporary.name) / "coordinator-journal-ready"
        script = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from coordinator import Coordinator
def stop(point):
    if point == 'after_journal':
        pathlib.Path(sys.argv[4]).write_text('ready')
        while True: time.sleep(1)
Coordinator(pathlib.Path(sys.argv[2]), fault_injector=stop).update(pathlib.Path(sys.argv[3]), sys.argv[5], sys.argv[6], sys.argv[7])
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(RUNTIME_ROOT),
                str(self.target),
                str(self.source),
                str(marker),
                version,
                manifest_sha,
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
        exit_code, payload = Coordinator(self.target).update(
            self.source, version, manifest_sha, preview["plan_token"]
        )
        self.assertEqual(EXIT_SUCCESS, exit_code)
        self.assertTrue(str(payload["recovery"]).startswith("restored:"))

    def test_hard_crash_recovery_preserves_post_crash_target_edit_and_journal(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n", "two": b"two\n"})
        _, preview = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        script = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from coordinator import Coordinator
def crash(point):
    if point == 'after_operation:1': os._exit(91)
Coordinator(pathlib.Path(sys.argv[2]), fault_injector=crash).update(pathlib.Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6])
"""
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(RUNTIME_ROOT),
                str(self.target),
                str(self.source),
                version,
                manifest_sha,
                preview["plan_token"],
            ],
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        journal_path = self.target / ".agents/project_management/setup/.runtime/journal.json"
        journal = json.loads(journal_path.read_text())
        edited = self.target / journal["operations"][0]["target"]
        edited.write_text("post-crash coordinator edit\n")
        with self.assertRaisesRegex(Exception, "post-crash edits"):
            Coordinator(self.target).update(
                self.source, version, manifest_sha, preview["plan_token"]
            )
        self.assertEqual("post-crash coordinator edit\n", edited.read_text())
        self.assertTrue(journal_path.exists())
        for operation in journal["operations"]:
            if operation["candidate"]:
                self.assertTrue((self.target / operation["candidate"]).is_file())
            backup = operation["backup"]["backup"]
            if backup:
                self.assertTrue((self.target / backup).is_file())

    def test_coordinator_recovery_refuses_corrupt_candidate_without_mutation(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        _, preview = Coordinator(self.target).plan_update(self.source, version, manifest_sha)
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from coordinator import Coordinator
def crash(point):
    if point == 'after_journal': os._exit(91)
Coordinator(pathlib.Path(sys.argv[2]), fault_injector=crash).update(pathlib.Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6])
""",
                str(RUNTIME_ROOT),
                str(self.target),
                str(self.source),
                version,
                manifest_sha,
                preview["plan_token"],
            ],
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        journal_path = self.target / ".agents/project_management/setup/.runtime/journal.json"
        journal = json.loads(journal_path.read_text())
        candidate = self.target / journal["operations"][0]["candidate"]
        candidate.write_text("corrupt\n")
        with self.assertRaisesRegex(Exception, "candidate is missing or corrupt"):
            Coordinator(self.target).update(
                self.source, version, manifest_sha, preview["plan_token"]
            )
        self.assertFalse((self.target / "managed/one.txt").exists())
        self.assertTrue(journal_path.exists())

    def test_state_excludes_absent_tracker_and_reports_obsolete_tracker_separately(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        coordinator = Coordinator(self.target)
        self.assertEqual(
            EXIT_SUCCESS,
            self.apply_update(coordinator, version, manifest_sha)[0],
        )
        state = json.loads(
            (self.target / ".agents/project_management/setup/state.json").read_text()
        )
        self.assertNotIn("task-tracking-setup", state["component_versions"])
        self.assertNotIn("tracker_data_schema_version", state)
        self.assertEqual("not_installed", coordinator.status()[1]["tracker"]["status"])
        tracker_state = self.target / ".agents/project_management/setup/tracker-install-state.json"
        tracker_state.write_text(
            json.dumps(
                {
                    "component": "task-tracking-setup",
                    "component_version": "0.1.0",
                    "tracker_data_schema_version": 3,
                    "board_data_version": 0,
                    "artifacts": [],
                }
            )
        )
        code, status = coordinator.status()
        self.assertEqual(EXIT_CONFLICT, code)
        self.assertEqual("obsolete", status["tracker"]["status"])

    def test_rollback_refuses_post_update_edit(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"v1\n"})
        sha2 = self.releases.add_release(v2, {"one": b"v2\n"})
        coordinator = Coordinator(self.target)
        self.apply_update(coordinator, v1, sha1)
        self.apply_update(coordinator, v2, sha2)
        (self.target / "managed/one.txt").write_text("edited\n")
        with self.assertRaises(Exception) as context:
            coordinator.rollback()
        self.assertIn("changed after update", str(context.exception))

    def test_retirement_removes_only_recorded_unchanged_artifact(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"v1\n"})
        sha2 = self.releases.add_retirement_release(v2, "one", "managed/one.txt")
        coordinator = Coordinator(self.target)
        self.apply_update(coordinator, v1, sha1)
        unknown = self.target / "managed/unknown.txt"
        unknown.write_text("keep\n")
        self.assertEqual(EXIT_SUCCESS, self.apply_update(coordinator, v2, sha2)[0])
        self.assertFalse((self.target / "managed/one.txt").exists())
        self.assertEqual("keep\n", unknown.read_text())
        self.assertEqual(EXIT_SUCCESS, coordinator.rollback()[0])
        self.assertEqual(b"v1\n", (self.target / "managed/one.txt").read_bytes())

    def test_arbitrary_historical_downgrade_is_rejected(self) -> None:
        v1 = "1.0.0-dev.0"
        v2 = "1.1.0-dev.0"
        sha1 = self.releases.add_release(v1, {"one": b"v1\n"})
        sha2 = self.releases.add_release(v2, {"one": b"v2\n"})
        coordinator = Coordinator(self.target)
        self.apply_update(coordinator, v1, sha1)
        self.apply_update(coordinator, v2, sha2)
        with self.assertRaises(ValidationError) as context:
            coordinator.plan_update(self.source, v1, sha1)
        self.assertIn("Historical downgrade", str(context.exception))

    def test_corrupt_state_and_deterministic_json_exit_codes(self) -> None:
        state = self.target / ".agents/project_management/setup/state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{broken")
        command = [
            sys.executable,
            str(RUNTIME_ROOT / "coordinator.py"),
            "--root",
            str(self.target),
            "--json",
            "status",
        ]
        first = subprocess.run(command, capture_output=True, text=True)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(EXIT_INVALID, first.returncode)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual("invalid", payload["status"])

    def test_plan_json_and_exit_code_are_deterministic(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        command = [
            sys.executable,
            str(RUNTIME_ROOT / "coordinator.py"),
            "--root",
            str(self.target),
            "--json",
            "plan-update",
            "--source",
            str(self.source),
            "--version",
            version,
            "--manifest-sha256",
            manifest_sha,
        ]
        first = subprocess.run(command, capture_output=True, text=True)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(EXIT_SUCCESS, first.returncode)
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual("ready", json.loads(first.stdout)["status"])

    def test_update_requires_exact_preview_token_and_rejects_target_or_source_drift(self) -> None:
        version = "1.0.0-dev.0"
        manifest_sha = self.releases.add_release(version, {"one": b"one\n"})
        coordinator = Coordinator(self.target)
        _, preview = coordinator.plan_update(self.source, version, manifest_sha)
        with self.assertRaises(ValidationError):
            coordinator.update(self.source, version, manifest_sha, "not-a-plan-token")
        target = self.target / "managed/one.txt"
        target.parent.mkdir()
        target.write_text("changed after preview\n")
        code, payload = coordinator.update(
            self.source,
            version,
            manifest_sha,
            preview["plan_token"],
        )
        self.assertEqual(EXIT_CONFLICT, code)
        self.assertEqual("conflict", payload["status"])
        self.assertFalse((self.target / ".agents/project_management/setup/state.json").exists())

        target.unlink()
        _, preview = coordinator.plan_update(self.source, version, manifest_sha)
        (self.source / f"artifacts/{version}/one.txt").write_text("source drift\n")
        with self.assertRaises(ValidationError):
            coordinator.update(
                self.source,
                version,
                manifest_sha,
                preview["plan_token"],
            )


if __name__ == "__main__":
    unittest.main()

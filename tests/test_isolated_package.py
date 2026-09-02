from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SETUP = REPOSITORY_ROOT / "project-setup"
TRACKER = REPOSITORY_ROOT / "task-tracking-setup"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


class IsolatedProjectSetupPackageTest(unittest.TestCase):
    def test_release_synchronizer_reports_reproducible_package_material(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "sync_release_material.py"), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        with zipfile.ZipFile(
            PROJECT_SETUP / "assets/vendor/task-tracking-setup.zip"
        ) as package:
            members = package.infolist()
        self.assertEqual(sorted(item.filename for item in members), [item.filename for item in members])
        self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in members))

    def test_release_check_does_not_require_ignored_root_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clean-release-check-") as temporary:
            copy = Path(temporary) / "source"
            shutil.copytree(
                REPOSITORY_ROOT,
                copy,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".ruff_cache",
                    "__pycache__",
                ),
            )
            root_archive = copy / "task-tracking-setup.zip"
            if root_archive.exists():
                root_archive.unlink()
            self.assertFalse((copy / "task-tracking-setup.zip").exists())
            result = subprocess.run(
                [sys.executable, str(copy / "sync_release_material.py"), "--check"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_vendored_catalog_and_tracker_archive_are_synchronized(self) -> None:
        repository_catalog = json.loads(
            (REPOSITORY_ROOT / "capabilities/catalog.yaml").read_text()
        )
        packaged_catalog = json.loads(
            (PROJECT_SETUP / "assets/capabilities/catalog.yaml").read_text()
        )
        repository_capabilities = {
            item["id"]: item for item in repository_catalog["capabilities"]
        }
        packaged_capabilities = {
            item["id"]: item for item in packaged_catalog["capabilities"]
        }
        self.assertEqual(set(repository_capabilities), set(packaged_capabilities))
        self.assertEqual(
            {key: value for key, value in repository_catalog.items() if key != "capabilities"},
            {key: value for key, value in packaged_catalog.items() if key != "capabilities"},
        )
        for capability_id in repository_capabilities:
            repository_record = dict(repository_capabilities[capability_id])
            packaged_record = dict(packaged_capabilities[capability_id])
            for field in ("reference", "repository_path"):
                repository_record.pop(field, None)
                packaged_record.pop(field, None)
            self.assertEqual(repository_record, packaged_record, capability_id)
        integration = json.loads(
            (PROJECT_SETUP / "assets/tracker-integration.json").read_text()
        )
        archive = PROJECT_SETUP / integration["vendored_archive"]
        self.assertEqual(integration["vendored_archive_sha256"], sha256(archive))
        expected = {
            f"task-tracking-setup/{path.relative_to(TRACKER).as_posix()}": path.read_bytes()
            for path in TRACKER.rglob("*")
            if path.is_file()
            and path.name != ".DS_Store"
            and "__pycache__" not in path.parts
            and "evals" not in path.relative_to(TRACKER).parts
            and path.suffix != ".pyc"
        }
        with zipfile.ZipFile(archive) as package:
            actual = {
                item.filename: package.read(item)
                for item in package.infolist()
                if not item.is_dir()
            }
        self.assertEqual(expected, actual)

    def test_release_check_rejects_semantic_mutations(self) -> None:
        mutations = {
            "isolated-migration-order": lambda root: self._swap_isolated_migrations(root),
            "isolated-catalog-version": lambda root: self._remove_isolated_catalog_version(root),
            "vendored-version": lambda root: (root / "project-setup/VERSION").write_text(
                "9.9.9\n"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"release-mutation-{name}-"
            ) as temporary:
                copy = Path(temporary) / "source"
                shutil.copytree(
                    REPOSITORY_ROOT,
                    copy,
                    ignore=shutil.ignore_patterns(".git", ".ruff_cache", "__pycache__"),
                )
                mutate(copy)
                result = subprocess.run(
                    [sys.executable, str(copy / "sync_release_material.py"), "--check"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, result.returncode)

    def _swap_isolated_migrations(self, root: Path) -> None:
        version = (root / "VERSION").read_text().strip()
        path = root / f"project-setup/releases/{version}/install-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["migrations"][0], manifest["migrations"][1] = (
            manifest["migrations"][1],
            manifest["migrations"][0],
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n")

    def _remove_isolated_catalog_version(self, root: Path) -> None:
        version = (root / "VERSION").read_text().strip()
        path = root / f"project-setup/releases/{version}/install-manifest.json"
        manifest = json.loads(path.read_text())
        manifest.pop("instruction_catalog_version")
        path.write_text(json.dumps(manifest, indent=2) + "\n")

    def test_isolated_copy_exercises_every_packaged_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="isolated-project-setup-") as temporary:
            base = Path(temporary)
            package = base / "project-setup"
            target = base / "target"
            shutil.copytree(PROJECT_SETUP, package)
            target.mkdir()

            index = json.loads((package / "releases/index.json").read_text())
            release = index["releases"][0]
            manifest = json.loads((package / release["manifest_path"]).read_text())
            for artifact in manifest["artifacts"]:
                source = package / artifact["source"]
                self.assertTrue(source.is_file(), artifact["id"])
                self.assertEqual(artifact["sha256"], sha256(source), artifact["id"])

            coordinator = package / "runtime/coordinator.py"
            common_update = [
                sys.executable,
                str(coordinator),
                "--root",
                str(target),
                "--json",
            ]
            update_arguments = [
                "--source",
                str(package),
                "--version",
                release["version"],
                "--manifest-sha256",
                release["manifest_sha256"],
            ]
            preview = run_json([*common_update, "plan-update", *update_arguments])
            applied = run_json(
                [
                    *common_update,
                    "update",
                    *update_arguments,
                    "--plan-token",
                    preview["plan_token"],
                ]
            )
            self.assertEqual("updated", applied["status"])

            tracker = package / "runtime/install_tracker.py"
            integration = json.loads(
                (package / "assets/tracker-integration.json").read_text()
            )
            tracker_common = [
                sys.executable,
                str(tracker),
                "--root",
                str(target),
                "--json",
            ]
            tracker_preview = run_json([*tracker_common, "--dry-run"])
            tracker_applied = run_json(
                [*tracker_common, "--plan-token", tracker_preview["plan_token"]]
            )
            self.assertIn(tracker_applied["status"], {"installed", "recorded"})

            repeated_preview = run_json([*tracker_common, "--dry-run"])
            repeated = run_json(
                [*tracker_common, "--plan-token", repeated_preview["plan_token"]]
            )
            self.assertEqual("current", repeated["status"])
            tracker_state = json.loads(
                (
                    target / ".agents/project_management/setup/tracker-install-state.json"
                ).read_text()
            )
            provenance = tracker_state["source_identity"]
            self.assertEqual("checksum_verified_vendored_archive", provenance["kind"])
            self.assertEqual(integration["vendored_archive_sha256"], provenance["archive_sha256"])
            self.assertNotIn("local_path", provenance)
            self.assertNotIn("project-setup-tracker-", json.dumps(tracker_state))

            status = run_json([*common_update, "status"])
            doctor = run_json([*common_update, "doctor"])
            self.assertEqual("ok", status["status"])
            self.assertEqual("ok", status["tracker"]["status"])
            self.assertEqual("healthy", doctor["status"])


if __name__ == "__main__":
    unittest.main()

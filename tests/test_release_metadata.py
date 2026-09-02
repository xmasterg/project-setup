from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTest(unittest.TestCase):
    def test_unreleased_index_manifest_and_artifact_checksums(self) -> None:
        version = (REPOSITORY_ROOT / "VERSION").read_text().strip()
        index = json.loads((REPOSITORY_ROOT / "releases/index.json").read_text())
        self.assertEqual(version, index["current_source_version"])
        self.assertIsNone(index["stable_version"])
        release = index["releases"][0]
        self.assertEqual("unreleased", release["status"])
        self.assertIsNone(release["published_tag"])
        self.assertIsNone(release["immutable_commit"])
        manifest_path = REPOSITORY_ROOT / release["manifest_path"]
        manifest_payload = manifest_path.read_bytes()
        self.assertEqual(hashlib.sha256(manifest_payload).hexdigest(), release["manifest_sha256"])
        manifest = json.loads(manifest_payload)
        self.assertEqual(version, manifest["bundle_version"])
        self.assertEqual(1, manifest["manifest_schema_version"])
        self.assertEqual("1.0.0", manifest["instruction_catalog_version"])
        release_info = json.loads(
            (REPOSITORY_ROOT / f"releases/{version}/release-info.json").read_text()
        )
        self.assertNotIn("task-tracking-setup", manifest["component_versions"])
        self.assertNotIn("tracker_data_schema_version", manifest)
        self.assertNotIn("board_data_version", manifest)
        self.assertEqual(4, release_info["tracker_data_schema_version"])
        self.assertEqual(1, release_info["board_data_version"])
        self.assertEqual(
            manifest["instruction_catalog_version"],
            release_info["instruction_catalog_version"],
        )
        self.assertEqual(
            manifest["component_versions"]["coordinator"],
            manifest["bundle_version"],
        )
        self.assertEqual(
            {"managed", "managed_block", "seed", "user_data", "generated"},
            set(manifest["artifact_policies"]),
        )
        self.assertIn(
            "instruction-bundles-v1", {item["id"] for item in manifest["migrations"]}
        )
        self.assertTrue(
            {
                "tracker-installer",
                "tracker-integration",
                "tracker-vendored-archive",
            }
            <= {item["id"] for item in manifest["artifacts"]}
        )
        for artifact in manifest["artifacts"]:
            source = REPOSITORY_ROOT / artifact["source"]
            self.assertTrue(source.is_file(), artifact["id"])
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                artifact["sha256"],
                artifact["id"],
            )

    def test_self_contained_project_setup_release_metadata(self) -> None:
        package = REPOSITORY_ROOT / "project-setup"
        index = json.loads((package / "releases/index.json").read_text())
        self.assertIsNone(index["stable_version"])
        release = index["releases"][0]
        self.assertEqual("unreleased", release["status"])
        manifest_path = package / release["manifest_path"]
        self.assertEqual(release["manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual({"coordinator", "project-setup"}, set(manifest["component_versions"]))
        self.assertTrue(
            {
                "tracker-installer",
                "tracker-integration",
                "tracker-vendored-archive",
            }
            <= {item["id"] for item in manifest["artifacts"]}
        )
        for artifact in manifest["artifacts"]:
            source = package / artifact["source"]
            self.assertTrue(source.is_file(), artifact["id"])
            self.assertEqual(artifact["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_tracker_install_manifest_declares_policy_and_rollback(self) -> None:
        manifest = json.loads(
            (REPOSITORY_ROOT / "task-tracking-setup/assets/install-manifest.json").read_text()
        )
        self.assertEqual(1, manifest["manifest_schema_version"])
        self.assertEqual(4, manifest["tracker_data_schema_version"])
        self.assertEqual(1, manifest["board_data_version"])
        self.assertEqual(
            {"managed", "managed_block", "seed", "user_data", "generated"},
            set(manifest["artifact_policies"]),
        )
        self.assertTrue(all("checksum" in artifact for artifact in manifest["artifacts"]))
        self.assertTrue(all("rollback" in migration for migration in manifest["migrations"]))
        self.assertIn("task-board-split-v1", {item["id"] for item in manifest["migrations"]})

    def test_task_tracking_setup_skill_does_not_own_routine_task_work(self) -> None:
        tracker = REPOSITORY_ROOT / "task-tracking-setup"
        skill = (tracker / "SKILL.md").read_text()
        metadata = (tracker / "agents/openai.yaml").read_text()
        self.assertIn("\nname: task-tracking-setup\n", skill)
        self.assertIn("not ordinary development", skill)
        self.assertIn("$task-tracking-setup", metadata)
        self.assertNotIn("$task-tracking-setup", metadata)

        for relative in ("assets/AGENTS.block.md", "assets/CLAUDE.block.md"):
            with self.subTest(path=relative):
                block = (tracker / relative).read_text()
                self.assertIn("Follow this block directly during ordinary development", block)
                self.assertNotIn("Use the `task-tracking-setup` skill", block)

        bundle = json.loads(
            (
                REPOSITORY_ROOT
                / "project-setup/assets/instructions/bundles/task-tracking.json"
            ).read_text()
        )
        for destination, content in bundle["content"].items():
            with self.subTest(destination=destination):
                self.assertIn("Follow this block directly during ordinary development", content)
                self.assertNotIn("Use the `task-tracking-setup` skill", content)

    def test_instruction_and_capability_catalog_schemas(self) -> None:
        instruction_catalog = json.loads(
            (REPOSITORY_ROOT / "project-setup/assets/instructions/catalog.json").read_text()
        )
        self.assertEqual(1, instruction_catalog["catalog_schema_version"])
        self.assertEqual("1.0.0", instruction_catalog["catalog_version"])
        required = {
            "id",
            "version",
            "purpose",
            "applicability",
            "dependencies",
            "conflicts",
            "destinations",
            "markers",
            "source_asset",
        }
        for bundle in instruction_catalog["bundles"]:
            self.assertTrue(required <= set(bundle), bundle["id"])
            self.assertEqual({"AGENTS.md", "CLAUDE.md"}, set(bundle["destinations"]))
        capability_catalog = json.loads(
            (REPOSITORY_ROOT / "capabilities/catalog.yaml").read_text()
        )
        self.assertEqual(2, capability_catalog["schema_version"])
        self.assertEqual("1.0.0", capability_catalog["catalog_version"])
        self.assertTrue(
            all(item["automatic_install"] is False for item in capability_catalog["capabilities"])
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePath


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPOSITORY_ROOT / "sync_release_material.py"
STABLE_VERSION = "0.2.0"
STABLE_TAG = "v0.2.0"


def run(
    command: list[str], *, expected: int = 0, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if completed.returncode != expected:
        raise AssertionError(
            f"Command returned {completed.returncode}, expected {expected}: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def repository_source_paths() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / raw.decode("utf-8")
        for raw in completed.stdout.split(b"\0")
        if raw and (REPOSITORY_ROOT / raw.decode("utf-8")).is_file()
    ]


def create_tagged_repository(root: Path, *, annotated: bool = False) -> tuple[Path, str]:
    repository = root / ("annotated-repository" if annotated else "lightweight-repository")
    repository.mkdir()
    for source in repository_source_paths():
        relative = source.relative_to(REPOSITORY_ROOT)
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    run(["git", "init", "--quiet", str(repository)])
    run(["git", "add", "-A"], expected=0, cwd=repository)
    run(
        [
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "test release source",
        ],
        cwd=repository,
    )
    commit = run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
    tag_command = ["git"]
    if annotated:
        tag_command.extend(
            [
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release-test@example.invalid",
                "tag",
                "-a",
                STABLE_TAG,
                "-m",
                "test release",
            ]
        )
    else:
        tag_command.extend(["tag", STABLE_TAG])
    run(tag_command, cwd=repository)
    return repository, commit


def build_stable(source: Path, output: Path, commit: str) -> None:
    run(
        [
            sys.executable,
            str(BUILDER),
            "finalize",
            "--source",
            str(source),
            "--output",
            str(output),
            "--version",
            STABLE_VERSION,
            "--tag",
            STABLE_TAG,
            "--commit",
            commit,
        ]
    )


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class StableReleaseBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="stable-builder-")
        self.root = Path(self.temporary.name)
        self.repository, self.commit = create_tagged_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def finalize(self, output: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        values = {
            "source": str(self.repository),
            "version": STABLE_VERSION,
            "tag": STABLE_TAG,
            "commit": self.commit,
            **overrides,
        }
        return run(
            [
                sys.executable,
                str(BUILDER),
                "finalize",
                "--source",
                values["source"],
                "--output",
                str(output),
                "--version",
                values["version"],
                "--tag",
                values["tag"],
                "--commit",
                values["commit"],
            ],
            expected=1,
        )

    def test_finalize_rejects_invalid_or_unverified_git_identity(self) -> None:
        cases = (
            ({"commit": "abc"}, "40 lowercase"),
            ({"commit": self.commit.upper()}, "40 lowercase"),
            ({"commit": "b" * 40}, "do not resolve to one identity"),
            ({"tag": "HEAD"}, "must be exactly v0.2.0"),
            ({"tag": "v9.9.9"}, "must be exactly v0.2.0"),
            ({"version": "9.9.9"}, "must be exactly v9.9.9"),
        )
        for position, (overrides, expected) in enumerate(cases):
            with self.subTest(overrides=overrides):
                completed = self.finalize(self.root / f"invalid-{position}", **overrides)
                self.assertIn(expected, completed.stderr)

    def test_unreleased_working_tree_build_is_guarded_and_excludes_debris(self) -> None:
        distribution = self.root / "unreleased"
        run(
            [
                sys.executable,
                str(BUILDER),
                "build",
                "--source",
                str(REPOSITORY_ROOT),
                "--output",
                str(distribution),
            ]
        )
        descriptor = json.loads((distribution / "release-candidate.json").read_text())
        self.assertEqual("unreleased", descriptor["status"])
        self.assertFalse(descriptor["bootstrap"]["final"])
        self.assertFalse((distribution / "github-bootstrap.txt").exists())
        source_paths = {
            path.relative_to(distribution / "source").as_posix()
            for path in (distribution / "source").rglob("*")
        }
        forbidden_parts = {
            ".git",
            ".ruff_cache",
            "__pycache__",
            "dist",
            "evals",
            "project-setup-workspace",
            "task-tracking-setup-workspace",
        }
        self.assertFalse(
            any(forbidden_parts.intersection(PurePath(path).parts) for path in source_paths)
        )
        self.assertFalse(any(path.endswith((".pyc", ".pyo", ".DS_Store")) for path in source_paths))

    def test_finalize_rejects_dirty_tracked_untracked_and_ignored_checkout(self) -> None:
        contamination = (
            ("tracked", lambda repository: (repository / "VERSION").write_text("changed\n")),
            ("untracked", lambda repository: (repository / "untracked.txt").write_text("x\n")),
            ("ignored", lambda repository: (repository / ".DS_Store").write_bytes(b"x")),
        )
        for name, contaminate in contamination:
            with self.subTest(name=name):
                case_root = self.root / name
                case_root.mkdir()
                repository, commit = create_tagged_repository(case_root)
                contaminate(repository)
                completed = self.finalize(
                    self.root / f"dirty-{name}",
                    source=str(repository),
                    commit=commit,
                )
                self.assertIn("must be clean", completed.stderr)

    def test_finalize_rejects_checkout_head_that_differs_from_tagged_commit(self) -> None:
        (self.repository / "post-tag.txt").write_text("new head\n")
        run(["git", "add", "post-tag.txt"], cwd=self.repository)
        run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "post tag",
            ],
            cwd=self.repository,
        )
        completed = self.finalize(self.root / "head-mismatch")
        self.assertIn("do not resolve to one identity", completed.stderr)

    def test_stable_build_is_byte_reproducible_and_has_verified_inventory(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        build_stable(self.repository, first, self.commit)
        build_stable(self.repository, second, self.commit)
        self.assertEqual(file_snapshot(first), file_snapshot(second))
        run(
            [
                sys.executable,
                str(BUILDER),
                "inspect",
                "--distribution",
                str(first),
            ]
        )
        descriptor = json.loads((first / "release-descriptor.json").read_text())
        self.assertTrue(descriptor["bootstrap"]["final"])
        checksum_lines = (first / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(3, len(checksum_lines))
        for package in descriptor["packages"]:
            path = first / package["filename"]
            self.assertEqual(package["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(package["file_count"], len(archive.infolist()))
                for member in archive.infolist():
                    payload = archive.read(member)
                    if member.filename == "project-setup/runtime/update_core.py":
                        self.assertIn(b"RELEASE_TAG", payload)
                        payload = payload.replace(b"RELEASE_TAG", b"")
                    for token in (
                        b"PROJECT_SETUP_RELEASE",
                        b"RELEASE_TAG",
                        b"RELEASE_COMMIT",
                    ):
                        self.assertNotIn(token, payload)
                    self.assertNotIn(b"0.2.0-dev.0", payload)

    def test_stable_documentation_is_coherent_and_runtime_guards_are_unchanged(
        self,
    ) -> None:
        distribution = self.root / "documentation"
        build_stable(self.repository, distribution, self.commit)
        source = distribution / "source"
        forbidden = {
            "README.md": (
                "explicitly unreleased",
                "Do not run a bootstrap containing `PROJECT_SETUP_RELEASE`",
                "No immutable first-party release is claimed",
            ),
            "RELEASE.md": (
                "tracked source tree remains truthfully `0.2.0` and unreleased",
            ),
            "CHANGELOG.md": (
                "## 0.2.0 — Unreleased",
                "No tag or immutable commit is claimed for this unreleased version.",
            ),
        }
        for relative, phrases in forbidden.items():
            with self.subTest(path=relative):
                text = (source / relative).read_text()
                self.assertIn(STABLE_TAG, text)
                self.assertIn(self.commit, text)
                for phrase in phrases:
                    self.assertNotIn(phrase, text)

        guard_path = "project-setup/runtime/update_core.py"
        committed_guard = run(
            ["git", "show", f"{self.commit}:{guard_path}"], cwd=self.repository
        ).stdout
        self.assertEqual(committed_guard, (source / guard_path).read_text())
        self.assertIn("RELEASE_TAG", committed_guard)

    def test_commit_staging_reads_blob_bytes_instead_of_export_substituted_bytes(
        self,
    ) -> None:
        template = "$Format:%H$\n"
        (self.repository / ".gitattributes").write_text("exported.txt export-subst\n")
        (self.repository / "exported.txt").write_text(template)
        run(["git", "add", ".gitattributes", "exported.txt"], cwd=self.repository)
        run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "add export substitution fixture",
            ],
            cwd=self.repository,
        )
        commit = run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()
        run(["git", "tag", "--force", STABLE_TAG], cwd=self.repository)
        distribution = self.root / "export-subst"
        build_stable(self.repository, distribution, commit)
        self.assertEqual(template, (distribution / "source/exported.txt").read_text())

    def test_finalize_accepts_lightweight_and_annotated_exact_tags(self) -> None:
        build_stable(self.repository, self.root / "lightweight", self.commit)
        annotated_root = self.root / "annotated-case"
        annotated_root.mkdir()
        repository, commit = create_tagged_repository(annotated_root, annotated=True)
        build_stable(repository, self.root / "annotated", commit)
        self.assertTrue((self.root / "lightweight/release-descriptor.json").is_file())
        self.assertTrue((self.root / "annotated/release-descriptor.json").is_file())

    def test_public_inspect_rejects_every_descriptor_field_mutation(self) -> None:
        original = self.root / "original"
        build_stable(self.repository, original, self.commit)

        def set_value(path: tuple[object, ...], value: object):
            def mutate(descriptor: dict) -> None:
                target: object = descriptor
                for key in path[:-1]:
                    target = target[key]  # type: ignore[index]
                target[path[-1]] = value  # type: ignore[index]

            return mutate

        mutations = {
            "schema": set_value(("release_descriptor_schema_version",), 2),
            "version": set_value(("version",), "9.9.9"),
            "status": set_value(("status",), "unreleased"),
            "tag": set_value(("published_tag",), "v9.9.9"),
            "commit": set_value(("immutable_commit",), "b" * 40),
            "source commit": set_value(("source_checkout_commit",), "b" * 40),
            "package filename": set_value(("packages", 0, "filename"), "other.skill"),
            "package count": set_value(("packages", 0, "file_count"), 1),
            "package hash": set_value(("packages", 0, "sha256"), "0" * 64),
            "checksums filename": set_value(("checksums", "filename"), "other"),
            "checksums hash": set_value(("checksums", "sha256"), "0" * 64),
            "bootstrap filename": set_value(("bootstrap", "filename"), "other"),
            "bootstrap final": set_value(("bootstrap", "final"), False),
            "bootstrap hash": set_value(("bootstrap", "sha256"), "0" * 64),
            "source path": set_value(("source_tree", "path"), "other"),
            "source count": set_value(("source_tree", "file_count"), 1),
            "source hash": set_value(("source_tree", "sha256"), "0" * 64),
            "source inventory path": set_value(
                ("source_tree", "files", 0, "path"), "other"
            ),
            "source inventory hash": set_value(
                ("source_tree", "files", 0, "sha256"), "0" * 64
            ),
            "metadata count": set_value(("metadata", "file_count"), 1),
            "metadata inventory hash": set_value(("metadata", "sha256"), "0" * 64),
            "metadata path": set_value(("metadata", "files", 0, "path"), "other"),
            "metadata hash": set_value(
                ("metadata", "files", 0, "sha256"), "0" * 64
            ),
            "unexpected field": lambda descriptor: descriptor.update({"extra": True}),
        }
        for position, (name, mutate) in enumerate(mutations.items()):
            with self.subTest(name=name):
                changed = self.root / f"descriptor-mutation-{position}"
                shutil.copytree(original, changed)
                descriptor_path = changed / "release-descriptor.json"
                descriptor = json.loads(descriptor_path.read_text())
                mutate(descriptor)
                descriptor_path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n")
                completed = run(
                    [
                        sys.executable,
                        str(BUILDER),
                        "inspect",
                        "--distribution",
                        str(changed),
                    ],
                    expected=1,
                )
                self.assertNotEqual("", completed.stderr)

    def test_public_inspect_rejects_checksum_artifact_and_inventory_mutations(self) -> None:
        original = self.root / "original"
        build_stable(self.repository, original, self.commit)

        def duplicate_checksum(distribution: Path) -> None:
            sums = distribution / "SHA256SUMS"
            first = sums.read_text().splitlines(keepends=True)[0]
            sums.write_text(sums.read_text() + first)

        def mutate_descriptor_inventory(distribution: Path, key: str) -> None:
            path = distribution / "release-descriptor.json"
            descriptor = json.loads(path.read_text())
            descriptor[key]["files"].append(dict(descriptor[key]["files"][0]))
            path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n")

        mutations = {
            "duplicate checksum": duplicate_checksum,
            "package bytes": lambda root: (root / "project-setup.skill").write_bytes(b"changed"),
            "bootstrap bytes": lambda root: (root / "github-bootstrap.txt").write_bytes(b"changed"),
            "source bytes": lambda root: (root / "source/README.md").write_text("changed\n"),
            "duplicate source inventory": lambda root: mutate_descriptor_inventory(
                root, "source_tree"
            ),
            "duplicate metadata inventory": lambda root: mutate_descriptor_inventory(
                root, "metadata"
            ),
            "unexpected top level": lambda root: (root / "unexpected.txt").write_text("x\n"),
        }
        for position, (name, mutate) in enumerate(mutations.items()):
            with self.subTest(name=name):
                changed = self.root / f"artifact-mutation-{position}"
                shutil.copytree(original, changed)
                mutate(changed)
                run(
                    [sys.executable, str(BUILDER), "inspect", "--distribution", str(changed)],
                    expected=1,
                )

    def test_public_inspect_rejects_equivalent_payload_repacked_with_new_metadata(
        self,
    ) -> None:
        distribution = self.root / "repacked"
        build_stable(self.repository, distribution, self.commit)
        package_path = distribution / "project-setup.skill"
        with zipfile.ZipFile(package_path) as archive:
            files = [
                (member.filename, member.date_time, archive.read(member))
                for member in archive.infolist()
            ]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, date_time, payload in files:
                member = zipfile.ZipInfo(name, date_time=date_time)
                member.compress_type = zipfile.ZIP_STORED
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                archive.writestr(member, payload, compress_type=zipfile.ZIP_STORED)
        package_path.write_bytes(buffer.getvalue())

        package_checksum = hashlib.sha256(package_path.read_bytes()).hexdigest()
        sums_path = distribution / "SHA256SUMS"
        sums_payload = "".join(
            f"{package_checksum if filename == package_path.name else checksum}  {filename}\n"
            for checksum, filename in (
                line.split("  ", 1) for line in sums_path.read_text().splitlines()
            )
        ).encode()
        sums_path.write_bytes(sums_payload)
        descriptor_path = distribution / "release-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        for package in descriptor["packages"]:
            if package["filename"] == package_path.name:
                package["sha256"] = package_checksum
        descriptor["checksums"]["sha256"] = hashlib.sha256(sums_payload).hexdigest()
        descriptor_path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n")

        completed = run(
            [sys.executable, str(BUILDER), "inspect", "--distribution", str(distribution)],
            expected=1,
        )
        self.assertIn("not the deterministic build output", completed.stderr)

    def test_stable_identity_propagates_across_every_release_surface(self) -> None:
        distribution = self.root / "distribution"
        build_stable(self.repository, distribution, self.commit)
        source = distribution / "source"
        metadata_paths = (
            "releases/index.json",
            f"releases/{STABLE_VERSION}/install-manifest.json",
            f"releases/{STABLE_VERSION}/release-info.json",
            "project-setup/releases/index.json",
            f"project-setup/releases/{STABLE_VERSION}/install-manifest.json",
            f"project-setup/releases/{STABLE_VERSION}/release-info.json",
            "capabilities/catalog.yaml",
            "project-setup/assets/capabilities/catalog.yaml",
            "project-setup/assets/tracker-integration.json",
            "task-tracking-setup/assets/install-manifest.json",
        )
        for relative in metadata_paths:
            with self.subTest(path=relative):
                payload = json.loads((source / relative).read_text())
                serialized = json.dumps(payload)
                self.assertIn(STABLE_VERSION, serialized)
                self.assertIn(STABLE_TAG, serialized)
                self.assertIn(self.commit, serialized)
                self.assertIn("stable", serialized)
        root_index = json.loads((source / "releases/index.json").read_text())
        isolated_index = json.loads(
            (source / "project-setup/releases/index.json").read_text()
        )
        self.assertEqual(STABLE_VERSION, root_index["stable_version"])
        self.assertEqual(STABLE_VERSION, isolated_index["stable_version"])
        bootstrap = (distribution / "github-bootstrap.txt").read_text()
        self.assertIn(STABLE_TAG, bootstrap)
        self.assertIn(self.commit, bootstrap)

    def test_extracted_stable_package_supports_index_derived_update_integrations_and_rollback(
        self,
    ) -> None:
        root = self.root
        distribution = root / "distribution"
        extracted = root / "extracted"
        target = root / "target"
        target.mkdir()
        build_stable(self.repository, distribution, self.commit)
        with zipfile.ZipFile(distribution / "project-setup.skill") as archive:
            archive.extractall(extracted)
        package = extracted / "project-setup"
        coordinator = package / "runtime/coordinator.py"
        common = [
            sys.executable,
            str(coordinator),
            "--root",
            str(target),
            "--json",
        ]
        preview = json.loads(
            run([*common, "plan-update", "--source", str(package)]).stdout
        )
        self.assertEqual(STABLE_VERSION, preview["bundle_version"])
        updated = json.loads(
            run(
                [
                    *common,
                    "update",
                    "--source",
                    str(package),
                    "--plan-token",
                    preview["plan_token"],
                ]
            ).stdout
        )
        self.assertEqual("updated", updated["status"])
        state = json.loads(
            (target / ".agents/project_management/setup/state.json").read_text()
        )
        self.assertEqual(STABLE_TAG, state["source_identity"]["published_tag"])
        self.assertEqual(self.commit, state["source_identity"]["immutable_commit"])

        tracker = package / "runtime/install_tracker.py"
        tracker_common = [
            sys.executable,
            str(tracker),
            "--root",
            str(target),
            "--json",
        ]
        tracker_preview = json.loads(run([*tracker_common, "--dry-run"]).stdout)
        run(
            [
                *tracker_common,
                "--plan-token",
                tracker_preview["plan_token"],
            ]
        )
        tracker_state = json.loads(
            (
                target / ".agents/project_management/setup/tracker-install-state.json"
            ).read_text()
        )
        self.assertEqual("stable", tracker_state["source_identity"]["release_status"])
        self.assertEqual(STABLE_TAG, tracker_state["source_identity"]["published_tag"])
        self.assertEqual(self.commit, tracker_state["source_identity"]["immutable_commit"])

        integrator = package / "runtime/integrate_instructions.py"
        instruction_common = [
            sys.executable,
            str(integrator),
            "--root",
            str(target),
            "--destination",
            "AGENTS.md",
            "--bundle",
            "development-guidance",
            "--json",
        ]
        instruction_preview = json.loads(
            run(
                [sys.executable, str(integrator), "plan", *instruction_common[2:]]
            ).stdout
        )
        run(
            [
                sys.executable,
                str(integrator),
                "apply",
                *instruction_common[2:],
                "--plan-token",
                instruction_preview["plan_token"],
            ]
        )
        self.assertEqual("ok", json.loads(run([*common, "status"]).stdout)["status"])
        self.assertEqual("healthy", json.loads(run([*common, "doctor"]).stdout)["status"])
        self.assertEqual(
            "rolled_back", json.loads(run([*common, "rollback"]).stdout)["status"]
        )


if __name__ == "__main__":
    unittest.main()

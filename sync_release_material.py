#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parent
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARTIFACT_EXCLUDED_DIRECTORIES = {
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "evals",
    "htmlcov",
    "node_modules",
    "project-setup-workspace",
    "task-tracking-setup-workspace",
}
ARTIFACT_EXCLUDED_FILES = {".coverage", ".DS_Store"}
PACKAGE_EXCLUDED_DIRECTORIES = ARTIFACT_EXCLUDED_DIRECTORIES
PACKAGE_EXCLUDED_FILES = ARTIFACT_EXCLUDED_FILES
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STABLE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PLACEHOLDER_TOKENS = (b"PROJECT_SETUP_RELEASE", b"RELEASE_TAG", b"RELEASE_COMMIT")
STABLE_LITERAL_TOKEN_ALLOWLIST = {
    "project-setup/runtime/update_core.py": frozenset({b"RELEASE_TAG"}),
    "sync_release_material.py": frozenset(PLACEHOLDER_TOKENS),
}
DESCRIPTOR_TOP_LEVEL_KEYS = {
    "bootstrap",
    "checksums",
    "immutable_commit",
    "metadata",
    "packages",
    "published_tag",
    "release_descriptor_schema_version",
    "source_checkout_commit",
    "source_tree",
    "status",
    "version",
}


class ReleaseSynchronizationError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    status: str
    published_tag: str | None
    immutable_commit: str | None
    source_checkout_commit: str | None

    @classmethod
    def unreleased(cls, version: str) -> "ReleaseIdentity":
        return cls(version, "unreleased", None, None, None)

    @classmethod
    def _verified_stable(
        cls,
        version: str,
        published_tag: str,
        immutable_commit: str,
        source_checkout_commit: str,
    ) -> "ReleaseIdentity":
        identity = cls(
            version,
            "stable",
            published_tag,
            immutable_commit,
            source_checkout_commit,
        )
        identity.assert_valid()
        return identity

    def assert_valid(self) -> None:
        if self.status == "unreleased":
            if any(
                value is not None
                for value in (
                    self.published_tag,
                    self.immutable_commit,
                    self.source_checkout_commit,
                )
            ):
                raise ReleaseSynchronizationError(
                    "Unreleased material must not claim a tag or commit"
                )
            return
        if self.status != "stable" or not STABLE_VERSION_PATTERN.fullmatch(self.version):
            raise ReleaseSynchronizationError(
                "Stable version must be a final major.minor.patch semantic version"
            )
        if not isinstance(self.published_tag, str) or not self.published_tag.strip():
            raise ReleaseSynchronizationError("Stable published tag must be non-empty")
        if any(token.decode() in self.published_tag for token in PLACEHOLDER_TOKENS):
            raise ReleaseSynchronizationError("Stable published tag must not be a placeholder")
        expected_tag = f"v{self.version}"
        if self.published_tag != expected_tag:
            raise ReleaseSynchronizationError(
                f"Stable published tag must be exactly {expected_tag} for version {self.version}"
            )
        if not isinstance(self.immutable_commit, str) or not COMMIT_PATTERN.fullmatch(
            self.immutable_commit
        ):
            raise ReleaseSynchronizationError(
                "Stable immutable commit must be exactly 40 lowercase hexadecimal characters"
            )
        if self.source_checkout_commit != self.immutable_commit:
            raise ReleaseSynchronizationError(
                "Verified source checkout commit must match immutable commit"
            )

    def source_identity(self) -> dict[str, Any]:
        return {
            "kind": "verified_local",
            "published_tag": self.published_tag,
            "immutable_commit": self.immutable_commit,
            "source_checkout_commit": self.source_checkout_commit,
        }


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def overridden_payload(path: Path, overrides: dict[Path, bytes]) -> bytes:
    if path in overrides:
        return overrides[path]
    return path.read_bytes()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def read_json(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseSynchronizationError(
                    f"Duplicate JSON object key in {path}: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseSynchronizationError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseSynchronizationError(f"Expected JSON object: {path}")
    return value


def source_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    isolated_version = (root / "project-setup/VERSION").read_text(encoding="utf-8").strip()
    if not version or isolated_version != version:
        raise ReleaseSynchronizationError(
            "Root VERSION and isolated project-setup/VERSION must match exactly"
        )
    return version


def instruction_catalog_version(root: Path) -> str:
    catalog = read_json(root / "project-setup/assets/instructions/catalog.json")
    version = catalog.get("catalog_version")
    if not isinstance(version, str) or not version:
        raise ReleaseSynchronizationError("Instruction catalog version is invalid")
    return version


def parse_safe_repository_path(raw_path: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or raw_path in {".", ".."}
        or raw_path.startswith("/")
        or "\\" in raw_path
        or any(ord(character) < 32 for character in raw_path)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseSynchronizationError(f"Unsafe {label} path: {raw_path!r}")
    return path


def is_artifact_source_path(relative: PurePosixPath) -> bool:
    if any(part in ARTIFACT_EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    if relative.name in ARTIFACT_EXCLUDED_FILES:
        return False
    if relative.as_posix() == "task-tracking-setup.zip":
        return False
    return not relative.name.endswith((".pyc", ".pyo"))


def is_runtime_package_file(path: Path, package_root: Path) -> bool:
    relative = path.relative_to(package_root)
    if any(part in PACKAGE_EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    if path.is_symlink():
        raise ReleaseSynchronizationError(f"Package source must not be a symlink: {path}")
    return (
        path.is_file()
        and path.name not in PACKAGE_EXCLUDED_FILES
        and not path.name.endswith((".pyc", ".pyo"))
    )


def deterministic_zip(files: Iterable[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name, payload in sorted(files):
            member = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            archive.writestr(
                member,
                payload,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return buffer.getvalue()


def tracker_archive_payload(root: Path, overrides: dict[Path, bytes]) -> bytes:
    tracker_root = root / "task-tracking-setup"
    files = (
        (
            f"task-tracking-setup/{path.relative_to(tracker_root).as_posix()}",
            overridden_payload(path, overrides),
        )
        for path in tracker_root.rglob("*")
        if is_runtime_package_file(path, tracker_root)
    )
    return deterministic_zip(files)


def refreshed_tracker_manifest(
    root: Path, source: str, identity: ReleaseIdentity
) -> bytes:
    path = root / "task-tracking-setup/assets/install-manifest.json"
    manifest = read_json(path)
    if manifest.get("bundle_version") != source or manifest.get("component_version") != source:
        raise ReleaseSynchronizationError(
            "Tracker source bundle/component versions must match source VERSION"
        )
    manifest.update(
        {
            "bundle_version": identity.version,
            "component_version": identity.version,
            "release_status": identity.status,
            "published_tag": identity.published_tag,
            "immutable_commit": identity.immutable_commit,
            "source_checkout_commit": identity.source_checkout_commit,
        }
    )
    for artifact in manifest.get("artifacts", []):
        artifact_source = artifact.get("source")
        if isinstance(artifact_source, str):
            artifact["source_sha256"] = sha256(
                (root / "task-tracking-setup" / artifact_source).read_bytes()
            )
        destination_sources = artifact.get("destination_sources")
        if isinstance(destination_sources, dict):
            artifact["destination_source_sha256"] = {
                destination: sha256(
                    (root / "task-tracking-setup" / mapped_source).read_bytes()
                )
                for destination, mapped_source in destination_sources.items()
            }
    return canonical_json(manifest)


def refreshed_capability_catalog(
    root: Path,
    catalog_path: Path,
    source: str,
    identity: ReleaseIdentity,
) -> bytes:
    catalog = read_json(catalog_path)
    if catalog.get("bundle_source_version") != source:
        raise ReleaseSynchronizationError(
            f"Capability catalog source version drifted: {catalog_path}"
        )
    catalog.update(
        {
            "bundle_source_version": identity.version,
            "release_status": identity.status,
            "published_tag": identity.published_tag,
            "immutable_commit": identity.immutable_commit,
            "source_checkout_commit": identity.source_checkout_commit,
        }
    )
    if identity.status == "stable":
        catalog.pop("release_placeholder", None)
        catalog["release_policy"] = (
            "First-party records are pinned to the verified published tag and immutable commit."
        )
    for capability in catalog.get("capabilities", []):
        if capability.get("kind") != "first_party":
            continue
        capability["version"] = identity.version
        capability["published_tag"] = identity.published_tag
        capability["immutable_commit"] = identity.immutable_commit
        if identity.status == "stable":
            repository_path = capability.get("repository_path")
            capability["source"] = (
                "https://github.com/xmasterg/project-setup/tree/"
                f"{identity.published_tag}/{repository_path}"
            )
            capability["pinned_ref"] = identity.immutable_commit
            capability["pin_status"] = "stable"
    return canonical_json(catalog)


def refreshed_integration(
    root: Path,
    source: str,
    archive_checksum: str,
    identity: ReleaseIdentity,
) -> bytes:
    integration = read_json(root / "project-setup/assets/tracker-integration.json")
    if integration.get("required_component_version") != source:
        raise ReleaseSynchronizationError(
            "Tracker integration source version must match source VERSION"
        )
    integration.update(
        {
            "required_component_version": identity.version,
            "vendored_archive_sha256": archive_checksum,
            "release_status": identity.status,
            "published_tag": identity.published_tag,
            "immutable_commit": identity.immutable_commit,
            "source_checkout_commit": identity.source_checkout_commit,
        }
    )
    return canonical_json(integration)


def release_info(
    identity: ReleaseIdentity, catalog_version: str, *, include_tracker: bool
) -> bytes:
    components = {
        "coordinator": identity.version,
        "project-setup": identity.version,
    }
    if include_tracker:
        components["task-tracking-setup"] = identity.version
    info: dict[str, Any] = {
        "bundle_version": identity.version,
        "component_versions": components,
        "instruction_catalog_version": catalog_version,
        "manifest_schema_version": 1,
        "release_status": identity.status,
        "published_tag": identity.published_tag,
        "immutable_commit": identity.immutable_commit,
        "source_checkout_commit": identity.source_checkout_commit,
    }
    if include_tracker:
        info.update({"board_data_version": 1, "tracker_data_schema_version": 4})
    return canonical_json(info)


def root_manifest(
    root: Path,
    source: str,
    identity: ReleaseIdentity,
    catalog_version: str,
    overrides: dict[Path, bytes],
) -> dict[str, Any]:
    template = read_json(root / f"releases/{source}/install-manifest.json")
    if template.get("bundle_version") != source:
        raise ReleaseSynchronizationError("Root release manifest source version drifted")
    template.update(
        {
            "bundle_version": identity.version,
            "component_versions": {
                "coordinator": identity.version,
                "project-setup": identity.version,
            },
            "instruction_catalog_version": catalog_version,
            "release_status": identity.status,
            "published_tag": identity.published_tag,
            "immutable_commit": identity.immutable_commit,
            "source_identity": identity.source_identity(),
        }
    )
    for artifact in template.get("artifacts", []):
        artifact_source = artifact.get("source")
        if not isinstance(artifact_source, str):
            raise ReleaseSynchronizationError(
                f"Release artifact has no source: {artifact.get('id')}"
            )
        old_release_info = f"releases/{source}/release-info.json"
        if artifact_source == old_release_info:
            artifact_source = f"releases/{identity.version}/release-info.json"
            artifact["source"] = artifact_source
        source_path = root / artifact_source
        payload = overridden_payload(source_path, overrides)
        artifact["sha256"] = sha256(payload)
    return template


def isolated_source_path(root_source: str) -> str:
    if root_source.startswith("project-setup/"):
        return root_source.removeprefix("project-setup/")
    if root_source == "capabilities/catalog.yaml":
        return "assets/capabilities/catalog.yaml"
    if root_source == "VERSION" or root_source.startswith("releases/"):
        return root_source
    raise ReleaseSynchronizationError(
        f"Release artifact source has no isolated path transformation: {root_source}"
    )


def isolated_manifest(
    root: Path,
    canonical_manifest: dict[str, Any],
    overrides: dict[Path, bytes],
) -> dict[str, Any]:
    manifest = copy.deepcopy(canonical_manifest)
    package_root = root / "project-setup"
    for artifact in manifest["artifacts"]:
        artifact["source"] = isolated_source_path(artifact["source"])
        source_path = package_root / artifact["source"]
        payload = overridden_payload(source_path, overrides)
        artifact["sha256"] = sha256(payload)
    return manifest


def release_index(identity: ReleaseIdentity, manifest_payload: bytes) -> bytes:
    entry = {
        "immutable_commit": identity.immutable_commit,
        "manifest_path": f"releases/{identity.version}/install-manifest.json",
        "manifest_sha256": sha256(manifest_payload),
        "published_tag": identity.published_tag,
        "source_checkout_commit": identity.source_checkout_commit,
        "status": identity.status,
        "version": identity.version,
    }
    return canonical_json(
        {
            "release_index_schema_version": 1,
            "current_source_version": identity.version,
            "stable_version": identity.version if identity.status == "stable" else None,
            "releases": [entry],
        }
    )


def expected_release_material(
    root: Path = REPOSITORY_ROOT,
    identity: ReleaseIdentity | None = None,
    *,
    source_version_override: str | None = None,
) -> dict[Path, bytes]:
    source = source_version_override or source_version(root)
    identity = identity or ReleaseIdentity.unreleased(source)
    identity.assert_valid()
    catalog_version = instruction_catalog_version(root)
    tracker_manifest_path = root / "task-tracking-setup/assets/install-manifest.json"
    tracker_manifest = refreshed_tracker_manifest(root, source, identity)
    root_catalog_path = root / "capabilities/catalog.yaml"
    isolated_catalog_path = root / "project-setup/assets/capabilities/catalog.yaml"
    root_catalog = refreshed_capability_catalog(root, root_catalog_path, source, identity)
    isolated_catalog = refreshed_capability_catalog(
        root, isolated_catalog_path, source, identity
    )
    tracker_archive = tracker_archive_payload(root, {tracker_manifest_path: tracker_manifest})
    archive_checksum = sha256(tracker_archive)
    root_info_path = root / f"releases/{identity.version}/release-info.json"
    isolated_info_path = (
        root / f"project-setup/releases/{identity.version}/release-info.json"
    )
    material = {
        root / "VERSION": f"{identity.version}\n".encode(),
        root / "project-setup/VERSION": f"{identity.version}\n".encode(),
        tracker_manifest_path: tracker_manifest,
        root_catalog_path: root_catalog,
        isolated_catalog_path: isolated_catalog,
        root / "project-setup/assets/vendor/task-tracking-setup.zip": tracker_archive,
        root / "project-setup/assets/tracker-integration.json": refreshed_integration(
            root, source, archive_checksum, identity
        ),
        root_info_path: release_info(identity, catalog_version, include_tracker=True),
        isolated_info_path: release_info(
            identity, catalog_version, include_tracker=True
        ),
    }
    canonical_manifest = root_manifest(
        root, source, identity, catalog_version, material
    )
    package_manifest = isolated_manifest(root, canonical_manifest, material)
    root_manifest_payload = canonical_json(canonical_manifest)
    isolated_manifest_payload = canonical_json(package_manifest)
    root_manifest_path = root / f"releases/{identity.version}/install-manifest.json"
    isolated_manifest_path = (
        root / f"project-setup/releases/{identity.version}/install-manifest.json"
    )
    material.update(
        {
            root_manifest_path: root_manifest_payload,
            isolated_manifest_path: isolated_manifest_payload,
            root / "releases/index.json": release_index(identity, root_manifest_payload),
            root / "project-setup/releases/index.json": release_index(
                identity, isolated_manifest_payload
            ),
        }
    )
    return material


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def synchronize(*, check: bool, root: Path = REPOSITORY_ROOT) -> None:
    material = expected_release_material(root)
    drifted = [
        path
        for path, payload in material.items()
        if not path.is_file() or path.read_bytes() != payload
    ]
    if check and drifted:
        relative = ", ".join(path.relative_to(root).as_posix() for path in drifted)
        raise ReleaseSynchronizationError(
            f"Release material has checksum, archive, index, or semantic drift: {relative}"
        )
    if check:
        return
    for path, payload in material.items():
        atomic_write(path, payload)


def run_git(source_root: Path, *arguments: str, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=source_root,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise ReleaseSynchronizationError(
            f"Read-only Git command failed: {' '.join(arguments)}: {stderr}"
        )
    return completed.stdout


def working_tree_file_paths(source_root: Path) -> list[PurePosixPath]:
    payload = run_git(
        source_root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    assert isinstance(payload, bytes)
    deleted_payload = run_git(source_root, "ls-files", "--deleted", "-z")
    assert isinstance(deleted_payload, bytes)
    deleted_paths = {
        raw_path.decode("utf-8")
        for raw_path in deleted_payload.split(b"\0")
        if raw_path
    }
    paths: list[PurePosixPath] = []
    seen: set[str] = set()
    for raw_path in payload.split(b"\0"):
        if not raw_path:
            continue
        try:
            decoded = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseSynchronizationError(
                "Working-tree artifact paths must be valid UTF-8"
            ) from exc
        relative = parse_safe_repository_path(decoded, label="working-tree")
        if decoded in seen:
            raise ReleaseSynchronizationError(f"Duplicate working-tree path: {decoded}")
        seen.add(decoded)
        if decoded in deleted_paths:
            continue
        if is_artifact_source_path(relative) and relative.parts[0] != "tests":
            paths.append(relative)
    return sorted(paths, key=lambda item: item.as_posix())


def copy_working_tree_source(source_root: Path, destination: Path) -> None:
    destination.mkdir()
    for relative in working_tree_file_paths(source_root):
        source = source_root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise ReleaseSynchronizationError(
                f"Working-tree artifact source must be a regular file: {relative.as_posix()}"
            )
        for parent in source.parents:
            if parent == source_root:
                break
            if parent.is_symlink():
                raise ReleaseSynchronizationError(
                    f"Working-tree artifact path crosses a symlink: {relative.as_posix()}"
                )
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


@dataclass(frozen=True)
class VerifiedGitSource:
    source_root: Path
    identity: ReleaseIdentity


@dataclass(frozen=True)
class GitBlobEntry:
    mode: str
    object_id: str


def parse_commit_tree(source_root: Path, commit: str) -> dict[str, GitBlobEntry]:
    payload = run_git(source_root, "ls-tree", "-r", "-z", "--full-tree", commit)
    assert isinstance(payload, bytes)
    inventory: dict[str, GitBlobEntry] = {}
    for raw_record in payload.split(b"\0"):
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ReleaseSynchronizationError("Malformed Git tree inventory record")
        mode, object_type, object_id = fields
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            decoded = raw_path.decode("utf-8", errors="replace")
            raise ReleaseSynchronizationError(
                f"Stable source tree contains a symlink, submodule, or non-file entry: {decoded}"
            )
        try:
            decoded = raw_path.decode("utf-8")
            decoded_mode = mode.decode("ascii")
            object_hash = object_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ReleaseSynchronizationError(
                "Stable Git tree paths and object ids must be valid UTF-8/ASCII"
            ) from exc
        parse_safe_repository_path(decoded, label="Git tree")
        if decoded in inventory:
            raise ReleaseSynchronizationError(f"Duplicate Git tree path: {decoded}")
        inventory[decoded] = GitBlobEntry(decoded_mode, object_hash)
    return inventory


def read_git_blobs(
    source_root: Path, entries: Iterable[GitBlobEntry]
) -> dict[str, bytes]:
    object_ids = tuple(dict.fromkeys(entry.object_id for entry in entries))
    completed = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=source_root,
        input="".join(f"{object_id}\n" for object_id in object_ids).encode("ascii"),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseSynchronizationError(
            f"Read-only Git command failed: cat-file --batch: {stderr}"
        )

    payload = completed.stdout
    offset = 0
    blobs: dict[str, bytes] = {}
    for expected_object_id in object_ids:
        header_end = payload.find(b"\n", offset)
        if header_end < 0:
            raise ReleaseSynchronizationError("Malformed Git cat-file batch header")
        header = payload[offset:header_end].split()
        if len(header) != 3:
            raise ReleaseSynchronizationError("Malformed Git cat-file batch record")
        object_id, object_type, raw_size = header
        if (
            object_id.decode("ascii", errors="replace") != expected_object_id
            or object_type != b"blob"
        ):
            raise ReleaseSynchronizationError(
                f"Git cat-file did not return the expected blob: {expected_object_id}"
            )
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise ReleaseSynchronizationError(
                "Malformed Git cat-file blob size"
            ) from exc
        start = header_end + 1
        end = start + size
        if end >= len(payload) or payload[end : end + 1] != b"\n":
            raise ReleaseSynchronizationError("Truncated Git cat-file blob payload")
        blobs[expected_object_id] = payload[start:end]
        offset = end + 1
    if offset != len(payload):
        raise ReleaseSynchronizationError("Unexpected trailing Git cat-file output")
    return blobs


def stage_verified_commit(verified: VerifiedGitSource, destination: Path) -> None:
    source_root = verified.source_root
    commit = verified.identity.immutable_commit
    assert commit is not None
    tree_inventory = parse_commit_tree(source_root, commit)
    selected_inventory = {
        raw_path: entry
        for raw_path, entry in tree_inventory.items()
        if is_artifact_source_path(PurePosixPath(raw_path))
        and PurePosixPath(raw_path).parts[0] != "tests"
    }
    blobs = read_git_blobs(source_root, selected_inventory.values())
    destination.mkdir()
    for raw_path, entry in sorted(selected_inventory.items()):
        relative = PurePosixPath(raw_path)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[entry.object_id])
        target.chmod(0o755 if entry.mode == "100755" else 0o644)


def prune_release_directories(staging_root: Path) -> None:
    for release_root in (
        staging_root / "releases",
        staging_root / "project-setup/releases",
    ):
        if release_root.exists():
            shutil.rmtree(release_root)
        release_root.mkdir(parents=True)


def apply_stable_documentation_overlay(
    staging_root: Path, source: str, identity: ReleaseIdentity
) -> None:
    tag = str(identity.published_tag)
    commit = str(identity.immutable_commit)
    replacements: dict[str, tuple[tuple[str, str], ...]] = {
        "README.md": (
            (
                f"The current source-tree bundle version is `{source}` and is explicitly "
                "unreleased. [`releases/index.json`](releases/index.json) distinguishes source, "
                "prerelease, and stable publication state; no tag or immutable commit is claimed.",
                f"This distribution contains published bundle version `{identity.version}`, "
                f"verified at tag `{tag}` and immutable commit `{commit}`. "
                "[`releases/index.json`](releases/index.json) declares the same stable identity.",
            ),
            (
                "> **Do not run a bootstrap containing `PROJECT_SETUP_RELEASE`.** "
                f"The current `{source}` source tree remains unreleased, and that value is a "
                "blocking sentinel rather than a tag, commit, or installer input.",
                f"> **Published release:** `{tag}` at immutable commit `{commit}`.",
            ),
            (
                "No immutable first-party release is claimed by this source tree. Do not "
                "substitute a floating branch or invent a stable reference. After a human "
                "publishes a commit and tag, the [post-publication finalizer](RELEASE.md) builds "
                "an out-of-tree stable staging tree, both public skill archives, checksums, "
                "descriptor, and final bootstrap from the independently verified identity. "
                "It never rewrites source or Git.",
                "This stable distribution was generated out of tree from the independently "
                f"verified `{tag}` / `{commit}` identity. Do not substitute a floating branch, "
                "shortened hash, or different release reference. The finalizer did not rewrite "
                "the source checkout or Git.",
            ),
        ),
        "RELEASE.md": (
            (
                f"The tracked source tree remains truthfully `{source}` and unreleased. Stable "
                "identity is created only after publication in an out-of-tree distribution. "
                "The finalizer requires an exact `v<version>` tag, verifies that "
                "`refs/tags/<tag>`, the supplied full commit, and checkout `HEAD` resolve to the "
                "same commit, and stages bytes from that verified commit object. It never writes "
                "to the source checkout or Git, avoiding the impossible requirement that a "
                "commit contain its own hash.",
                f"This staged source belongs to published release `{tag}` at immutable commit "
                f"`{commit}`. The finalizer verified that the tag, supplied commit, and checkout "
                "`HEAD` resolved to that identity, then read every staged file directly from its "
                "Git blob object. It did not write to the source checkout or Git.",
            ),
            (
                f'python3 sync_release_material.py build --output "dist/{source}" --force',
                "python3 sync_release_material.py build --output "
                f'"dist/{identity.version}-candidate" --force',
            ),
            (
                f'python3 sync_release_material.py inspect --distribution "dist/{source}"',
                "python3 sync_release_material.py inspect --distribution "
                f'"dist/{identity.version}-candidate"',
            ),
        ),
        "CHANGELOG.md": (
            (
                f"## {source} — Unreleased",
                f"## {identity.version} — Released as {tag}",
            ),
            (
                "No tag or immutable commit is claimed for this unreleased version.",
                f"Published as `{tag}` at immutable commit `{commit}`.",
            ),
        ),
    }
    for relative, path_replacements in replacements.items():
        path = staging_root / relative
        text = path.read_text(encoding="utf-8")
        for old, new in path_replacements:
            if text.count(old) != 1:
                raise ReleaseSynchronizationError(
                    f"Stable documentation source drifted: {relative}"
                )
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    assert_stable_documentation(staging_root, identity)


def assert_stable_documentation(root: Path, identity: ReleaseIdentity) -> None:
    tag = str(identity.published_tag)
    commit = str(identity.immutable_commit)
    forbidden = {
        "README.md": (
            "explicitly unreleased",
            "Do not run a bootstrap containing `PROJECT_SETUP_RELEASE`",
            "No immutable first-party release is claimed",
        ),
        "RELEASE.md": (
            f"tracked source tree remains truthfully `{identity.version}` and unreleased",
        ),
        "CHANGELOG.md": (
            f"## {identity.version} — Unreleased",
            "No tag or immutable commit is claimed for this unreleased version.",
        ),
    }
    for relative, rejected_phrases in forbidden.items():
        text = (root / relative).read_text(encoding="utf-8")
        if tag not in text or commit not in text:
            raise ReleaseSynchronizationError(
                f"Stable documentation identity is incomplete: {relative}"
            )
        for phrase in rejected_phrases:
            if phrase in text:
                raise ReleaseSynchronizationError(
                    f"Stable documentation retains unreleased provenance: {relative}"
                )


def apply_material(root: Path, material: dict[Path, bytes]) -> None:
    for path, payload in material.items():
        atomic_write(path, payload)


def skill_package_payload(skill_root: Path) -> tuple[bytes, int]:
    files = [
        (
            f"{skill_root.name}/{path.relative_to(skill_root).as_posix()}",
            path.read_bytes(),
        )
        for path in skill_root.rglob("*")
        if is_runtime_package_file(path, skill_root)
    ]
    return deterministic_zip(files), len(files)


def bootstrap_payload(identity: ReleaseIdentity) -> tuple[str, bytes]:
    if identity.status == "stable":
        filename = "github-bootstrap.txt"
        heading = "Bootstrap this project from https://github.com/xmasterg/project-setup."
        tag = identity.published_tag
        commit = identity.immutable_commit
    else:
        filename = "github-bootstrap.template.txt"
        heading = (
            "UNRELEASED RELEASE CANDIDATE — DO NOT RUN\n"
            f"Version: {identity.version}\n"
            "This guarded template is not a published bootstrap. "
            "RELEASE_TAG and RELEASE_COMMIT are unresolved.\n\n"
            "Bootstrap this project from https://github.com/xmasterg/project-setup."
        )
        tag = "RELEASE_TAG"
        commit = "RELEASE_COMMIT"
    text = f"""{heading}

Verified stable tag: {tag}
Verified immutable commit: {commit}

Guard: stop immediately if the commit is not exactly 40 lowercase hexadecimal characters or if the tag does not resolve to that commit. Never substitute a branch, shortened hash, placeholder, or invented release reference.

First ask me: "Where should I set up the project? Please give me the exact absolute local folder path." Wait for my answer. Never infer the path from the current session, an open repository, or a previous directory.

Then ask me to choose exactly one setup type:
1. Local folder only (no Git).
2. Local folder + local Git repository.
3. Local folder + remote Git repository.
4. Existing remote Git repository cloned locally.

Do not ask about GitHub before I choose a remote option. For option 3, ask for the remote URL or whether to create one, including private or public when creating it. For option 4, ask for the repository URL. Ask for a one-sentence project purpose only when it is still unknown.

Verify the release identity and the `project-setup` and `task-tracking-setup` skill paths from a separate source checkout that does not overlap the target. Then execute the simple setup in `project-setup/SKILL.md`: create the complete AGENTS.md from its full template, project-management docs, README.md, .gitignore, app/ for a fresh project, and the bundled local task tracker. Preserve existing application paths in a cloned or non-empty project and ask before overwriting collisions. Do not expose coordinator modes, capability catalogs, instruction bundles, release pins, or preview tokens as user choices.

Apply Git changes only according to the selected option and only inside the confirmed target. Never mutate Git in the setup-source checkout, force-push, or rewrite history. Finish by reporting the local path, created files, tracker location, and Git result.
"""
    return filename, text.encode()


def source_metadata_inventory(source_root: Path, identity: ReleaseIdentity) -> list[dict[str, str]]:
    paths = (
        "releases/index.json",
        f"releases/{identity.version}/install-manifest.json",
        f"releases/{identity.version}/release-info.json",
        "project-setup/releases/index.json",
        f"project-setup/releases/{identity.version}/install-manifest.json",
        f"project-setup/releases/{identity.version}/release-info.json",
        "capabilities/catalog.yaml",
        "project-setup/assets/capabilities/catalog.yaml",
        "project-setup/assets/tracker-integration.json",
        "project-setup/assets/vendor/task-tracking-setup.zip",
        "task-tracking-setup/assets/install-manifest.json",
    )
    return [
        {"path": relative, "sha256": sha256((source_root / relative).read_bytes())}
        for relative in paths
    ]


def directory_inventory(root: Path) -> tuple[list[dict[str, str]], str]:
    if not root.is_dir() or root.is_symlink():
        raise ReleaseSynchronizationError(f"Inventory root must be a real directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReleaseSynchronizationError(f"Inventory contains a symlink: {path}")
        if path.is_file():
            files.append(path)
    inventory = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path.read_bytes()),
        }
        for path in files
    ]
    return inventory, inventory_checksum(inventory)


def inventory_checksum(inventory: list[dict[str, str]]) -> str:
    records = b"".join(
        record["path"].encode("utf-8")
        + b"\0"
        + record["sha256"].encode("ascii")
        + b"\n"
        for record in inventory
    )
    return sha256(records)


def build_distribution(
    source_root: Path,
    output: Path,
    identity: ReleaseIdentity,
    *,
    force: bool,
    verified_source: VerifiedGitSource | None = None,
) -> None:
    source_root = source_root.resolve()
    output = output.resolve()
    if not source_root.is_dir():
        raise ReleaseSynchronizationError(f"Source root does not exist: {source_root}")
    if output == source_root or output in source_root.parents:
        raise ReleaseSynchronizationError("Distribution output must not contain or replace source")
    if source_root in output.parents and output.relative_to(source_root).parts[0] != "dist":
        raise ReleaseSynchronizationError(
            "An output inside source is allowed only under the ignored dist directory"
        )
    if output.exists() and not force:
        raise ReleaseSynchronizationError(f"Output already exists; pass --force: {output}")
    if identity.status == "unreleased":
        source = source_version(source_root)
        if identity.version != source:
            raise ReleaseSynchronizationError("Unreleased build version must equal source VERSION")
        synchronize(check=True, root=source_root)
        if verified_source is not None:
            raise ReleaseSynchronizationError(
                "Unreleased builds must use the current working-tree source"
            )
    elif verified_source is None or verified_source.identity != identity:
        raise ReleaseSynchronizationError(
            "Stable distributions require a matching verified Git source"
        )
    else:
        current_verification = verify_git_identity(
            source_root,
            identity.version,
            str(identity.published_tag),
            str(identity.immutable_commit),
        )
        if current_verification != verified_source:
            raise ReleaseSynchronizationError(
                "Stable distribution verification does not match the source checkout"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="project-setup-release-", dir=output.parent) as temp:
        temporary_output = Path(temp) / output.name
        temporary_output.mkdir()
        staged_source = temporary_output / "source"
        if verified_source is None:
            copy_working_tree_source(source_root, staged_source)
        else:
            stage_verified_commit(verified_source, staged_source)
        source = source_version(staged_source)
        if identity.status == "stable":
            apply_stable_documentation_overlay(staged_source, source, identity)
        material = expected_release_material(
            staged_source,
            identity,
            source_version_override=source,
        )
        prune_release_directories(staged_source)
        apply_material(staged_source, material)

        packages: list[dict[str, Any]] = []
        checksum_lines: list[str] = []
        for skill_name in ("project-setup", "task-tracking-setup"):
            payload, file_count = skill_package_payload(staged_source / skill_name)
            filename = f"{skill_name}.skill"
            (temporary_output / filename).write_bytes(payload)
            package_checksum = sha256(payload)
            checksum_lines.append(f"{package_checksum}  {filename}\n")
            packages.append(
                {
                    "filename": filename,
                    "file_count": file_count,
                    "sha256": package_checksum,
                }
            )
        bootstrap_name, bootstrap = bootstrap_payload(identity)
        (temporary_output / bootstrap_name).write_bytes(bootstrap)
        checksum_lines.append(f"{sha256(bootstrap)}  {bootstrap_name}\n")
        checksums = "".join(checksum_lines).encode()
        (temporary_output / "SHA256SUMS").write_bytes(checksums)
        descriptor_name = (
            "release-descriptor.json"
            if identity.status == "stable"
            else "release-candidate.json"
        )
        source_inventory, source_checksum = directory_inventory(staged_source)
        metadata_inventory = source_metadata_inventory(staged_source, identity)
        descriptor = {
            "release_descriptor_schema_version": 3,
            "version": identity.version,
            "status": identity.status,
            "published_tag": identity.published_tag,
            "immutable_commit": identity.immutable_commit,
            "source_checkout_commit": identity.source_checkout_commit,
            "packages": packages,
            "checksums": {
                "filename": "SHA256SUMS",
                "sha256": sha256(checksums),
            },
            "bootstrap": {
                "filename": bootstrap_name,
                "sha256": sha256(bootstrap),
                "final": identity.status == "stable",
            },
            "source_tree": {
                "path": "source",
                "file_count": len(source_inventory),
                "sha256": source_checksum,
                "files": source_inventory,
            },
            "metadata": {
                "file_count": len(metadata_inventory),
                "files": metadata_inventory,
                "sha256": inventory_checksum(metadata_inventory),
            },
        }
        (temporary_output / descriptor_name).write_bytes(canonical_json(descriptor))
        inspect_distribution(temporary_output)
        if verified_source is not None:
            refreshed_verification = verify_git_identity(
                source_root,
                identity.version,
                str(identity.published_tag),
                str(identity.immutable_commit),
            )
            if refreshed_verification != verified_source:
                raise ReleaseSynchronizationError(
                    "Stable Git identity changed while the distribution was being built"
                )
        if output.exists():
            shutil.rmtree(output)
        os.replace(temporary_output, output)


def require_exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unexpected = sorted(set(value) - expected)
        raise ReleaseSynchronizationError(
            f"{label} schema differs (missing={missing}, unexpected={unexpected})"
        )


def parse_inventory_records(value: Any, *, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ReleaseSynchronizationError(f"{label} must be an array")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, record in enumerate(value, start=1):
        if not isinstance(record, dict):
            raise ReleaseSynchronizationError(f"{label} record {position} must be an object")
        require_exact_keys(record, {"path", "sha256"}, label=f"{label} record {position}")
        path = record.get("path")
        checksum = record.get("sha256")
        if not isinstance(path, str):
            raise ReleaseSynchronizationError(f"{label} record {position} path is invalid")
        parse_safe_repository_path(path, label=label)
        if path in seen:
            raise ReleaseSynchronizationError(f"{label} contains duplicate path: {path}")
        if not isinstance(checksum, str) or not SHA256_PATTERN.fullmatch(checksum):
            raise ReleaseSynchronizationError(f"{label} checksum is invalid for {path}")
        seen.add(path)
        records.append({"path": path, "sha256": checksum})
    return records


def expected_package_files(source_root: Path, skill_name: str) -> dict[str, bytes]:
    skill_root = source_root / skill_name
    return {
        f"{skill_name}/{path.relative_to(skill_root).as_posix()}": path.read_bytes()
        for path in sorted(skill_root.rglob("*"))
        if is_runtime_package_file(path, skill_root)
    }


def inspect_distribution(distribution: Path) -> None:
    if not distribution.is_dir() or distribution.is_symlink():
        raise ReleaseSynchronizationError(
            f"Distribution directory does not exist or is unsafe: {distribution}"
        )
    descriptor_paths = [
        path
        for path in (
            distribution / "release-descriptor.json",
            distribution / "release-candidate.json",
        )
        if path.is_file() and not path.is_symlink()
    ]
    if len(descriptor_paths) != 1:
        raise ReleaseSynchronizationError("Distribution requires exactly one descriptor")
    descriptor_path = descriptor_paths[0]
    descriptor = read_json(descriptor_path)
    require_exact_keys(descriptor, DESCRIPTOR_TOP_LEVEL_KEYS, label="Release descriptor")
    if descriptor.get("release_descriptor_schema_version") != 3:
        raise ReleaseSynchronizationError("Release descriptor schema version must be 3")
    version = descriptor.get("version")
    status = descriptor.get("status")
    if not isinstance(version, str) or status not in {"unreleased", "stable"}:
        raise ReleaseSynchronizationError("Release descriptor version or status is invalid")
    identity = ReleaseIdentity(
        version,
        status,
        descriptor.get("published_tag"),
        descriptor.get("immutable_commit"),
        descriptor.get("source_checkout_commit"),
    )
    identity.assert_valid()
    expected_descriptor_name = (
        "release-descriptor.json" if status == "stable" else "release-candidate.json"
    )
    if descriptor_path.name != expected_descriptor_name:
        raise ReleaseSynchronizationError("Descriptor filename and release status differ")

    packages = descriptor.get("packages")
    if not isinstance(packages, list) or len(packages) != 2:
        raise ReleaseSynchronizationError("Descriptor must declare exactly two packages")
    package_records: dict[str, dict[str, Any]] = {}
    for position, package in enumerate(packages, start=1):
        if not isinstance(package, dict):
            raise ReleaseSynchronizationError(f"Package descriptor {position} must be an object")
        require_exact_keys(
            package,
            {"filename", "file_count", "sha256"},
            label=f"Package descriptor {position}",
        )
        filename = package.get("filename")
        if filename not in {"project-setup.skill", "task-tracking-setup.skill"}:
            raise ReleaseSynchronizationError(f"Unexpected package filename: {filename}")
        if filename in package_records:
            raise ReleaseSynchronizationError(f"Duplicate package descriptor: {filename}")
        if type(package.get("file_count")) is not int or package["file_count"] < 1:
            raise ReleaseSynchronizationError(f"Package file count is invalid: {filename}")
        if not isinstance(package.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
            package["sha256"]
        ):
            raise ReleaseSynchronizationError(f"Package checksum is invalid: {filename}")
        package_records[filename] = package

    checksums_descriptor = descriptor.get("checksums")
    if not isinstance(checksums_descriptor, dict):
        raise ReleaseSynchronizationError("Checksums descriptor must be an object")
    require_exact_keys(
        checksums_descriptor, {"filename", "sha256"}, label="Checksums descriptor"
    )
    if checksums_descriptor.get("filename") != "SHA256SUMS":
        raise ReleaseSynchronizationError("Checksums filename must be SHA256SUMS")
    sums_path = distribution / "SHA256SUMS"
    sums_payload = sums_path.read_bytes()
    if checksums_descriptor.get("sha256") != sha256(sums_payload):
        raise ReleaseSynchronizationError("SHA256SUMS checksum differs from descriptor")

    bootstrap_descriptor = descriptor.get("bootstrap")
    if not isinstance(bootstrap_descriptor, dict):
        raise ReleaseSynchronizationError("Bootstrap descriptor must be an object")
    require_exact_keys(
        bootstrap_descriptor,
        {"filename", "final", "sha256"},
        label="Bootstrap descriptor",
    )
    expected_bootstrap_name = (
        "github-bootstrap.txt" if status == "stable" else "github-bootstrap.template.txt"
    )
    if (
        bootstrap_descriptor.get("filename") != expected_bootstrap_name
        or bootstrap_descriptor.get("final") is not (status == "stable")
    ):
        raise ReleaseSynchronizationError("Bootstrap semantics differ from release status")
    bootstrap_path = distribution / expected_bootstrap_name
    bootstrap = bootstrap_path.read_bytes()
    if bootstrap_descriptor.get("sha256") != sha256(bootstrap):
        raise ReleaseSynchronizationError("Bootstrap checksum differs from descriptor")

    checksum_records: dict[str, str] = {}
    try:
        checksum_lines = sums_payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseSynchronizationError("SHA256SUMS must be valid UTF-8") from exc
    for line in checksum_lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match:
            raise ReleaseSynchronizationError(f"Malformed SHA256SUMS line: {line!r}")
        filename = match.group(2)
        if filename in checksum_records:
            raise ReleaseSynchronizationError(f"Duplicate SHA256SUMS record: {filename}")
        checksum_records[filename] = match.group(1)
    expected_checksum_names = set(package_records) | {expected_bootstrap_name}
    if set(checksum_records) != expected_checksum_names:
        raise ReleaseSynchronizationError("SHA256SUMS inventory differs from descriptor")

    for filename, package in package_records.items():
        package_path = distribution / filename
        package_payload = package_path.read_bytes()
        package_checksum = sha256(package_payload)
        if package["sha256"] != package_checksum or checksum_records[filename] != package_checksum:
            raise ReleaseSynchronizationError(f"Package checksum differs for {filename}")
        skill_name = filename.removesuffix(".skill")
        expected_files = expected_package_files(distribution / "source", skill_name)
        expected_package_payload = deterministic_zip(expected_files.items())
        if package_payload != expected_package_payload:
            raise ReleaseSynchronizationError(
                f"Package bytes are not the deterministic build output: {filename}"
            )
        with zipfile.ZipFile(io.BytesIO(package_payload)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ReleaseSynchronizationError(f"Package contains duplicate paths: {filename}")
            if names != sorted(names) or any(
                member.date_time != FIXED_ZIP_TIMESTAMP or member.is_dir()
                for member in members
            ):
                raise ReleaseSynchronizationError(
                    f"Package inventory is not deterministic: {filename}"
                )
            if len(names) != package["file_count"] or set(names) != set(expected_files):
                raise ReleaseSynchronizationError(
                    f"Package inventory differs from staged source: {filename}"
                )
            for member in members:
                parse_safe_repository_path(member.filename, label="package")
                payload = archive.read(member)
                if payload != expected_files[member.filename]:
                    raise ReleaseSynchronizationError(
                        f"Package payload differs from staged source: {member.filename}"
                    )
        if checksum_records[filename] != package["sha256"]:
            raise ReleaseSynchronizationError(
                f"Package descriptor and SHA256SUMS differ: {filename}"
            )
    if checksum_records[expected_bootstrap_name] != sha256(bootstrap):
        raise ReleaseSynchronizationError("Bootstrap SHA256SUMS record differs")

    source_tree = descriptor.get("source_tree")
    if not isinstance(source_tree, dict):
        raise ReleaseSynchronizationError("Distribution source tree descriptor is invalid")
    require_exact_keys(
        source_tree,
        {"path", "file_count", "files", "sha256"},
        label="Source tree descriptor",
    )
    declared_source_inventory = parse_inventory_records(
        source_tree.get("files"), label="Source tree inventory"
    )
    actual_source_inventory, actual_source_checksum = directory_inventory(
        distribution / "source"
    )
    if (
        source_tree.get("path") != "source"
        or type(source_tree.get("file_count")) is not int
        or source_tree["file_count"] != len(actual_source_inventory)
        or source_tree.get("sha256") != actual_source_checksum
        or declared_source_inventory != actual_source_inventory
    ):
        raise ReleaseSynchronizationError("Distribution source tree inventory has drifted")

    metadata_descriptor = descriptor.get("metadata")
    if not isinstance(metadata_descriptor, dict):
        raise ReleaseSynchronizationError("Metadata descriptor must be an object")
    require_exact_keys(
        metadata_descriptor,
        {"file_count", "files", "sha256"},
        label="Metadata descriptor",
    )
    declared_metadata = parse_inventory_records(
        metadata_descriptor.get("files"), label="Metadata inventory"
    )
    actual_metadata = source_metadata_inventory(distribution / "source", identity)
    if (
        type(metadata_descriptor.get("file_count")) is not int
        or metadata_descriptor["file_count"] != len(actual_metadata)
        or metadata_descriptor.get("sha256") != inventory_checksum(actual_metadata)
        or declared_metadata != actual_metadata
    ):
        raise ReleaseSynchronizationError("Distribution metadata inventory has drifted")
    expected_material = expected_release_material(distribution / "source", identity)
    for path, expected_payload in expected_material.items():
        if path.read_bytes() != expected_payload:
            relative = path.relative_to(distribution / "source").as_posix()
            raise ReleaseSynchronizationError(f"Release metadata semantic drift: {relative}")

    expected_top_level = {
        "source",
        expected_descriptor_name,
        "SHA256SUMS",
        expected_bootstrap_name,
        *package_records,
    }
    actual_top_level = {path.name for path in distribution.iterdir()}
    if actual_top_level != expected_top_level:
        raise ReleaseSynchronizationError("Distribution top-level inventory has drifted")

    if status == "stable":
        assert_stable_documentation(distribution / "source", identity)
        if identity.published_tag.encode() not in bootstrap or identity.immutable_commit.encode() not in bootstrap:
            raise ReleaseSynchronizationError("Stable bootstrap identity is incomplete")
        for record in actual_source_inventory:
            payload = (distribution / "source" / record["path"]).read_bytes()
            allowed_tokens = STABLE_LITERAL_TOKEN_ALLOWLIST.get(
                record["path"], frozenset()
            )
            if any(
                token in payload and token not in allowed_tokens
                for token in PLACEHOLDER_TOKENS
            ):
                raise ReleaseSynchronizationError(
                    f"Stable source contains a release placeholder: {record['path']}"
                )
            if re.search(rb"\b\d+\.\d+\.\d+-dev\.\d+\b", payload):
                raise ReleaseSynchronizationError(
                    f"Stable source contains a development version: {record['path']}"
                )
    elif not all(token in bootstrap for token in (b"RELEASE_TAG", b"RELEASE_COMMIT")):
        raise ReleaseSynchronizationError(
            "Unreleased bootstrap template must retain unresolved identity placeholders"
        )


def verify_git_identity(
    source_root: Path, version: str, tag: str, commit: str
) -> VerifiedGitSource:
    if not STABLE_VERSION_PATTERN.fullmatch(version):
        raise ReleaseSynchronizationError(
            "Stable version must be a final major.minor.patch semantic version"
        )
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ReleaseSynchronizationError(
            f"Stable published tag must be exactly {expected_tag} for version {version}"
        )
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseSynchronizationError(
            "Stable immutable commit must be exactly 40 lowercase hexadecimal characters"
        )
    source_root = source_root.resolve()
    status = run_git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    )
    assert isinstance(status, bytes)
    if status:
        first_record = status.splitlines()[0].decode("utf-8", errors="replace")
        raise ReleaseSynchronizationError(
            f"Stable source checkout must be clean with no tracked, untracked, or ignored contamination: {first_record}"
        )
    tag_ref = f"refs/tags/{tag}"
    head = run_git(source_root, "rev-parse", "--verify", "HEAD^{commit}", text=True)
    resolved_tag = run_git(
        source_root,
        "rev-parse",
        "--verify",
        f"{tag_ref}^{{commit}}",
        text=True,
    )
    assert isinstance(head, str) and isinstance(resolved_tag, str)
    if head.strip() != commit or resolved_tag.strip() != commit:
        raise ReleaseSynchronizationError(
            "Supplied commit, checkout HEAD, and exact published tag ref do not resolve to one identity"
        )
    identity = ReleaseIdentity._verified_stable(version, tag, commit, commit)
    return VerifiedGitSource(source_root, identity)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize, build, finalize, and inspect deterministic release material"
    )
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    sync = commands.add_parser("sync")
    sync.add_argument("--check", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, default=REPOSITORY_ROOT)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--force", action="store_true")
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--source", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--version", required=True)
    finalize.add_argument("--tag", required=True)
    finalize.add_argument("--commit", required=True)
    finalize.add_argument("--force", action="store_true")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--distribution", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command is None:
            synchronize(check=args.check)
        elif args.command == "sync":
            synchronize(check=args.check)
        elif args.command == "build":
            version = source_version(args.source)
            build_distribution(
                args.source,
                args.output,
                ReleaseIdentity.unreleased(version),
                force=args.force,
            )
        elif args.command == "finalize":
            verified_source = verify_git_identity(
                args.source, args.version, args.tag, args.commit
            )
            build_distribution(
                args.source,
                args.output,
                verified_source.identity,
                force=args.force,
                verified_source=verified_source,
            )
        else:
            inspect_distribution(args.distribution)
    except (OSError, ReleaseSynchronizationError, zipfile.BadZipFile) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

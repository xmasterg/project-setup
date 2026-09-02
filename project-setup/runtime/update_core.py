#!/usr/bin/env python3
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


STATE_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 2
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_POLICIES = {"managed", "managed_block", "seed", "user_data", "generated"}


class UpdateError(Exception):
    """Base class for deterministic coordinator failures."""


class ValidationError(UpdateError):
    """Input, manifest, state, or filesystem validation failed."""


class UpdateConflict(UpdateError):
    """A target customization or ownership conflict blocks the transaction."""


class LockBusy(UpdateError):
    """Another mutating coordinator process owns the lock."""


class FaultInjected(RuntimeError):
    """Test-only interruption raised through an injected callback."""


@dataclass(frozen=True)
class ManagedBlock:
    start_marker: str
    end_marker: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    component: str
    source: str | None
    target: str
    policy: str
    sha256: str | None
    action: str
    block: ManagedBlock | None


@dataclass(frozen=True)
class ReleaseManifest:
    manifest_schema_version: int
    bundle_version: str
    release_status: str
    component_versions: dict[str, str]
    tracker_data_schema_version: int | None
    board_data_version: int | None
    migrations: tuple[dict[str, Any], ...]
    rollback: dict[str, Any]
    artifacts: tuple[Artifact, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class PlannedOperation:
    artifact_id: str
    policy: str
    action: str
    target: str
    candidate_path: Path | None
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class UpdatePlan:
    operations: tuple[PlannedOperation, ...]
    conflicts: tuple[dict[str, str], ...]
    artifact_state: tuple[dict[str, Any], ...]


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(read_regular_file(path))


def parse_project_relative_path(raw_path: Any, *, label: str) -> str:
    if not isinstance(raw_path, str):
        raise ValidationError(f"{label} must be a string")
    if not raw_path or raw_path == ".":
        raise ValidationError(f"{label} must be a non-empty project-relative path")
    if "\x00" in raw_path:
        raise ValidationError(f"{label} contains NUL")
    if "\\" in raw_path:
        raise ValidationError(f"{label} must use forward slashes")
    if raw_path.startswith("/") or re.match(r"^[A-Za-z]:", raw_path):
        raise ValidationError(f"{label} must not be absolute: {raw_path}")

    path = PurePosixPath(raw_path)
    if any(part in {"", ".", ".."} for part in raw_path.split("/")):
        raise ValidationError(f"{label} contains an empty, dot, or parent component: {raw_path}")
    normalized = path.as_posix()
    if normalized != raw_path:
        raise ValidationError(f"{label} is not normalized: {raw_path}")
    return normalized


def validate_unique_targets(paths: Iterable[str]) -> None:
    exact: dict[str, str] = {}
    folded: dict[str, str] = {}
    for raw_path in paths:
        normalized = parse_project_relative_path(raw_path, label="artifact target")
        if normalized in exact:
            raise ValidationError(f"Duplicate artifact target: {normalized}")
        casefolded = normalized.casefold()
        if casefolded in folded:
            raise ValidationError(
                f"Case-fold artifact target collision: {folded[casefolded]} and {normalized}"
            )
        exact[normalized] = normalized
        folded[casefolded] = normalized
    ordered = sorted(exact, key=lambda item: (len(PurePosixPath(item).parts), item.casefold()))
    for position, parent in enumerate(ordered):
        parent_parts = tuple(part.casefold() for part in PurePosixPath(parent).parts)
        for child in ordered[position + 1 :]:
            child_parts = tuple(part.casefold() for part in PurePosixPath(child).parts)
            if child_parts[: len(parent_parts)] == parent_parts:
                raise ValidationError(f"Artifact target is an ancestor of another target: {parent}")


def validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValidationError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def assert_root_directory(root: Path, *, label: str) -> Path:
    if not root.is_absolute():
        raise ValidationError(f"{label} must be an absolute path: {root}")
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} does not exist: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValidationError(f"{label} must not be a symlink: {root}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"{label} must be a directory: {root}")
    return root


def assert_no_symlink_components(
    root: Path,
    relative_path: str,
    *,
    allow_missing: bool,
    require_final_file: bool = False,
) -> Path:
    assert_root_directory(root, label="path root")
    normalized = parse_project_relative_path(relative_path, label="project-relative path")
    current = root
    parts = PurePosixPath(normalized).parts
    missing_seen = False
    for position, part in enumerate(parts):
        current = current / part
        if missing_seen:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise ValidationError(f"Required path does not exist: {current}")
            missing_seen = True
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"Symlinked path component is not allowed: {current}")
        is_final = position == len(parts) - 1
        if not is_final and not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(f"Path component is not a directory: {current}")
        if is_final and require_final_file and not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"Path is not a regular file: {current}")
    return current


def read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValidationError(f"Required file does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"Expected a non-symlink regular file: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValidationError(f"Expected a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_file(path).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain a JSON object: {path}")
    return value


def parse_managed_block(raw: Any, *, artifact_id: str) -> ManagedBlock:
    if not isinstance(raw, dict):
        raise ValidationError(f"Artifact {artifact_id} managed_block requires block markers")
    start_marker = raw.get("start_marker")
    end_marker = raw.get("end_marker")
    if not isinstance(start_marker, str) or not start_marker:
        raise ValidationError(f"Artifact {artifact_id} has an invalid start marker")
    if not isinstance(end_marker, str) or not end_marker or end_marker == start_marker:
        raise ValidationError(f"Artifact {artifact_id} has an invalid end marker")
    return ManagedBlock(start_marker=start_marker, end_marker=end_marker)


def parse_release_manifest(raw: dict[str, Any]) -> ReleaseManifest:
    if raw.get("manifest_schema_version") != 1:
        raise ValidationError("install manifest must use manifest_schema_version 1")
    bundle_version = raw.get("bundle_version")
    if not isinstance(bundle_version, str) or not bundle_version:
        raise ValidationError("install manifest bundle_version must be a non-empty string")
    release_status = raw.get("release_status")
    if release_status not in {"unreleased", "prerelease", "stable"}:
        raise ValidationError("install manifest release_status is invalid")
    component_versions = raw.get("component_versions")
    if not isinstance(component_versions, dict) or not component_versions:
        raise ValidationError("install manifest component_versions must be a non-empty object")
    for component, version in component_versions.items():
        if not isinstance(component, str) or not component or not isinstance(version, str) or not version:
            raise ValidationError("component_versions must map non-empty strings to versions")
    includes_tracker = "task-tracking-setup" in component_versions
    tracker_schema = raw.get("tracker_data_schema_version")
    board_data_version = raw.get("board_data_version")
    if includes_tracker:
        if not isinstance(tracker_schema, int) or tracker_schema < 1:
            raise ValidationError("tracker_data_schema_version must be a positive integer")
        if not isinstance(board_data_version, int) or board_data_version < 1:
            raise ValidationError("board_data_version must be a positive integer")
    elif tracker_schema is not None or board_data_version is not None:
        raise ValidationError(
            "Tracker schema versions require task-tracking-setup in component_versions"
        )
    migrations = raw.get("migrations")
    if not isinstance(migrations, list):
        raise ValidationError("install manifest migrations must be an array")
    migration_ids: set[str] = set()
    for migration in migrations:
        if not isinstance(migration, dict) or not isinstance(migration.get("id"), str):
            raise ValidationError("Every migration requires a string id")
        if migration["id"] in migration_ids:
            raise ValidationError(f"Duplicate migration id: {migration['id']}")
        if migration.get("rollback") not in {"restore_backup", "not_applicable"}:
            raise ValidationError(f"Migration {migration['id']} requires a rollback declaration")
        migration_component = migration.get("component")
        if migration_component is not None and migration_component not in component_versions:
            raise ValidationError(
                f"Migration {migration['id']} references unknown component: {migration_component}"
            )
        migration_ids.add(migration["id"])
    rollback = raw.get("rollback")
    if not isinstance(rollback, dict) or rollback.get("mode") != "one_release":
        raise ValidationError("install manifest rollback.mode must be one_release")
    if not isinstance(rollback.get("declaration"), str) or not rollback["declaration"]:
        raise ValidationError("install manifest rollback requires a declaration")

    policy_declarations = raw.get("artifact_policies")
    if not isinstance(policy_declarations, dict) or set(policy_declarations) != ARTIFACT_POLICIES:
        raise ValidationError(
            "install manifest artifact_policies must declare managed, managed_block, seed, "
            "user_data, and generated"
        )
    if any(not isinstance(description, str) or not description for description in policy_declarations.values()):
        raise ValidationError("Every artifact policy requires a non-empty declaration")
    source_identity = raw.get("source_identity")
    if not isinstance(source_identity, dict) or source_identity.get("kind") != "verified_local":
        raise ValidationError("install manifest source_identity.kind must be verified_local")
    published_tag = raw.get("published_tag", source_identity.get("published_tag"))
    immutable_commit = raw.get("immutable_commit", source_identity.get("immutable_commit"))
    source_checkout_commit = source_identity.get("source_checkout_commit")
    if release_status == "stable":
        if not isinstance(published_tag, str) or not published_tag.strip():
            raise ValidationError("Stable manifests require a non-placeholder published_tag")
        if any(token in published_tag.upper() for token in ("PLACEHOLDER", "RELEASE_TAG")):
            raise ValidationError("Stable manifests require a non-placeholder published_tag")
        if published_tag != f"v{bundle_version}":
            raise ValidationError(
                "Stable manifest published_tag must be exactly v<bundle_version>"
            )
        if not isinstance(immutable_commit, str) or not COMMIT_PATTERN.fullmatch(
            immutable_commit
        ):
            raise ValidationError(
                "Stable manifests require an immutable_commit with 40 lowercase hex characters"
            )
        if source_checkout_commit != immutable_commit:
            raise ValidationError(
                "Stable manifest source checkout commit must match immutable_commit"
            )
        if source_identity.get("published_tag") != published_tag:
            raise ValidationError("Stable manifest published_tag identity is inconsistent")
        if raw.get("published_tag") != published_tag:
            raise ValidationError("Stable manifest published_tag identity is inconsistent")
        if raw.get("immutable_commit") != immutable_commit:
            raise ValidationError("Stable manifest immutable_commit identity is inconsistent")
        if "-dev." in bundle_version:
            raise ValidationError("Stable manifest bundle_version must not be a development version")
    elif any(
        value is not None
        for value in (published_tag, immutable_commit, source_checkout_commit)
    ):
        raise ValidationError("Unreleased manifests must not claim stable identity")
    artifacts_raw = raw.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise ValidationError("install manifest artifacts must be an array")

    artifacts: list[Artifact] = []
    artifact_ids: set[str] = set()
    targets: list[str] = []
    for position, artifact_raw in enumerate(artifacts_raw, start=1):
        if not isinstance(artifact_raw, dict):
            raise ValidationError(f"Artifact {position} must be an object")
        artifact_id = artifact_raw.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValidationError(f"Artifact {position} requires a non-empty id")
        if artifact_id in artifact_ids:
            raise ValidationError(f"Duplicate artifact id: {artifact_id}")
        artifact_ids.add(artifact_id)
        component = artifact_raw.get("component")
        if component not in component_versions:
            raise ValidationError(f"Artifact {artifact_id} references unknown component: {component}")
        target = parse_project_relative_path(
            artifact_raw.get("target"), label=f"Artifact {artifact_id} target"
        )
        targets.append(target)
        policy = artifact_raw.get("policy")
        if policy not in ARTIFACT_POLICIES:
            raise ValidationError(f"Artifact {artifact_id} has invalid policy: {policy}")
        action = artifact_raw.get("action", "install")
        if action not in {"install", "retire"}:
            raise ValidationError(f"Artifact {artifact_id} has invalid action: {action}")
        source = artifact_raw.get("source")
        checksum = artifact_raw.get("sha256")
        if action == "install":
            source = parse_project_relative_path(source, label=f"Artifact {artifact_id} source")
            checksum = validate_sha256(checksum, label=f"Artifact {artifact_id} sha256")
        elif source is not None or checksum is not None:
            raise ValidationError(f"Retired artifact {artifact_id} must not declare source or sha256")
        if action == "retire" and policy in {"seed", "user_data"}:
            raise ValidationError(f"Artifact {artifact_id} cannot retire {policy} data")
        block = None
        if policy == "managed_block":
            block = parse_managed_block(artifact_raw.get("block"), artifact_id=artifact_id)
        elif "block" in artifact_raw:
            raise ValidationError(f"Artifact {artifact_id} declares block markers for {policy}")
        artifacts.append(
            Artifact(
                artifact_id=artifact_id,
                component=component,
                source=source,
                target=target,
                policy=policy,
                sha256=checksum,
                action=action,
                block=block,
            )
        )
    validate_unique_targets(targets)
    return ReleaseManifest(
        manifest_schema_version=1,
        bundle_version=bundle_version,
        release_status=release_status,
        component_versions=dict(sorted(component_versions.items())),
        tracker_data_schema_version=tracker_schema,
        board_data_version=board_data_version,
        migrations=tuple(migrations),
        rollback=rollback,
        artifacts=tuple(sorted(artifacts, key=lambda item: (item.target.casefold(), item.artifact_id))),
        raw=raw,
    )


def locate_block(text: str, block: ManagedBlock, *, label: str) -> tuple[int, int]:
    start_count = text.count(block.start_marker)
    end_count = text.count(block.end_marker)
    if start_count != 1 or end_count != 1:
        raise UpdateConflict(
            f"{label} requires exactly one {block.start_marker!r}/{block.end_marker!r} pair"
        )
    start = text.index(block.start_marker)
    end = text.index(block.end_marker)
    if end < start:
        raise UpdateConflict(f"{label} managed markers are misordered")
    return start, end + len(block.end_marker)


def managed_block_bytes(payload: bytes, block: ManagedBlock, *, label: str) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateConflict(f"{label} must be UTF-8 for managed block updates") from exc
    start, end = locate_block(text, block, label=label)
    return text[start:end].encode("utf-8")


def replace_managed_block(
    current_payload: bytes,
    candidate_payload: bytes,
    block: ManagedBlock,
    *,
    label: str,
) -> bytes:
    try:
        current = current_payload.decode("utf-8")
        candidate = candidate_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateConflict(f"{label} must be UTF-8 for managed block updates") from exc
    current_start, current_end = locate_block(current, block, label=label)
    candidate_start, candidate_end = locate_block(candidate, block, label=f"{label} candidate")
    updated = current[:current_start] + candidate[candidate_start:candidate_end] + current[current_end:]
    return updated.encode("utf-8")


def current_file_payload(root: Path, target: str) -> bytes | None:
    path = assert_no_symlink_components(root, target, allow_missing=True)
    if not path.exists():
        return None
    return read_regular_file(path)


def stage_release_candidates(
    source_root: Path,
    manifest: ReleaseManifest,
    staging_root: Path,
) -> dict[str, Path]:
    assert_root_directory(source_root, label="release source")
    candidates: dict[str, Path] = {}
    for artifact in manifest.artifacts:
        if artifact.action == "retire":
            continue
        assert artifact.source is not None
        source_path = assert_no_symlink_components(
            source_root,
            artifact.source,
            allow_missing=False,
            require_final_file=True,
        )
        payload = read_regular_file(source_path)
        actual_checksum = sha256_bytes(payload)
        if actual_checksum != artifact.sha256:
            raise ValidationError(
                f"Artifact {artifact.artifact_id} checksum mismatch: "
                f"expected {artifact.sha256}, got {actual_checksum}"
            )
        if artifact.block:
            managed_block_bytes(payload, artifact.block, label=f"Artifact {artifact.artifact_id}")
        staged_path = staging_root / artifact.artifact_id
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(payload)
        candidates[artifact.artifact_id] = staged_path
    return candidates


def plan_release_update(
    root: Path,
    manifest: ReleaseManifest,
    candidates: dict[str, Path],
    state: dict[str, Any] | None,
) -> UpdatePlan:
    state_artifacts = {
        record["id"]: record
        for record in (state or {}).get("artifacts", [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    operations: list[PlannedOperation] = []
    conflicts: list[dict[str, str]] = []
    artifact_state: list[dict[str, Any]] = []

    for artifact in manifest.artifacts:
        previous = state_artifacts.get(artifact.artifact_id)
        try:
            current_payload = current_file_payload(root, artifact.target)
        except ValidationError as exc:
            conflicts.append({"artifact_id": artifact.artifact_id, "target": artifact.target, "reason": str(exc)})
            continue
        current_checksum = sha256_bytes(current_payload) if current_payload is not None else None

        if artifact.action == "retire":
            if previous is None:
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": "retirement requires a recorded owned artifact",
                    }
                )
                continue
            baseline = previous.get("installed_sha256")
            if current_checksum != baseline:
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": "retired artifact differs from its recorded baseline",
                    }
                )
                continue
            operations.append(
                PlannedOperation(
                    artifact_id=artifact.artifact_id,
                    policy=artifact.policy,
                    action="delete",
                    target=artifact.target,
                    candidate_path=None,
                    before_sha256=current_checksum,
                    after_sha256=None,
                )
            )
            continue

        candidate_path = candidates[artifact.artifact_id]
        candidate_payload = read_regular_file(candidate_path)
        candidate_checksum = sha256_bytes(candidate_payload)
        next_payload = candidate_payload

        if artifact.policy == "managed_block":
            assert artifact.block is not None
            if current_payload is None:
                next_payload = candidate_payload
                current_block_checksum = None
            else:
                try:
                    current_block = managed_block_bytes(
                        current_payload, artifact.block, label=f"Artifact {artifact.artifact_id} target"
                    )
                    current_block_checksum = sha256_bytes(current_block)
                    next_payload = replace_managed_block(
                        current_payload,
                        candidate_payload,
                        artifact.block,
                        label=f"Artifact {artifact.artifact_id}",
                    )
                except UpdateConflict as exc:
                    conflicts.append(
                        {
                            "artifact_id": artifact.artifact_id,
                            "target": artifact.target,
                            "reason": str(exc),
                        }
                    )
                    continue
            if previous is not None and current_block_checksum != previous.get("installed_sha256"):
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": "managed block was customized after installation",
                    }
                )
                continue
            if previous is None and current_payload is not None:
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": "managed block has no recorded ownership baseline",
                    }
                )
                continue
            installed_checksum = sha256_bytes(
                managed_block_bytes(candidate_payload, artifact.block, label=artifact.artifact_id)
            )
            after_checksum = sha256_bytes(next_payload)
        elif artifact.policy in {"seed", "user_data"} and current_payload is not None:
            next_payload = current_payload
            installed_checksum = current_checksum
            after_checksum = current_checksum
        else:
            baseline = previous.get("installed_sha256") if previous else None
            if previous is not None and current_checksum != baseline:
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": f"{artifact.policy} artifact was customized after installation",
                    }
                )
                continue
            if previous is None and current_payload is not None and current_checksum != candidate_checksum:
                conflicts.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target": artifact.target,
                        "reason": "existing file has no recorded ownership baseline",
                    }
                )
                continue
            installed_checksum = sha256_bytes(next_payload)
            after_checksum = installed_checksum

        staged_target = candidate_path.parent / f"{candidate_path.name}.target"
        staged_target.write_bytes(next_payload)
        if current_payload != next_payload:
            operations.append(
                PlannedOperation(
                    artifact_id=artifact.artifact_id,
                    policy=artifact.policy,
                    action="write",
                    target=artifact.target,
                    candidate_path=staged_target,
                    before_sha256=current_checksum,
                    after_sha256=after_checksum,
                )
            )
        artifact_state.append(
            {
                "id": artifact.artifact_id,
                "component": artifact.component,
                "component_version": manifest.component_versions[artifact.component],
                "target": artifact.target,
                "policy": artifact.policy,
                "installed_sha256": installed_checksum,
                "source_sha256": artifact.sha256,
            }
        )

    return UpdatePlan(
        operations=tuple(sorted(operations, key=lambda item: (item.target.casefold(), item.artifact_id))),
        conflicts=tuple(sorted(conflicts, key=lambda item: (item["target"].casefold(), item["artifact_id"]))),
        artifact_state=tuple(sorted(artifact_state, key=lambda item: item["id"])),
    )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_safe_parent(root: Path, relative_path: str) -> Path:
    target = assert_no_symlink_components(root, relative_path, allow_missing=True)
    missing: list[Path] = []
    parent = target.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if parent != root:
        relative_parent = parent.relative_to(root).as_posix()
        assert_no_symlink_components(root, relative_parent, allow_missing=False)
    for directory in reversed(missing):
        directory.mkdir()
        fsync_directory(directory.parent)
    assert_no_symlink_components(root, relative_path, allow_missing=True)
    return target


def atomic_write_under_root(root: Path, relative_path: str, payload: bytes) -> None:
    target = ensure_safe_parent(root, relative_path)
    assert_no_symlink_components(root, relative_path, allow_missing=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        assert_no_symlink_components(root, relative_path, allow_missing=True)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def unlink_under_root(root: Path, relative_path: str) -> None:
    target = assert_no_symlink_components(root, relative_path, allow_missing=True)
    if not target.exists():
        return
    if not target.is_file():
        raise ValidationError(f"Refusing to remove non-file target: {target}")
    target.unlink()
    fsync_directory(target.parent)


def remove_empty_runtime_parents(root: Path, relative_path: str) -> None:
    stop = assert_no_symlink_components(
        root,
        ".agents/project_management/setup/.runtime",
        allow_missing=False,
    )
    current = assert_no_symlink_components(root, relative_path, allow_missing=True).parent
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        fsync_directory(current.parent)
        current = current.parent


class CoordinatorLock:
    def __init__(self, root: Path, lock_relative: str) -> None:
        self.root = root
        self.lock_relative = parse_project_relative_path(lock_relative, label="lock path")
        self.descriptor: int | None = None

    def __enter__(self) -> "CoordinatorLock":
        lock_path = ensure_safe_parent(self.root, self.lock_relative)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.descriptor)
            self.descriptor = None
            raise LockBusy(f"Coordinator lock is already held: {lock_path}") from exc
        payload = canonical_json_bytes({"pid": os.getpid()})
        os.ftruncate(self.descriptor, 0)
        os.write(self.descriptor, payload)
        os.fsync(self.descriptor)
        fsync_directory(lock_path.parent)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None

    @staticmethod
    def is_held(root: Path, lock_relative: str) -> bool:
        relative = parse_project_relative_path(lock_relative, label="lock path")
        lock_path = assert_no_symlink_components(root, relative, allow_missing=True)
        if not lock_path.exists():
            return False
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)


def transaction_id_for(
    manifest_sha256: str,
    operations: Iterable[PlannedOperation],
    previous_state: dict[str, Any] | None,
) -> str:
    identity = {
        "manifest_sha256": manifest_sha256,
        "operations": [
            {
                "artifact_id": operation.artifact_id,
                "action": operation.action,
                "target": operation.target,
                "before": operation.before_sha256,
                "after": operation.after_sha256,
            }
            for operation in operations
        ],
        "previous_state_sha256": sha256_bytes(canonical_json_bytes(previous_state))
        if previous_state is not None
        else None,
    }
    return f"tx-{sha256_bytes(canonical_json_bytes(identity))[:20]}"


def approved_update_plan_token(
    root: Path,
    source_root: Path,
    manifest_sha256: str,
    manifest: ReleaseManifest,
    plan: UpdatePlan,
) -> str:
    identity = {
        "target_root": str(root.resolve(strict=True)),
        "source_root": str(source_root.resolve(strict=True)),
        "manifest_sha256": manifest_sha256,
        "bundle_version": manifest.bundle_version,
        "source_artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "source": artifact.source,
                "source_sha256": artifact.sha256,
                "target": artifact.target,
                "policy": artifact.policy,
                "action": artifact.action,
            }
            for artifact in manifest.artifacts
        ],
        "operations": [
            {
                "artifact_id": operation.artifact_id,
                "policy": operation.policy,
                "action": operation.action,
                "target": operation.target,
                "before_sha256": operation.before_sha256,
                "candidate_sha256": operation.after_sha256,
            }
            for operation in plan.operations
        ],
        "candidate_artifacts": list(plan.artifact_state),
        "conflicts": list(plan.conflicts),
    }
    return f"setup-plan-{sha256_bytes(canonical_json_bytes(identity))[:24]}"


def _backup_relative(transaction_id: str, target: str) -> str:
    return f".agents/project_management/setup/.runtime/transactions/{transaction_id}/backup/{target}"


def _candidate_relative(transaction_id: str, target: str) -> str:
    return f".agents/project_management/setup/.runtime/transactions/{transaction_id}/candidate/{target}"


def _journal_relative() -> str:
    return ".agents/project_management/setup/.runtime/journal.json"


def _state_relative() -> str:
    return ".agents/project_management/setup/state.json"


def _receipt_relative(transaction_id: str) -> str:
    return f".agents/project_management/setup/receipts/{transaction_id}.json"


def load_state(root: Path) -> dict[str, Any] | None:
    state_path = assert_no_symlink_components(root, _state_relative(), allow_missing=True)
    if not state_path.exists():
        return None
    state = read_json_object(state_path, label="coordinator state")
    if state.get("state_schema_version") != STATE_SCHEMA_VERSION:
        raise ValidationError("Coordinator state has an unsupported state_schema_version")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValidationError("Coordinator state artifacts must be an array")
    ids: set[str] = set()
    targets: list[str] = []
    for record in artifacts:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValidationError("Coordinator state contains an invalid artifact record")
        if record["id"] in ids:
            raise ValidationError(f"Coordinator state has duplicate artifact id: {record['id']}")
        ids.add(record["id"])
        targets.append(parse_project_relative_path(record.get("target"), label="state target"))
        validate_sha256(record.get("installed_sha256"), label=f"State artifact {record['id']} checksum")
    validate_unique_targets(targets)
    return state


def _copy_candidate_into_runtime(
    root: Path,
    transaction_id: str,
    operation: PlannedOperation,
) -> str | None:
    if operation.action != "write":
        return None
    assert operation.candidate_path is not None
    candidate_relative = _candidate_relative(transaction_id, operation.target)
    atomic_write_under_root(root, candidate_relative, read_regular_file(operation.candidate_path))
    return candidate_relative


def _backup_target(root: Path, transaction_id: str, target: str) -> dict[str, Any]:
    payload = current_file_payload(root, target)
    if payload is None:
        return {"target": target, "existed": False, "sha256": None, "backup": None}
    backup_relative = _backup_relative(transaction_id, target)
    atomic_write_under_root(root, backup_relative, payload)
    return {
        "target": target,
        "existed": True,
        "sha256": sha256_bytes(payload),
        "backup": backup_relative,
    }


def apply_transaction(
    root: Path,
    transaction_id: str,
    operations: tuple[PlannedOperation, ...],
    new_state: dict[str, Any],
    receipt: dict[str, Any],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    if assert_no_symlink_components(root, _journal_relative(), allow_missing=True).exists():
        raise ValidationError("Interrupted transaction journal must be recovered before applying")

    previous_state = load_state(root)
    previous_state_payload = canonical_json_bytes(previous_state) if previous_state is not None else None
    receipt_relative = _receipt_relative(transaction_id)
    receipt_payload = canonical_json_bytes(receipt)
    new_state_payload = canonical_json_bytes(new_state)

    planned_targets = [operation.target for operation in operations]
    validate_unique_targets(planned_targets)
    for operation in operations:
        actual = current_file_payload(root, operation.target)
        actual_checksum = sha256_bytes(actual) if actual is not None else None
        if actual_checksum != operation.before_sha256:
            raise UpdateConflict(f"Target changed after planning: {operation.target}")

    runtime_operations: list[dict[str, Any]] = []
    for operation in operations:
        backup = _backup_target(root, transaction_id, operation.target)
        candidate_relative = _copy_candidate_into_runtime(root, transaction_id, operation)
        runtime_operations.append(
            {
                "artifact_id": operation.artifact_id,
                "policy": operation.policy,
                "action": operation.action,
                "target": operation.target,
                "before_sha256": operation.before_sha256,
                "after_sha256": operation.after_sha256,
                "backup": backup,
                "candidate": candidate_relative,
            }
        )

    receipt_before_payload = current_file_payload(root, receipt_relative)
    receipt_backup = _backup_target(root, transaction_id, receipt_relative)
    receipt_candidate = _candidate_relative(transaction_id, receipt_relative)
    state_candidate = _candidate_relative(transaction_id, _state_relative())
    atomic_write_under_root(root, receipt_candidate, receipt_payload)
    atomic_write_under_root(root, state_candidate, new_state_payload)
    journal = {
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "applied_count": 0,
        "operations": runtime_operations,
        "receipt": {
            "target": receipt_relative,
            "before_sha256": sha256_bytes(receipt_before_payload)
            if receipt_before_payload is not None
            else None,
            "after_sha256": sha256_bytes(receipt_payload),
            "backup": receipt_backup,
            "candidate": receipt_candidate,
        },
        "state": {
            "target": _state_relative(),
            "before_sha256": sha256_bytes(previous_state_payload)
            if previous_state_payload is not None
            else None,
            "after_sha256": sha256_bytes(new_state_payload),
            "candidate": state_candidate,
        },
        "previous_state_base64": base64.b64encode(previous_state_payload).decode("ascii")
        if previous_state_payload is not None
        else None,
    }
    atomic_write_under_root(root, _journal_relative(), canonical_json_bytes(journal))
    if fault_injector:
        fault_injector("after_journal")

    for index, operation in enumerate(runtime_operations, start=1):
        target = operation["target"]
        if operation["action"] == "delete":
            unlink_under_root(root, target)
        else:
            candidate = operation["candidate"]
            assert isinstance(candidate, str)
            candidate_payload = read_regular_file(
                assert_no_symlink_components(root, candidate, allow_missing=False, require_final_file=True)
            )
            atomic_write_under_root(root, target, candidate_payload)
        journal["phase"] = "applying"
        journal["applied_count"] = index
        atomic_write_under_root(root, _journal_relative(), canonical_json_bytes(journal))
        if fault_injector:
            fault_injector(f"after_operation:{index}")

    committed_receipt = current_file_payload(root, receipt_candidate)
    if committed_receipt is None or sha256_bytes(committed_receipt) != sha256_bytes(receipt_payload):
        raise UpdateConflict("Coordinator receipt candidate changed during transaction")
    atomic_write_under_root(root, receipt_relative, committed_receipt)
    if fault_injector:
        fault_injector("before_state")
    committed_state = current_file_payload(root, state_candidate)
    if committed_state is None or sha256_bytes(committed_state) != sha256_bytes(new_state_payload):
        raise UpdateConflict("Coordinator state candidate changed during transaction")
    atomic_write_under_root(root, _state_relative(), committed_state)
    if fault_injector:
        fault_injector("after_state")
    journal["phase"] = "committed"
    atomic_write_under_root(root, _journal_relative(), canonical_json_bytes(journal))
    unlink_under_root(root, _journal_relative())
    _cleanup_committed_runtime_material(root, journal)


def recover_interrupted_transaction(root: Path) -> str | None:
    journal_path = assert_no_symlink_components(root, _journal_relative(), allow_missing=True)
    if not journal_path.exists():
        return None
    journal = read_json_object(journal_path, label="transaction journal")
    expected_journal_fields = {
        "applied_count",
        "journal_schema_version",
        "operations",
        "phase",
        "previous_state_base64",
        "receipt",
        "state",
        "transaction_id",
    }
    if set(journal) != expected_journal_fields:
        raise ValidationError("Interrupted journal fields are invalid")
    if journal.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION:
        raise ValidationError("Interrupted journal has an unsupported schema")
    transaction_id = journal.get("transaction_id")
    if not isinstance(transaction_id, str) or not re.fullmatch(
        r"(?:tx|rollback)-[0-9a-f]{20}", transaction_id
    ):
        raise ValidationError("Interrupted journal is missing transaction_id")

    operations = journal.get("operations")
    if not isinstance(operations, list):
        raise ValidationError("Interrupted journal operations must be an array")
    applied_count = journal.get("applied_count")
    if (
        not isinstance(applied_count, int)
        or isinstance(applied_count, bool)
        or not 0 <= applied_count <= len(operations)
    ):
        raise ValidationError("Interrupted journal applied_count is invalid")
    phase = journal.get("phase")
    if phase not in {"prepared", "applying", "committed"}:
        raise ValidationError("Interrupted journal phase is invalid")
    if phase == "prepared" and applied_count != 0:
        raise ValidationError("Prepared journal cannot contain applied operations")
    if phase == "committed" and applied_count != len(operations):
        raise ValidationError("Committed journal does not include every operation")
    receipt = journal.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("backup"), dict):
        raise ValidationError("Interrupted journal contains an invalid receipt backup")
    state = journal.get("state")
    if not isinstance(state, dict):
        raise ValidationError("Interrupted journal contains invalid state metadata")

    recovery_records = [*operations, receipt, state]
    _assert_recovery_targets_are_known(root, recovery_records)
    _assert_coordinator_recovery_material(root, transaction_id, operations, receipt, state, journal)

    if all(_record_matches_after(root, record) for record in recovery_records):
        unlink_under_root(root, _journal_relative())
        _cleanup_committed_runtime_material(root, journal)
        return f"completed:{transaction_id}"

    for operation in reversed(operations):
        _restore_backup_record(root, operation["backup"])
    _restore_backup_record(root, receipt["backup"])
    _restore_previous_state(root, journal, state)
    cleanup_paths: list[str] = []
    for operation in operations:
        candidate = operation.get("candidate")
        backup = operation.get("backup", {}).get("backup")
        if isinstance(candidate, str):
            cleanup_paths.append(candidate)
        if isinstance(backup, str):
            cleanup_paths.append(backup)
    receipt_backup = receipt.get("backup", {}).get("backup")
    if isinstance(receipt_backup, str):
        cleanup_paths.append(receipt_backup)
    for record in (receipt, state):
        candidate = record.get("candidate")
        if isinstance(candidate, str):
            cleanup_paths.append(candidate)
    for relative_path in sorted(set(cleanup_paths), reverse=True):
        unlink_under_root(root, relative_path)
        remove_empty_runtime_parents(root, relative_path)
    unlink_under_root(root, _journal_relative())
    return f"restored:{transaction_id}"


def _journal_checksum(value: Any, *, label: str, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    return validate_sha256(value, label=label)


def _parse_recovery_record(record: Any, *, label: str) -> tuple[str, str | None, str | None]:
    if not isinstance(record, dict):
        raise ValidationError(f"Interrupted journal {label} is invalid")
    target = parse_project_relative_path(record.get("target"), label=f"{label} target")
    before = _journal_checksum(record.get("before_sha256"), label=f"{label} before checksum")
    after = _journal_checksum(record.get("after_sha256"), label=f"{label} after checksum")
    return target, before, after


def _target_checksum(root: Path, target: str) -> str | None:
    payload = current_file_payload(root, target)
    return sha256_bytes(payload) if payload is not None else None


def _assert_recovery_targets_are_known(root: Path, records: list[Any]) -> None:
    drifted: list[str] = []
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        target, before, after = _parse_recovery_record(record, label=f"record {index}")
        if target in seen:
            raise ValidationError(f"Interrupted journal duplicates target: {target}")
        seen.add(target)
        if _target_checksum(root, target) not in {before, after}:
            drifted.append(target)
    if drifted:
        raise UpdateConflict(
            "Recovery aborted because targets contain post-crash edits; no files were changed. "
            "Preserve the journal and transaction materials, then reconcile: "
            + ", ".join(sorted(drifted))
        )


def _validate_backup_material(root: Path, backup: Any, *, target: str, before: str | None) -> None:
    if (
        not isinstance(backup, dict)
        or set(backup) != {"backup", "existed", "sha256", "target"}
        or backup.get("target") != target
    ):
        raise ValidationError(f"Interrupted journal backup metadata is invalid for {target}")
    if backup.get("existed") is not (before is not None):
        raise ValidationError(f"Interrupted journal backup existence is invalid for {target}")
    if backup.get("sha256") != before:
        raise ValidationError(f"Interrupted journal backup checksum metadata is invalid for {target}")
    backup_relative = backup.get("backup")
    if before is None:
        if backup_relative is not None:
            raise ValidationError(f"Interrupted journal has an unexpected backup for {target}")
        return
    parsed_backup = parse_project_relative_path(backup_relative, label=f"{target} backup path")
    backup_payload = current_file_payload(root, parsed_backup)
    if backup_payload is None or sha256_bytes(backup_payload) != before:
        raise UpdateConflict(
            f"Recovery artifact is missing or corrupt for {target}: {parsed_backup}; "
            "journal and targets were preserved"
        )


def _assert_coordinator_recovery_material(
    root: Path,
    transaction_id: str,
    operations: list[Any],
    receipt: dict[str, Any],
    state: dict[str, Any],
    journal: dict[str, Any],
) -> None:
    for index, operation in enumerate(operations, 1):
        expected_operation_fields = {
            "action",
            "after_sha256",
            "artifact_id",
            "backup",
            "before_sha256",
            "candidate",
            "policy",
            "target",
        }
        if not isinstance(operation, dict) or set(operation) != expected_operation_fields:
            raise ValidationError(f"Interrupted operation {index} fields are invalid")
        target, before, after = _parse_recovery_record(operation, label=f"operation {index}")
        if not isinstance(operation.get("artifact_id"), str) or not operation["artifact_id"]:
            raise ValidationError(f"Interrupted operation {index} artifact id is invalid")
        if not isinstance(operation.get("policy"), str) or not operation["policy"]:
            raise ValidationError(f"Interrupted operation {index} policy is invalid")
        action = operation.get("action")
        if action not in {"write", "delete"}:
            raise ValidationError(f"Interrupted operation has invalid action for {target}")
        expected_backup = _backup_relative(transaction_id, target) if before is not None else None
        if operation.get("backup", {}).get("backup") != expected_backup:
            raise ValidationError(f"Interrupted backup path is invalid for {target}")
        _validate_backup_material(root, operation.get("backup"), target=target, before=before)
        candidate = operation.get("candidate")
        if action == "delete":
            if candidate is not None or after is not None:
                raise ValidationError(f"Interrupted delete metadata is invalid for {target}")
            continue
        expected_candidate = _candidate_relative(transaction_id, target)
        if candidate != expected_candidate or after is None:
            raise ValidationError(f"Interrupted candidate metadata is invalid for {target}")
        candidate_payload = current_file_payload(root, expected_candidate)
        if candidate_payload is None or sha256_bytes(candidate_payload) != after:
            raise UpdateConflict(
                f"Recovery candidate is missing or corrupt for {target}: {expected_candidate}; "
                "journal and targets were preserved"
            )

    receipt_target, receipt_before, receipt_after = _parse_recovery_record(
        receipt, label="receipt"
    )
    if set(receipt) != {
        "after_sha256",
        "backup",
        "before_sha256",
        "candidate",
        "target",
    }:
        raise ValidationError("Interrupted receipt fields are invalid")
    if receipt_target != _receipt_relative(transaction_id) or receipt_after is None:
        raise ValidationError("Interrupted receipt metadata is invalid")
    expected_receipt_candidate = _candidate_relative(transaction_id, receipt_target)
    if receipt.get("candidate") != expected_receipt_candidate:
        raise ValidationError("Interrupted receipt candidate path is invalid")
    receipt_candidate = current_file_payload(root, expected_receipt_candidate)
    if receipt_candidate is None or sha256_bytes(receipt_candidate) != receipt_after:
        raise UpdateConflict(
            "Recovery receipt candidate is missing or corrupt; journal and targets were preserved"
        )
    expected_receipt_backup = (
        _backup_relative(transaction_id, receipt_target) if receipt_before is not None else None
    )
    if receipt.get("backup", {}).get("backup") != expected_receipt_backup:
        raise ValidationError("Interrupted receipt backup path is invalid")
    _validate_backup_material(
        root, receipt.get("backup"), target=receipt_target, before=receipt_before
    )

    state_target, state_before, state_after = _parse_recovery_record(state, label="state")
    if set(state) != {"after_sha256", "before_sha256", "candidate", "target"}:
        raise ValidationError("Interrupted state fields are invalid")
    if state_target != _state_relative() or state_after is None:
        raise ValidationError("Interrupted state metadata is invalid")
    expected_state_candidate = _candidate_relative(transaction_id, state_target)
    if state.get("candidate") != expected_state_candidate:
        raise ValidationError("Interrupted state candidate path is invalid")
    state_candidate = current_file_payload(root, expected_state_candidate)
    if state_candidate is None or sha256_bytes(state_candidate) != state_after:
        raise UpdateConflict(
            "Recovery state candidate is missing or corrupt; journal and targets were preserved"
        )
    previous = journal.get("previous_state_base64")
    if previous is None:
        if state_before is not None:
            raise ValidationError("Interrupted previous state is missing")
        return
    if not isinstance(previous, str):
        raise ValidationError("Interrupted previous state is invalid")
    try:
        previous_payload = base64.b64decode(previous, validate=True)
    except ValueError as exc:
        raise ValidationError("Interrupted previous state is corrupt") from exc
    if sha256_bytes(previous_payload) != state_before:
        raise UpdateConflict(
            "Recovery previous-state artifact is corrupt; journal and targets were preserved"
        )


def _record_matches_after(root: Path, record: Any) -> bool:
    target, _, after = _parse_recovery_record(record, label="recovery record")
    return _target_checksum(root, target) == after


def _restore_previous_state(root: Path, journal: dict[str, Any], state: dict[str, Any]) -> None:
    _, before, _ = _parse_recovery_record(state, label="state")
    previous = journal.get("previous_state_base64")
    if before is None:
        unlink_under_root(root, _state_relative())
        return
    assert isinstance(previous, str)
    atomic_write_under_root(root, _state_relative(), base64.b64decode(previous, validate=True))


def _cleanup_committed_runtime_material(root: Path, journal: dict[str, Any]) -> None:
    removable: list[str] = []
    for operation in journal.get("operations", []):
        candidate = operation.get("candidate") if isinstance(operation, dict) else None
        if isinstance(candidate, str):
            removable.append(candidate)
    for key in ("receipt", "state"):
        record = journal.get(key)
        if not isinstance(record, dict):
            continue
        candidate = record.get("candidate")
        if isinstance(candidate, str):
            removable.append(candidate)
        if key == "receipt":
            backup = record.get("backup")
            backup_path = backup.get("backup") if isinstance(backup, dict) else None
            if isinstance(backup_path, str):
                removable.append(backup_path)
    for relative in sorted(set(removable), reverse=True):
        unlink_under_root(root, relative)
        remove_empty_runtime_parents(root, relative)


def _restore_backup_record(root: Path, backup: dict[str, Any]) -> None:
    target = parse_project_relative_path(backup.get("target"), label="backup target")
    existed = backup.get("existed")
    if existed is False:
        unlink_under_root(root, target)
        return
    if existed is not True or not isinstance(backup.get("backup"), str):
        raise ValidationError(f"Invalid backup record for {target}")
    backup_path = assert_no_symlink_components(
        root,
        backup["backup"],
        allow_missing=False,
        require_final_file=True,
    )
    payload = read_regular_file(backup_path)
    expected = validate_sha256(backup.get("sha256"), label=f"Backup {target} checksum")
    if sha256_bytes(payload) != expected:
        raise ValidationError(f"Backup checksum mismatch for {target}")
    atomic_write_under_root(root, target, payload)


def plan_to_json(plan: UpdatePlan) -> dict[str, Any]:
    return {
        "conflicts": list(plan.conflicts),
        "operations": [
            {
                "action": operation.action,
                "artifact_id": operation.artifact_id,
                "before_sha256": operation.before_sha256,
                "after_sha256": operation.after_sha256,
                "policy": operation.policy,
                "target": operation.target,
            }
            for operation in plan.operations
        ],
    }


def write_conflict_report(
    root: Path,
    transaction_id: str,
    plan: UpdatePlan,
    candidates: dict[str, Path],
) -> str:
    conflict_root = f".agents/project_management/setup/.runtime/conflicts/{transaction_id}"
    for conflict in plan.conflicts:
        candidate = candidates.get(conflict["artifact_id"])
        if candidate is None:
            continue
        candidate_relative = f"{conflict_root}/candidate/{conflict['target']}"
        atomic_write_under_root(root, candidate_relative, read_regular_file(candidate))
    report_relative = f"{conflict_root}/report.json"
    atomic_write_under_root(root, report_relative, canonical_json_bytes(plan_to_json(plan)))
    return report_relative

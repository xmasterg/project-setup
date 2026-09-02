#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

from install_transaction import (
    InstallConflict,
    InstallLock,
    InstallOperation,
    InstallSafetyError,
    apply_install_transaction,
    atomic_write,
    canonical_json,
    checksum,
    load_install_state,
    parse_relative,
    read_file,
    read_target,
    recover_install_transaction,
    safe_path,
    transaction_id,
)
from task_store import (
    ACTIVE_FILE_NAMES,
    envelope_with_tasks,
    layout_from_root,
    load_sources,
    read_json,
    tasks_from_source,
    write_json_atomic,
)


BLOCK_START = "<!-- task-tracker:start -->"
BLOCK_END = "<!-- task-tracker:end -->"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
ARTIFACT_POLICIES = {"generated", "managed", "managed_block", "seed", "user_data"}
SUPPORTED_DYNAMIC_RULES = {
    (
        ".agents/project_management/tasks/task_tracking/completed/YYYY/MM/week-NN/tasks.json",
        "user_data",
    ),
    (
        ".agents/project_management/tasks/ideation/<project-relative-file>",
        "user_data",
    ),
}
EXPECTED_MIGRATIONS = (
    {"id": "tracker-v1-to-v4", "rollback": "restore_backup"},
    {"id": "tracker-v2-to-v4", "rollback": "restore_backup"},
    {"id": "tracker-v3-to-v4", "rollback": "restore_backup"},
    {"id": "task-board-split-v1", "rollback": "restore_backup"},
    {"id": "approved-plan-binding-v1", "rollback": "restore_backup"},
    {"id": "archive-transaction-v1", "rollback": "restore_backup"},
    {"id": "legacy-metadata-namespace-v1", "rollback": "restore_backup"},
)
EXPECTED_RETIREMENT_POLICY = (
    "Delete only exact positively-owned legacy files validated and checksummed during the "
    "transaction; preserve unknown files and directories."
)
EXPECTED_ROLLBACK = {
    "declaration": "Restore every touched file from the fsynced transaction backup.",
    "mode": "transaction",
}
REQUIRED_ARTIFACT_SEMANTICS = {
    artifact_id: {
        "source": source,
        "target": target,
        "policy": policy,
        "action": action,
        "checksum": {"algorithm": "sha256", "mode": checksum_mode},
        **metadata,
    }
    for artifact_id, source, target, policy, action, checksum_mode, metadata in (
        (
            "task-data-backlog",
            "assets/project/tasks/task_tracking/backlog.json",
            ".agents/project_management/tasks/task_tracking/backlog.json",
            "user_data",
            "install",
            "candidate",
            {},
        ),
        (
            "task-data-blocked",
            "assets/project/tasks/task_tracking/blocked.json",
            ".agents/project_management/tasks/task_tracking/blocked.json",
            "user_data",
            "install",
            "candidate",
            {},
        ),
        (
            "task-data-in-progress",
            "assets/project/tasks/task_tracking/in-progress.json",
            ".agents/project_management/tasks/task_tracking/in-progress.json",
            "user_data",
            "install",
            "candidate",
            {},
        ),
        (
            "task-data-ready",
            "assets/project/tasks/task_tracking/ready.json",
            ".agents/project_management/tasks/task_tracking/ready.json",
            "user_data",
            "install",
            "candidate",
            {},
        ),
        (
            "tracker-metadata",
            "assets/project/tasks/setup/tracker.json",
            ".agents/project_management/tasks/setup/tracker.json",
            "user_data",
            "install",
            "candidate",
            {},
        ),
        (
            "ideation-readme",
            "assets/project/tasks/ideation/README.md",
            ".agents/project_management/tasks/ideation/README.md",
            "seed",
            "install",
            "candidate",
            {},
        ),
        (
            "ideation-template",
            "assets/project/tasks/ideation/feature-plan.template.md",
            ".agents/project_management/tasks/ideation/feature-plan.template.md",
            "seed",
            "install",
            "candidate",
            {},
        ),
        (
            "runtime-task-store",
            "scripts/task_store.py",
            ".agents/project_management/tasks/setup/scripts/task_store.py",
            "managed",
            "install",
            "candidate",
            {
                "accepted_legacy_sha256": [
                    "105d39e912f734900e5002bb7aedebc0e6998ae05765e6e6a49915a7ed1eccb5"
                ]
            },
        ),
        (
            "runtime-archive",
            "scripts/archive_tasks.py",
            ".agents/project_management/tasks/setup/scripts/archive_tasks.py",
            "managed",
            "install",
            "candidate",
            {
                "accepted_legacy_sha256": [
                    "c4abae8a2564cea5055e1ea095b5002d304ba6d2f8ba8af72ee518f41f7b1e20"
                ]
            },
        ),
        (
            "runtime-render",
            "scripts/render_tasks.py",
            ".agents/project_management/tasks/setup/scripts/render_tasks.py",
            "managed",
            "install",
            "candidate",
            {
                "accepted_legacy_sha256": [
                    "d88a39edb4cf58c01860b3bc4c9276d54e6e517cffbd29f55eb8d259be099022"
                ]
            },
        ),
        (
            "runtime-install-transaction",
            "scripts/install_transaction.py",
            ".agents/project_management/tasks/setup/scripts/install_transaction.py",
            "managed",
            "install",
            "candidate",
            {"accepted_legacy_sha256": []},
        ),
        (
            "board-shell",
            "assets/project/tasks/task_tracking/open_task_board.html",
            ".agents/project_management/tasks/task_tracking/open_task_board.html",
            "managed",
            "install",
            "candidate",
            {"replaces_artifact_id": "generated-board"},
        ),
        (
            "board-styles",
            "assets/project/tasks/setup/task_board/task_board.css",
            ".agents/project_management/tasks/setup/task_board/task_board.css",
            "managed",
            "install",
            "candidate",
            {},
        ),
        (
            "board-application",
            "assets/project/tasks/setup/task_board/task_board.js",
            ".agents/project_management/tasks/setup/task_board/task_board.js",
            "managed",
            "install",
            "candidate",
            {},
        ),
        (
            "board-data",
            None,
            ".agents/project_management/tasks/task_tracking/task_board.data.js",
            "generated",
            "install",
            "candidate",
            {"board_data_version": 1, "generator": "runtime-render"},
        ),
        (
            "board-template",
            None,
            ".agents/project_management/tasks/setup/task_board/task_board.template.html",
            "managed",
            "retire",
            "recorded_or_accepted_legacy",
            {
                "accepted_legacy_sha256": [
                    "dac5044ce7023cf7766206a5d5c59d1262fb6aedb77974146fcfa758ac582d75"
                ]
            },
        ),
        (
            "instruction-block",
            "assets/AGENTS.block.md",
            "{instruction_file}",
            "managed_block",
            "install",
            "managed_block",
            {
                "block": {"start_marker": BLOCK_START, "end_marker": BLOCK_END},
                "destination_sources": {
                    "AGENTS.md": "assets/AGENTS.block.md",
                    "CLAUDE.md": "assets/CLAUDE.block.md",
                },
            },
        ),
    )
}
SEMANTIC_ARTIFACT_FIELDS = {
    "accepted_legacy_sha256",
    "action",
    "block",
    "board_data_version",
    "checksum",
    "destination_sources",
    "generator",
    "policy",
    "replaces_artifact_id",
    "source",
    "target",
}


@dataclass(frozen=True)
class MigrationBatch:
    target: Path
    tasks: tuple[dict[str, Any], ...]
    envelope_fields: dict[str, Any]


@dataclass(frozen=True)
class InstallPlan:
    operations: tuple[InstallOperation, ...]
    conflicts: tuple[dict[str, str], ...]
    state: dict[str, Any]
    migrated_tasks: int
    candidate_by_artifact: dict[str, Path | None]
    plan_token: str


def comparable_tracker_states(
    current: dict[str, Any] | None, planned: dict[str, Any]
) -> bool:
    if current is None:
        return False
    comparable_current = dict(current)
    comparable_planned = dict(planned)
    comparable_current.pop("approved_plan_token", None)
    comparable_planned.pop("approved_plan_token", None)
    return comparable_current == comparable_planned


def tracker_state_content(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    content = dict(state)
    content.pop("approved_plan_token", None)
    content.pop("last_successful_transaction", None)
    return content


def tracker_receipt_findings(root: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    transaction = state.get("last_successful_transaction")
    if not isinstance(transaction, str) or not transaction:
        return [{"code": "tracker_receipt_missing"}]
    receipt_path = f".agents/project_management/setup/receipts/{transaction}.json"
    payload = read_target(root, receipt_path)
    if payload is None:
        return [{"code": "tracker_receipt_missing", "path": receipt_path}]
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [{"code": "tracker_receipt_invalid", "path": receipt_path}]
    if not isinstance(receipt, dict):
        return [{"code": "tracker_receipt_invalid", "path": receipt_path}]
    operations_raw = receipt.get("operations")
    if not isinstance(operations_raw, list):
        return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
    operations: list[InstallOperation] = []
    expected_operation_fields = {
        "artifact_id",
        "policy",
        "action",
        "target",
        "before_sha256",
        "after_sha256",
    }
    for raw in operations_raw:
        if not isinstance(raw, dict) or set(raw) != expected_operation_fields:
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
        if raw.get("action") not in {"write", "delete"}:
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
        try:
            target = parse_relative(raw.get("target"), label="Tracker receipt operation target")
        except InstallSafetyError:
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
        before = raw.get("before_sha256")
        after = raw.get("after_sha256")
        if before is not None and not isinstance(before, str):
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
        if after is not None and not isinstance(after, str):
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
        operations.append(
            InstallOperation(
                artifact_id=raw.get("artifact_id"),
                policy=raw.get("policy"),
                action=raw["action"],
                target=target,
                candidate=None,
                before_sha256=before,
                after_sha256=after,
            )
        )
    expected = {
        "receipt_schema_version": 1,
        "transaction_id": transaction,
        "component": state.get("component"),
        "component_version": state.get("component_version"),
        "bundle_version": state.get("bundle_version"),
        "manifest_schema_version": state.get("manifest_schema_version"),
        "tracker_data_schema_version": state.get("tracker_data_schema_version"),
        "board_data_version": state.get("board_data_version"),
        "artifact_policies": state.get("artifact_policies"),
        "source_identity": state.get("source_identity"),
        "artifacts": state.get("artifacts"),
        "migrations": state.get("migrations"),
        "approved_plan_token": state.get("approved_plan_token"),
        "operations": operations_raw,
    }
    if receipt != expected:
        return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
    if transaction_id(operations, state.get("component_version"), state) != transaction:
        return [{"code": "tracker_receipt_state_mismatch", "path": receipt_path}]
    return []


def has_consistent_tracker_receipt(root: Path, state: dict[str, Any]) -> bool:
    return not tracker_receipt_findings(root, state)


def tracker_source_identity(
    skill: Path,
    manifest: dict[str, Any],
    manifest_checksum: str,
    archive_checksum: str | None,
) -> dict[str, Any]:
    component_version = manifest["component_version"]
    release_status = manifest["release_status"]
    published_tag = manifest["published_tag"]
    immutable_commit = manifest["immutable_commit"]
    source_checkout_commit = manifest["source_checkout_commit"]
    if archive_checksum is None:
        return {
            "kind": "verified_local_skill",
            "local_path": str(skill),
            "manifest_sha256": manifest_checksum,
            "component": "task-tracking-setup",
            "component_version": component_version,
            "immutable_commit": immutable_commit,
            "published_tag": published_tag,
            "release_status": release_status,
            "source_checkout_commit": source_checkout_commit,
        }
    if not SHA256_PATTERN.fullmatch(archive_checksum):
        raise InstallSafetyError("Tracker package archive checksum is invalid")
    return {
        "kind": "checksum_verified_vendored_archive",
        "archive_sha256": archive_checksum,
        "manifest_sha256": manifest_checksum,
        "component": "task-tracking-setup",
        "component_version": component_version,
        "immutable_commit": immutable_commit,
        "published_tag": published_tag,
        "release_status": release_status,
        "source_checkout_commit": source_checkout_commit,
    }


def normalize_task(raw_task: dict[str, Any], source: Path, *, rebase_v3_paths: bool = False) -> dict[str, Any]:
    if not isinstance(raw_task, dict):
        raise InstallSafetyError(f"Every task in {source} must be an object")
    task = dict(raw_task)
    task.setdefault("urgency", "normal")
    task.setdefault("planning_docs", [])
    if rebase_v3_paths:
        task["planning_docs"] = [
            f".agents/project_management/tasks/{path[len('tasks/') :]}"
            if isinstance(path, str) and path.startswith("tasks/ideation/")
            else path
            for path in task["planning_docs"]
        ]
    return task


def envelope_fields(envelope: dict[str, Any], known_keys: set[str]) -> dict[str, Any]:
    return {key: value for key, value in envelope.items() if key not in known_keys}


def safe_target_json(project_root: Path, relative: str) -> tuple[Path, dict[str, Any]]:
    path = safe_path(project_root, relative, allow_missing=False)
    try:
        value = json.loads(read_file(path))
    except json.JSONDecodeError as exc:
        raise InstallSafetyError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallSafetyError(f"{path} must contain a JSON object")
    return path, value


def collect_v1(
    project_root: Path, candidate_root: Path
) -> tuple[list[MigrationBatch], str | None, list[str], dict[str, Any]]:
    relative = ".agents/tasks.json"
    path = safe_path(project_root, relative)
    if not path.exists():
        return [], None, [], {}
    source, data = safe_target_json(project_root, relative)
    if data.get("schema_version") != 1:
        raise InstallSafetyError(f"Refusing unrecognized v1 tracker: {source}")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise InstallSafetyError(f"{source} must contain a tasks array")
    layout = layout_from_root(candidate_root)
    grouped: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for raw_task in tasks:
        task = normalize_task(raw_task, source)
        status = task.get("status")
        target_status = "in_progress" if status in {"in_progress", "done"} else status
        if target_status not in layout.active_paths:
            raise InstallSafetyError(f"Legacy task {task.get('id')} has unsupported status: {status}")
        grouped[layout.active_paths[target_status]].append(task)
    extras = envelope_fields(data, {"schema_version", "project", "updated_at", "tasks"})
    batches = [MigrationBatch(target, tuple(records), extras) for target, records in grouped.items()]
    metadata = {"legacy_v1_empty_envelope": extras} if not tasks and extras else {}
    return batches, data.get("project"), [relative], metadata


def collect_v2(
    project_root: Path, candidate_root: Path
) -> tuple[list[MigrationBatch], str | None, list[str], dict[str, Any]]:
    index_relative = ".agents/tasks/index.json"
    index_path = safe_path(project_root, index_relative)
    if not index_path.exists():
        return [], None, [], {}
    _, index = safe_target_json(project_root, index_relative)
    if index.get("schema_version") != 2:
        raise InstallSafetyError("Refusing unrecognized v2 tracker")
    files = index.get("files")
    if not isinstance(files, dict):
        raise InstallSafetyError("V2 tracker index must contain a files object")
    layout = layout_from_root(candidate_root)
    batches: list[MigrationBatch] = []
    retirements = [index_relative]
    for old_key in ("backlog", "ready", "in_progress", "blocked", "completed"):
        file_relative = files.get(old_key)
        if not isinstance(file_relative, str):
            raise InstallSafetyError(f"V2 index is missing file mapping: {old_key}")
        file_relative = parse_relative(file_relative, label=f"V2 {old_key} path")
        source_relative = f".agents/tasks/{file_relative}"
        source, envelope = safe_target_json(project_root, source_relative)
        target_status = "in_progress" if old_key in {"in_progress", "completed"} else old_key
        records = tuple(normalize_task(task, source) for task in tasks_from_source(source, envelope))
        batches.append(
            MigrationBatch(
                layout.active_paths[target_status],
                records,
                envelope_fields(envelope, {"tasks"}),
            )
        )
        retirements.append(source_relative)
    archive_relative = parse_relative(
        index.get("completed_archive", "completed"), label="V2 archive path"
    )
    old_archive = safe_path(project_root, f".agents/tasks/{archive_relative}")
    if old_archive.exists():
        for source in sorted(old_archive.glob("*/*/week-*/tasks.json")):
            relative_to_root = source.relative_to(project_root).as_posix()
            safe_path(project_root, relative_to_root, allow_missing=False)
            envelope = read_json(source)
            target = layout.archive_root / source.relative_to(old_archive)
            batches.append(
                MigrationBatch(
                    target,
                    tuple(normalize_task(task, source) for task in tasks_from_source(source, envelope)),
                    envelope_fields(envelope, {"tasks"}),
                )
            )
            retirements.append(relative_to_root)
    metadata = envelope_fields(
        index,
        {"schema_version", "project", "files", "completed_archive"},
    )
    return batches, index.get("project"), retirements, {"legacy_v2_index": metadata} if metadata else {}


def collect_v3(
    project_root: Path, candidate_root: Path
) -> tuple[list[MigrationBatch], str | None, list[str], dict[str, Any]]:
    tracker_relative = "tasks/setup/tracker.json"
    tracker_path = safe_path(project_root, tracker_relative)
    if not tracker_path.exists():
        return [], None, [], {}
    _, tracker = safe_target_json(project_root, tracker_relative)
    if tracker.get("schema_version") != 3:
        raise InstallSafetyError("Refusing unrecognized v3 tracker")
    layout = layout_from_root(candidate_root)
    batches: list[MigrationBatch] = []
    retirements = [tracker_relative]
    for status, filename in ACTIVE_FILE_NAMES.items():
        relative = f"tasks/task_tracking/{filename}"
        source, envelope = safe_target_json(project_root, relative)
        batches.append(
            MigrationBatch(
                layout.active_paths[status],
                tuple(
                    normalize_task(task, source, rebase_v3_paths=True)
                    for task in tasks_from_source(source, envelope)
                ),
                envelope_fields(envelope, {"tasks"}),
            )
        )
        retirements.append(relative)
    old_archive = safe_path(project_root, "tasks/task_tracking/completed")
    if old_archive.exists():
        for source in sorted(old_archive.glob("*/*/week-*/tasks.json")):
            relative_to_root = source.relative_to(project_root).as_posix()
            safe_path(project_root, relative_to_root, allow_missing=False)
            envelope = read_json(source)
            batches.append(
                MigrationBatch(
                    layout.archive_root / source.relative_to(old_archive),
                    tuple(
                        normalize_task(task, source, rebase_v3_paths=True)
                        for task in tasks_from_source(source, envelope)
                    ),
                    envelope_fields(envelope, {"tasks"}),
                )
            )
            retirements.append(relative_to_root)
    metadata = envelope_fields(tracker, {"schema_version", "project", "updated_at"})
    return batches, tracker.get("project"), retirements, {"legacy_v3_tracker": metadata} if metadata else {}


def copy_user_tree(source_root: Path, source_relative: str, target_root: Path, target_relative: str) -> None:
    source = safe_path(source_root, source_relative)
    if not source.exists():
        return
    for path in sorted(item for item in source.rglob("*") if item.is_file() or item.is_symlink()):
        relative = path.relative_to(source_root).as_posix()
        safe_path(source_root, relative, allow_missing=False)
        destination = target_root / target_relative / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = read_file(path)
        if destination.exists() and destination.read_bytes() != payload:
            raise InstallConflict(f"User data conflicts with migration destination: {relative}")
        destination.write_bytes(payload)


def copy_existing_v4_data(project_root: Path, candidate_root: Path) -> None:
    source_tasks = safe_path(project_root, ".agents/project_management/tasks")
    if not source_tasks.exists():
        return
    relative_files = [
        f".agents/project_management/tasks/task_tracking/{filename}"
        for filename in ACTIVE_FILE_NAMES.values()
    ]
    relative_files.append(".agents/project_management/tasks/setup/tracker.json")
    for relative in relative_files:
        source = safe_path(project_root, relative)
        if not source.exists():
            continue
        destination = candidate_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(read_file(source))
    archive_root = safe_path(
        project_root, ".agents/project_management/tasks/task_tracking/completed"
    )
    if archive_root.exists():
        for source in sorted(archive_root.glob("*/*/week-*/tasks.json")):
            relative = source.relative_to(project_root).as_posix()
            safe_path(project_root, relative, allow_missing=False)
            destination = candidate_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(read_file(source))
    copy_user_tree(
        project_root,
        ".agents/project_management/tasks/ideation",
        candidate_root,
        ".agents/project_management/tasks/ideation",
    )


def seed_candidate_files(
    skill: Path,
    candidate_root: Path,
    project_name: str,
    manifest: dict[str, Any],
) -> None:
    layout = layout_from_root(candidate_root)
    for artifact in manifest["artifacts"]:
        source_relative = artifact["source"]
        target_relative = artifact["target"]
        if source_relative is None or target_relative == "{instruction_file}":
            continue
        if artifact.get("action") == "retire" or artifact["policy"] == "generated":
            continue
        source = safe_path(skill, source_relative, allow_missing=False)
        target = candidate_root / target_relative
        if target.exists() and artifact["policy"] in {"seed", "user_data"}:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_file(source))
    tracker = read_json(layout.tracker_path)
    if tracker.get("project") == "Project tasks":
        tracker["project"] = project_name
        write_json_atomic(layout.tracker_path, tracker)


def merge_migrations(candidate_root: Path, batches: list[MigrationBatch]) -> int:
    layout = layout_from_root(candidate_root)
    _, sources = load_sources(layout)
    current: dict[str, dict[str, Any]] = {}
    envelopes = {path.resolve(): envelope for path, envelope in sources}
    for path, envelope in sources:
        for task in tasks_from_source(path, envelope):
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise InstallSafetyError(f"Task in {path} has invalid id")
            if task_id in current:
                raise InstallSafetyError(f"Duplicate task ID before migration: {task_id}")
            current[task_id] = task
    additions: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    extras: dict[Path, dict[str, Any]] = defaultdict(dict)
    migrated = 0
    for batch in batches:
        target = batch.target.resolve()
        for key, value in batch.envelope_fields.items():
            existing_value = extras[target].get(key, value)
            if existing_value != value:
                raise InstallConflict(f"Legacy envelope field conflicts at {batch.target}: {key}")
            current_envelope = envelopes.get(target, {})
            if key in current_envelope and current_envelope[key] != value:
                raise InstallConflict(f"Legacy envelope field conflicts with target at {batch.target}: {key}")
            extras[target][key] = value
        for task in batch.tasks:
            task_id = task.get("id")
            existing = current.get(task_id)
            if existing is not None:
                if existing != task:
                    raise InstallConflict(f"Legacy task {task_id} conflicts with target task data")
                continue
            additions[target].append(task)
            current[task_id] = task
            migrated += 1
    for target in sorted(set(additions) | set(extras), key=str):
        envelope = dict(envelopes.get(target, read_json(target) if target.exists() else {"tasks": []}))
        envelope.update(extras[target])
        records = tasks_from_source(target, envelope)
        write_json_atomic(target, envelope_with_tasks(envelope, records + additions[target]))
    return migrated


def instruction_candidate(current: bytes | None, block_payload: bytes, *, has_state: bool) -> tuple[bytes, str]:
    if current is None:
        return block_payload.rstrip() + b"\n", checksum(block_payload.strip())
    try:
        text = current.decode("utf-8")
        block = block_payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InstallConflict("Instruction file and tracker block must be UTF-8") from exc
    starts = text.count(BLOCK_START)
    ends = text.count(BLOCK_END)
    if starts != ends or starts > 1:
        raise InstallConflict("Malformed or duplicate task-tracker managed markers")
    if starts == 0:
        if has_state:
            raise InstallConflict("Managed task-tracker block is missing")
        separator = "\n\n" if text.strip() else ""
        return (text.rstrip() + separator + block + "\n").encode(), checksum(block.encode())
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END)
    if end < start:
        raise InstallConflict("Task-tracker managed markers are misordered")
    end += len(BLOCK_END)
    current_block = text[start:end].encode()
    prefix = text[:start].rstrip()
    separator = "\n\n" if prefix else ""
    updated = (prefix + separator + block + text[end:]).rstrip() + "\n"
    return updated.encode(), checksum(current_block)


def instruction_block_payload(skill: Path, destination: str) -> bytes:
    asset = "CLAUDE.block.md" if destination == "CLAUDE.md" else "AGENTS.block.md"
    return read_file(safe_path(skill, f"assets/{asset}", allow_missing=False))


def load_install_manifest(skill: Path) -> dict[str, Any]:
    path = safe_path(skill, "assets/install-manifest.json", allow_missing=False)
    try:
        value = json.loads(read_file(path))
    except json.JSONDecodeError as exc:
        raise InstallSafetyError(f"Tracker install manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallSafetyError("Tracker install manifest must contain an object")
    required_keys = {
        "artifact_policies",
        "artifacts",
        "board_data_version",
        "bundle_version",
        "component",
        "component_version",
        "dynamic_artifact_rules",
        "immutable_commit",
        "manifest_schema_version",
        "migrations",
        "published_tag",
        "release_status",
        "retirement_policy",
        "rollback",
        "source_checkout_commit",
        "tracker_data_schema_version",
    }
    if set(value) != required_keys or value.get("manifest_schema_version") != 1:
        raise InstallSafetyError("Tracker install manifest schema is invalid")
    if value.get("component") != "task-tracking-setup":
        raise InstallSafetyError("Tracker install manifest component is invalid")
    component_version = value.get("component_version")
    if not isinstance(component_version, str) or not VERSION_PATTERN.fullmatch(component_version):
        raise InstallSafetyError("Tracker install manifest component_version is invalid")
    if value.get("tracker_data_schema_version") != 4:
        raise InstallSafetyError("Tracker install manifest data schema is invalid")
    if value.get("board_data_version") != 1:
        raise InstallSafetyError("Tracker install manifest board data schema is invalid")
    if value.get("bundle_version") != component_version:
        raise InstallSafetyError("Tracker install manifest bundle_version must match component_version")
    validate_tracker_release_identity(value)
    policies = value.get("artifact_policies")
    if not isinstance(policies, dict) or set(policies) != ARTIFACT_POLICIES:
        raise InstallSafetyError("Tracker install manifest artifact policies are invalid")
    if any(not isinstance(description, str) or not description for description in policies.values()):
        raise InstallSafetyError("Every tracker artifact policy needs a description")
    retirement_policy = value.get("retirement_policy")
    if retirement_policy != EXPECTED_RETIREMENT_POLICY:
        raise InstallSafetyError("Tracker retirement_policy does not match runtime semantics")
    rollback = value.get("rollback")
    if rollback != EXPECTED_ROLLBACK:
        raise InstallSafetyError("Tracker rollback declaration does not match runtime semantics")
    migrations = value.get("migrations")
    if migrations != list(EXPECTED_MIGRATIONS):
        raise InstallSafetyError(
            "Tracker migrations must exactly match the runtime migration ids, order, and rollback semantics"
        )
    dynamic_rules = value.get("dynamic_artifact_rules")
    if not isinstance(dynamic_rules, list) or not dynamic_rules:
        raise InstallSafetyError("Tracker dynamic_artifact_rules must be a non-empty array")
    parsed_dynamic_rules: set[tuple[str, str]] = set()
    for rule in dynamic_rules:
        if not isinstance(rule, dict) or set(rule) != {"path_pattern", "policy", "purpose"}:
            raise InstallSafetyError("Tracker dynamic artifact rule is invalid")
        if rule.get("policy") != "user_data":
            raise InstallSafetyError("Tracker dynamic artifacts must use user_data policy")
        if any(not isinstance(rule.get(key), str) or not rule[key] for key in ("path_pattern", "purpose")):
            raise InstallSafetyError("Tracker dynamic artifact rule fields are invalid")
        parsed_dynamic_rules.add((rule["path_pattern"], rule["policy"]))
    if parsed_dynamic_rules != SUPPORTED_DYNAMIC_RULES or len(parsed_dynamic_rules) != len(dynamic_rules):
        raise InstallSafetyError("Tracker dynamic artifact rules are missing, duplicated, or unsupported")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise InstallSafetyError("Tracker artifacts must be a non-empty array")
    artifact_ids: set[str] = set()
    targets: set[str] = set()
    normalized_targets: list[str] = []
    common_keys = {"checksum", "id", "policy", "source", "target"}
    optional_keys = {
        "accepted_legacy_sha256",
        "action",
        "block",
        "board_data_version",
        "destination_source_sha256",
        "destination_sources",
        "generator",
        "replaces_artifact_id",
        "source_sha256",
    }
    for position, artifact in enumerate(artifacts, start=1):
        if not isinstance(artifact, dict) or not common_keys <= set(artifact):
            raise InstallSafetyError(f"Tracker artifact {position} fields are invalid")
        unknown_keys = set(artifact) - common_keys - optional_keys
        if unknown_keys:
            raise InstallSafetyError(
                f"Tracker artifact {position} has unknown fields: {', '.join(sorted(unknown_keys))}"
            )
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or not artifact_id or artifact_id in artifact_ids:
            raise InstallSafetyError(f"Tracker artifact {position} id is invalid or duplicated")
        artifact_ids.add(artifact_id)
        expected_semantics = REQUIRED_ARTIFACT_SEMANTICS.get(artifact_id)
        if expected_semantics is None:
            raise InstallSafetyError(f"Tracker artifact id is not supported: {artifact_id}")
        actual_semantics = {
            key: artifact.get(key, "install" if key == "action" else None)
            for key in SEMANTIC_ARTIFACT_FIELDS
            if key in artifact or key in expected_semantics
        }
        if actual_semantics != expected_semantics:
            raise InstallSafetyError(
                f"Tracker artifact {artifact_id} semantic mapping is invalid"
            )
        policy = artifact.get("policy")
        if policy not in ARTIFACT_POLICIES:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} policy is invalid")
        target = artifact.get("target")
        if target != "{instruction_file}":
            target = parse_relative(target, label=f"Artifact {artifact_id} target")
            if target.casefold() in targets:
                raise InstallSafetyError(f"Tracker artifact target is duplicated: {target}")
            targets.add(target.casefold())
            normalized_targets.append(target)
        checksum_declaration = artifact.get("checksum")
        if not isinstance(checksum_declaration, dict) or set(checksum_declaration) != {
            "algorithm",
            "mode",
        }:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} checksum is invalid")
        if checksum_declaration.get("algorithm") != "sha256":
            raise InstallSafetyError(f"Tracker artifact {artifact_id} checksum algorithm is invalid")
        action = artifact.get("action", "install")
        if action not in {"install", "retire"}:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} action is invalid")
        if action == "install" and "action" in artifact:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} has redundant install action")
        expected_mode = (
            "recorded_or_accepted_legacy"
            if action == "retire"
            else "managed_block"
            if policy == "managed_block"
            else "candidate"
        )
        if checksum_declaration.get("mode") != expected_mode:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} checksum mode is invalid")
        source_relative = artifact.get("source")
        if action == "retire" or policy == "generated":
            if source_relative is not None or artifact.get("source_sha256") is not None:
                raise InstallSafetyError(f"Tracker artifact {artifact_id} must not have a source")
        elif policy == "managed_block":
            destination_sources = artifact.get("destination_sources")
            destination_hashes = artifact.get("destination_source_sha256")
            if (
                target != "{instruction_file}"
                or not isinstance(destination_sources, dict)
                or set(destination_sources) != {"AGENTS.md", "CLAUDE.md"}
                or not isinstance(destination_hashes, dict)
                or set(destination_hashes) != set(destination_sources)
            ):
                raise InstallSafetyError("Tracker instruction artifact destination mappings are invalid")
            for destination, mapped_source in destination_sources.items():
                mapped_path = parse_relative(
                    mapped_source, label=f"Tracker instruction source for {destination}"
                )
                mapped_payload = read_file(safe_path(skill, mapped_path, allow_missing=False))
                expected_hash = destination_hashes.get(destination)
                if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(expected_hash):
                    raise InstallSafetyError(f"Tracker instruction checksum is invalid: {destination}")
                if checksum(mapped_payload) != expected_hash:
                    raise InstallSafetyError(f"Tracker instruction source checksum drifted: {destination}")
            if source_relative != destination_sources["AGENTS.md"]:
                raise InstallSafetyError("Tracker instruction primary source mapping is invalid")
            block = artifact.get("block")
            if block != {"start_marker": BLOCK_START, "end_marker": BLOCK_END}:
                raise InstallSafetyError("Tracker instruction artifact markers are invalid")
        else:
            source_relative = parse_relative(
                source_relative, label=f"Tracker artifact {artifact_id} source"
            )
            source_payload = read_file(safe_path(skill, source_relative, allow_missing=False))
            source_sha256 = artifact.get("source_sha256")
            if not isinstance(source_sha256, str) or not SHA256_PATTERN.fullmatch(source_sha256):
                raise InstallSafetyError(f"Tracker artifact {artifact_id} source checksum is invalid")
            if checksum(source_payload) != source_sha256:
                raise InstallSafetyError(f"Tracker artifact {artifact_id} source checksum drifted")
        accepted = artifact.get("accepted_legacy_sha256", [])
        if not isinstance(accepted, list) or any(
            not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item) for item in accepted
        ):
            raise InstallSafetyError(f"Tracker artifact {artifact_id} legacy checksums are invalid")
        if len(accepted) != len(set(accepted)):
            raise InstallSafetyError(f"Tracker artifact {artifact_id} legacy checksums are duplicated")
        board_version = artifact.get("board_data_version")
        if policy == "generated":
            if board_version != value["board_data_version"]:
                raise InstallSafetyError(f"Tracker generated artifact {artifact_id} version is invalid")
            if not isinstance(artifact.get("generator"), str) or not artifact["generator"]:
                raise InstallSafetyError(f"Tracker generated artifact {artifact_id} generator is invalid")
        elif "board_data_version" in artifact or "generator" in artifact:
            raise InstallSafetyError(f"Tracker artifact {artifact_id} has generated-only fields")
        if policy != "managed_block" and (
            "block" in artifact
            or "destination_sources" in artifact
            or "destination_source_sha256" in artifact
        ):
            raise InstallSafetyError(f"Tracker artifact {artifact_id} has instruction-only fields")
        if policy not in {"managed", "managed_block"} and "accepted_legacy_sha256" in artifact:
            raise InstallSafetyError(
                f"Tracker artifact {artifact_id} has policy-incompatible legacy checksums"
            )
        if action != "retire" and "replaces_artifact_id" in artifact and policy != "managed":
            raise InstallSafetyError(
                f"Tracker artifact {artifact_id} has a policy-incompatible replacement id"
            )
    ordered_targets = sorted(
        normalized_targets,
        key=lambda item: (len(Path(item).parts), item.casefold()),
    )
    for position, parent in enumerate(ordered_targets):
        parent_parts = tuple(part.casefold() for part in Path(parent).parts)
        for child in ordered_targets[position + 1 :]:
            child_parts = tuple(part.casefold() for part in Path(child).parts)
            if child_parts[: len(parent_parts)] == parent_parts:
                raise InstallSafetyError(
                    f"Tracker artifact target is an ancestor of another target: {parent}"
                )
    if artifact_ids != set(REQUIRED_ARTIFACT_SEMANTICS):
        missing = sorted(set(REQUIRED_ARTIFACT_SEMANTICS) - artifact_ids)
        raise InstallSafetyError(
            "Tracker artifacts are missing required ids: " + ", ".join(missing)
        )
    replacement_ids: set[str] = set()
    for artifact in artifacts:
        replacement = artifact.get("replaces_artifact_id")
        if replacement is not None:
            if (
                not isinstance(replacement, str)
                or not replacement
                or replacement == artifact["id"]
                or replacement in artifact_ids
                or replacement in replacement_ids
            ):
                raise InstallSafetyError(f"Tracker artifact {artifact['id']} replacement id is invalid")
            replacement_ids.add(replacement)
        generator = artifact.get("generator")
        if generator is not None:
            generator_artifact = next(
                (candidate for candidate in artifacts if candidate.get("id") == generator),
                None,
            )
            if generator_artifact is None or generator_artifact.get("policy") != "managed":
                raise InstallSafetyError(
                    f"Tracker artifact {artifact['id']} references an invalid generator"
                )
    return value


def validate_tracker_release_identity(manifest: dict[str, Any]) -> None:
    status = manifest.get("release_status")
    tag = manifest.get("published_tag")
    commit = manifest.get("immutable_commit")
    source_commit = manifest.get("source_checkout_commit")
    if status == "unreleased":
        if tag is not None or commit is not None or source_commit is not None:
            raise InstallSafetyError("Unreleased tracker metadata must not claim stable identity")
        return
    if status != "stable":
        raise InstallSafetyError("Tracker release_status must be unreleased or stable")
    if not isinstance(tag, str) or not tag or "PLACEHOLDER" in tag.upper():
        raise InstallSafetyError("Stable tracker published_tag is invalid")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise InstallSafetyError("Stable tracker immutable_commit must be 40 lowercase hex characters")
    if source_commit != commit:
        raise InstallSafetyError("Stable tracker source checkout commit does not match immutable commit")
    if "-dev." in manifest["component_version"]:
        raise InstallSafetyError("Stable tracker component_version must not be a development version")


def build_candidate(
    skill: Path,
    project_root: Path,
    instruction_files: tuple[str, ...],
    temporary_root: Path,
    manifest: dict[str, Any],
) -> tuple[Path, int, list[str], dict[str, Path], dict[str, str]]:
    candidate_root = temporary_root / "candidate-project"
    candidate_root.mkdir()
    copy_existing_v4_data(project_root, candidate_root)
    copy_user_tree(
        project_root,
        "tasks/ideation",
        candidate_root,
        ".agents/project_management/tasks/ideation",
    )
    seed_candidate_files(skill, candidate_root, project_root.name, manifest)

    v1, v1_project, v1_retirements, v1_metadata = collect_v1(project_root, candidate_root)
    v2, v2_project, v2_retirements, v2_metadata = collect_v2(project_root, candidate_root)
    v3, v3_project, v3_retirements, v3_metadata = collect_v3(project_root, candidate_root)
    migrated = merge_migrations(candidate_root, v1 + v2 + v3)
    layout = layout_from_root(candidate_root)
    tracker = read_json(layout.tracker_path)
    legacy_project = v3_project or v2_project or v1_project
    if legacy_project:
        tracker["project"] = legacy_project
    migration_metadata = tracker.get("migration_metadata", {})
    if not isinstance(migration_metadata, dict):
        raise InstallConflict("Tracker migration_metadata must be an object")
    for namespace, metadata in {**v1_metadata, **v2_metadata, **v3_metadata}.items():
        existing = migration_metadata.get(namespace)
        if existing is not None and existing != metadata:
            raise InstallConflict(f"Legacy metadata conflicts in migration namespace: {namespace}")
        migration_metadata[namespace] = metadata
    if migration_metadata:
        tracker["migration_metadata"] = migration_metadata
    tracker.setdefault("schema_version", 4)
    write_json_atomic(layout.tracker_path, tracker)
    subprocess.run(
        [sys.executable, str(layout.scripts_root / "archive_tasks.py"), "--no-render"],
        cwd=candidate_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, str(layout.scripts_root / "render_tasks.py")],
        cwd=candidate_root,
        check=True,
        capture_output=True,
        text=True,
    )
    candidate_by_artifact: dict[str, Path] = {}
    candidate_errors: dict[str, str] = {}
    state = load_install_state(project_root)
    state_ids = {item.get("id") for item in (state or {}).get("artifacts", []) if isinstance(item, dict)}
    for instruction_file in instruction_files:
        block_payload = instruction_block_payload(skill, instruction_file)
        current = read_target(project_root, instruction_file)
        artifact_id = f"instruction-block:{instruction_file}"
        try:
            payload, _ = instruction_candidate(
                current,
                block_payload,
                has_state=artifact_id in state_ids,
            )
        except InstallConflict as exc:
            payload = block_payload.rstrip() + b"\n"
            candidate_errors[artifact_id] = str(exc)
        candidate = temporary_root / "instructions" / instruction_file
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(payload)
        candidate_by_artifact[artifact_id] = candidate
    return (
        candidate_root,
        migrated,
        sorted(set(v1_retirements + v2_retirements + v3_retirements)),
        candidate_by_artifact,
        candidate_errors,
    )


def candidate_artifacts(
    manifest: dict[str, Any],
    candidate_root: Path,
    instruction_files: tuple[str, ...],
    instruction_candidates: dict[str, Path],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for raw in manifest.get("artifacts", []):
        if not isinstance(raw, dict):
            raise InstallSafetyError("Tracker install manifest artifact must be an object")
        target = raw.get("target")
        if target == "{instruction_file}":
            for instruction_file in instruction_files:
                artifact_id = f"{raw['id']}:{instruction_file}"
                artifacts.append(
                    {
                        **raw,
                        "id": artifact_id,
                        "target": instruction_file,
                        "candidate": instruction_candidates[artifact_id],
                    }
                )
            continue
        target = parse_relative(target, label=f"Artifact {raw.get('id')} target")
        candidate = None if raw.get("action") == "retire" else candidate_root / target
        artifacts.append({**raw, "target": target, "candidate": candidate})
    known_targets = {item["target"] for item in artifacts}
    dynamic_roots: list[tuple[Path, str]] = []
    for rule in manifest["dynamic_artifact_rules"]:
        pattern = rule["path_pattern"]
        marker_positions = [position for marker in ("/YYYY", "/<") if (position := pattern.find(marker)) > 0]
        if not marker_positions:
            raise InstallSafetyError(f"Dynamic artifact pattern has no variable segment: {pattern}")
        fixed_root = parse_relative(
            pattern[: min(marker_positions)], label="Dynamic artifact root"
        )
        prefix = "archive" if "/task_tracking/completed" in fixed_root else "ideation"
        dynamic_roots.append((candidate_root / fixed_root, prefix))
    for dynamic_root, artifact_prefix in dynamic_roots:
        if not dynamic_root.exists():
            continue
        for candidate in sorted(path for path in dynamic_root.rglob("*") if path.is_file()):
            target = candidate.relative_to(candidate_root).as_posix()
            if target in known_targets:
                continue
            artifacts.append(
                {
                    "id": f"{artifact_prefix}:{target}",
                    "policy": "user_data",
                    "target": target,
                    "candidate": candidate,
                    "checksum": {"algorithm": "sha256", "mode": "candidate"},
                }
            )
            known_targets.add(target)
    targets = [item["target"] for item in artifacts]
    folded: dict[str, str] = {}
    for target in targets:
        key = target.casefold()
        if key in folded:
            raise InstallSafetyError(f"Duplicate or case-fold tracker target: {folded[key]} and {target}")
        folded[key] = target
    return artifacts


def plan_install(
    skill: Path,
    project_root: Path,
    instruction_files: tuple[str, ...],
    temporary_root: Path,
    archive_checksum: str | None = None,
) -> InstallPlan:
    manifest = load_install_manifest(skill)
    manifest_checksum = checksum(
        read_file(safe_path(skill, "assets/install-manifest.json", allow_missing=False))
    )
    state = load_install_state(project_root)
    receipt_findings = tracker_receipt_findings(project_root, state) if state is not None else []
    if receipt_findings:
        source_identity = tracker_source_identity(
            skill, manifest, manifest_checksum, archive_checksum
        )
        conflicts = tuple(
            {
                "artifact_id": "tracker-receipt",
                "target": finding.get(
                    "path", ".agents/project_management/setup/receipts"
                ),
                "reason": (
                    "tracker durable state does not have a canonical matching receipt; "
                    "receipt repair requires a separately approved state-only repair"
                ),
                "code": finding["code"],
            }
            for finding in receipt_findings
        )
        token_identity = {
            "target_root": str(project_root.resolve(strict=True)),
            "manifest_sha256": manifest_checksum,
            "source_identity": source_identity,
            "instruction_files": list(instruction_files),
            "receipt_conflicts": list(conflicts),
        }
        return InstallPlan(
            operations=(),
            conflicts=conflicts,
            state={},
            migrated_tasks=0,
            candidate_by_artifact={},
            plan_token=f"tracker-plan-{checksum(canonical_json(token_identity))[:24]}",
        )
    candidate_root, migrated, retirements, instruction_candidates, candidate_errors = build_candidate(
        skill, project_root, instruction_files, temporary_root, manifest
    )
    state_records = {
        item["id"]: item
        for item in (state or {}).get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    artifacts = candidate_artifacts(
        manifest, candidate_root, instruction_files, instruction_candidates
    )
    operations: list[InstallOperation] = []
    conflicts: list[dict[str, str]] = [
        {
            "artifact_id": artifact_id,
            "target": artifact_id.split(":", 1)[1],
            "reason": reason,
        }
        for artifact_id, reason in candidate_errors.items()
    ]
    artifact_state: list[dict[str, Any]] = []
    for artifact in artifacts:
        artifact_id = artifact["id"]
        target = artifact["target"]
        policy = artifact["policy"]
        candidate = artifact["candidate"]
        current_payload = read_target(project_root, target)
        current_checksum = checksum(current_payload) if current_payload is not None else None
        previous = state_records.get(artifact_id)
        replacement_id = artifact.get("replaces_artifact_id")
        if previous is None and isinstance(replacement_id, str):
            previous = state_records.get(replacement_id)

        if artifact_id in candidate_errors:
            continue

        if artifact.get("action") == "retire":
            if current_payload is None:
                continue
            accepted = set(artifact.get("accepted_legacy_sha256", []))
            baseline = previous.get("installed_sha256") if previous else None
            if current_checksum != baseline and current_checksum not in accepted:
                conflicts.append(
                    {
                        "artifact_id": artifact_id,
                        "target": target,
                        "reason": "retired managed artifact was customized or is not positively owned",
                    }
                )
                continue
            operations.append(
                InstallOperation(
                    artifact_id=artifact_id,
                    policy="retired",
                    action="delete",
                    target=target,
                    candidate=None,
                    before_sha256=current_checksum,
                    after_sha256=None,
                )
            )
            continue

        if candidate is None or not candidate.is_file():
            raise InstallSafetyError(f"Candidate artifact is missing: {artifact_id}")
        candidate_payload = read_file(candidate)
        candidate_checksum = checksum(candidate_payload)

        if policy == "managed_block":
            block_payload = instruction_block_payload(skill, target)
            try:
                _, current_block_checksum = instruction_candidate(
                    current_payload,
                    block_payload,
                    has_state=previous is not None,
                )
            except InstallConflict as exc:
                conflicts.append({"artifact_id": artifact_id, "target": target, "reason": str(exc)})
                continue
            if previous and current_block_checksum != previous.get("installed_sha256"):
                conflicts.append(
                    {
                        "artifact_id": artifact_id,
                        "target": target,
                        "reason": "managed task-tracker block was customized",
                    }
                )
                continue
            start = block_payload.decode().index(BLOCK_START)
            end = block_payload.decode().index(BLOCK_END) + len(BLOCK_END)
            installed_checksum = checksum(block_payload.decode()[start:end].encode())
            if previous is None and current_block_checksum != installed_checksum:
                conflicts.append(
                    {
                        "artifact_id": artifact_id,
                        "target": target,
                        "reason": "existing managed task-tracker block has no recognized baseline",
                    }
                )
                continue
        elif policy == "managed":
            accepted = set(artifact.get("accepted_legacy_sha256", []))
            baseline = previous.get("installed_sha256") if previous else None
            if previous and current_checksum != baseline:
                conflicts.append(
                    {"artifact_id": artifact_id, "target": target, "reason": "managed artifact was customized"}
                )
                continue
            if previous is None and current_payload is not None:
                if current_checksum != candidate_checksum and current_checksum not in accepted:
                    conflicts.append(
                        {
                            "artifact_id": artifact_id,
                            "target": target,
                            "reason": "existing managed path has no recognized ownership baseline",
                        }
                    )
                    continue
            installed_checksum = candidate_checksum
        elif policy in {"seed", "user_data"}:
            installed_checksum = current_checksum or candidate_checksum
        elif policy == "generated":
            installed_checksum = candidate_checksum
        else:
            raise InstallSafetyError(f"Unsupported tracker artifact policy: {policy}")

        if current_payload != candidate_payload:
            operations.append(
                InstallOperation(
                    artifact_id=artifact_id,
                    policy=policy,
                    action="write",
                    target=target,
                    candidate=candidate,
                    before_sha256=current_checksum,
                    after_sha256=candidate_checksum,
                )
            )
        artifact_state.append(
            {
                "id": artifact_id,
                "component_version": manifest["component_version"],
                "policy": policy,
                "target": target,
                "installed_sha256": installed_checksum,
                "source_sha256": candidate_checksum,
            }
        )

    for relative in retirements:
        payload = read_target(project_root, relative)
        if payload is None:
            continue
        operations.append(
            InstallOperation(
                artifact_id=f"retire:{relative}",
                policy="retired",
                action="delete",
                target=relative,
                candidate=None,
                before_sha256=checksum(payload),
                after_sha256=None,
            )
        )

    selected_ids = {item["id"] for item in artifacts}
    replaced_ids = {
        item["replaces_artifact_id"]
        for item in artifacts
        if isinstance(item.get("replaces_artifact_id"), str)
    }
    for artifact_id, record in state_records.items():
        if artifact_id not in selected_ids and artifact_id not in replaced_ids:
            artifact_state.append(record)

    operations.sort(key=lambda item: (item.target.casefold(), item.artifact_id))
    conflicts.sort(key=lambda item: (item["target"].casefold(), item["artifact_id"]))
    artifact_state.sort(key=lambda item: item["id"])
    state_payload = {
        "state_schema_version": 1,
        "bundle_version": manifest.get("bundle_version"),
        "component": "task-tracking-setup",
        "component_version": manifest["component_version"],
        "manifest_schema_version": manifest.get("manifest_schema_version"),
        "tracker_data_schema_version": manifest.get("tracker_data_schema_version"),
        "board_data_version": manifest.get("board_data_version"),
        "artifact_policies": manifest.get("artifact_policies"),
        "artifacts": artifact_state,
        "migrations": manifest.get("migrations", []),
        "source_identity": tracker_source_identity(
            skill, manifest, manifest_checksum, archive_checksum
        ),
    }
    token_payload = {
        "target_root": str(project_root.resolve(strict=True)),
        "manifest_sha256": manifest_checksum,
        "source_identity": state_payload["source_identity"],
        "instruction_files": list(instruction_files),
        "operations": [
            {
                "artifact_id": item.artifact_id,
                "policy": item.policy,
                "action": item.action,
                "target": item.target,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
            }
            for item in operations
        ],
        "artifacts": artifact_state,
        "candidate_sha256": {
            item["id"]: (
                checksum(read_file(item["candidate"]))
                if isinstance(item.get("candidate"), Path)
                else None
            )
            for item in artifacts
        },
    }
    plan_token = f"tracker-plan-{checksum(canonical_json(token_payload))[:24]}"
    state_payload["approved_plan_token"] = plan_token
    if tracker_state_content(state) == tracker_state_content(state_payload):
        state_payload["last_successful_transaction"] = (state or {}).get(
            "last_successful_transaction"
        )
    else:
        state_payload["last_successful_transaction"] = transaction_id(
            operations, manifest["component_version"], state_payload
        )
    return InstallPlan(
        operations=tuple(operations),
        conflicts=tuple(conflicts),
        state=state_payload,
        migrated_tasks=migrated,
        candidate_by_artifact={item["id"]: item["candidate"] for item in artifacts},
        plan_token=plan_token,
    )


def plan_payload(plan: InstallPlan, *, dry_run: bool) -> dict[str, Any]:
    return {
        "command": "install",
        "status": "conflict" if plan.conflicts else ("planned" if dry_run else "ready"),
        "dry_run": dry_run,
        "component_version": plan.state.get("component_version"),
        "plan_token": plan.plan_token,
        "migrated_tasks": plan.migrated_tasks,
        "conflicts": list(plan.conflicts),
        "operations": [
            {
                "action": item.action,
                "artifact_id": item.artifact_id,
                "after_sha256": item.after_sha256,
                "before_sha256": item.before_sha256,
                "policy": item.policy,
                "target": item.target,
            }
            for item in plan.operations
        ],
    }


def write_tracker_conflict_report(project_root: Path, plan: InstallPlan) -> str:
    tx_id = transaction_id(list(plan.operations), str(plan.state.get("component_version")))
    conflict_root = (
        f".agents/project_management/setup/.runtime/conflicts/{tx_id}-tracker-install"
    )
    for conflict in plan.conflicts:
        candidate = plan.candidate_by_artifact.get(conflict["artifact_id"])
        if candidate is not None:
            atomic_write(
                project_root,
                f"{conflict_root}/candidate/{conflict['target']}",
                read_file(candidate),
            )
    report = f"{conflict_root}/report.json"
    atomic_write(project_root, report, canonical_json(plan_payload(plan, dry_run=False)))
    return report


def execute_install(
    skill: Path,
    project_root: Path,
    instruction_files: tuple[str, ...],
    *,
    dry_run: bool,
    plan_token: str | None = None,
    fault_injector: Callable[[str], None] | None = None,
    archive_checksum: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="task-tracking-setup-plan-") as temporary:
            try:
                plan = plan_install(
                    skill,
                    project_root,
                    instruction_files,
                    Path(temporary),
                    archive_checksum,
                )
            except SystemExit as exc:
                raise InstallSafetyError(str(exc)) from exc
            return (1 if plan.conflicts else 0), plan_payload(plan, dry_run=True)
    if not isinstance(plan_token, str) or not plan_token:
        raise InstallSafetyError("Tracker apply requires --plan-token from an approved dry-run")
    preflight_state = load_install_state(project_root)
    if preflight_state is not None and tracker_receipt_findings(
        project_root, preflight_state
    ):
        with tempfile.TemporaryDirectory(prefix="task-tracking-setup-preflight-") as temporary:
            plan = plan_install(
                skill,
                project_root,
                instruction_files,
                Path(temporary),
                archive_checksum,
            )
        payload = plan_payload(plan, dry_run=False)
        payload["status"] = "conflict"
        payload["recovery"] = None
        return 1, payload
    with InstallLock(project_root):
        recovery = recover_install_transaction(project_root)
        with tempfile.TemporaryDirectory(prefix="task-tracking-setup-apply-") as temporary:
            try:
                plan = plan_install(
                    skill,
                    project_root,
                    instruction_files,
                    Path(temporary),
                    archive_checksum,
                )
            except SystemExit as exc:
                raise InstallSafetyError(str(exc)) from exc
            payload = plan_payload(plan, dry_run=False)
            if plan.plan_token != plan_token:
                recovered_state = load_install_state(project_root)
                if (
                    recovered_state is not None
                    and not plan.conflicts
                    and not plan.operations
                    and comparable_tracker_states(recovered_state, plan.state)
                    and recovered_state.get("approved_plan_token") == plan_token
                    and has_consistent_tracker_receipt(project_root, recovered_state)
                ):
                    payload["status"] = "current"
                    payload["recovery"] = recovery.as_dict() if recovery else None
                    return 0, payload
                payload["status"] = "conflict"
                payload["conflicts"] = [
                    {
                        "artifact_id": "approved-plan",
                        "target": ".agents/project_management/tasks",
                        "reason": "target or tracker source changed after the approved dry-run",
                    }
                ]
                return 1, payload
            if plan.conflicts:
                payload["status"] = "conflict"
                if not any(
                    conflict.get("artifact_id") == "tracker-receipt"
                    for conflict in plan.conflicts
                ):
                    payload["conflict_report"] = write_tracker_conflict_report(
                        project_root, plan
                    )
                return 1, payload
            current_state = load_install_state(project_root)
            if not plan.operations and comparable_tracker_states(current_state, plan.state):
                if current_state is None or not has_consistent_tracker_receipt(
                    project_root, current_state
                ):
                    payload["status"] = "conflict"
                    payload["conflicts"] = [
                        {
                            "artifact_id": "tracker-receipt",
                            "target": ".agents/project_management/setup/receipts",
                            "reason": (
                                "tracker durable state does not have a canonical matching receipt; "
                                "receipt repair requires a separately approved state-only repair"
                            ),
                        }
                    ]
                    return 1, payload
                payload["status"] = "current"
                payload["recovery"] = recovery.as_dict() if recovery else None
                return 0, payload
            transaction = apply_install_transaction(
                project_root,
                list(plan.operations),
                plan.state,
                str(plan.state["component_version"]),
                fault_injector=fault_injector,
            )
            payload["status"] = "installed" if plan.operations else "recorded"
            payload["transaction_id"] = transaction
            payload["recovery"] = recovery.as_dict() if recovery else None
            return 0, payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or apply local task tracker installation")
    parser.add_argument("--root", required=True, help="Exact absolute target project root")
    parser.add_argument(
        "--instruction-file",
        action="append",
        choices=("AGENTS.md", "CLAUDE.md"),
        default=[],
        help="Update this approved root instruction file; repeat for both",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan and validate without target writes")
    parser.add_argument("--plan-token", help="Exact token returned by the approved dry-run")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--archive-sha256", help=argparse.SUPPRESS)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    project_root = Path(args.root)
    if not project_root.is_absolute() or not project_root.is_dir():
        raise SystemExit(f"Project root must be an existing absolute directory: {project_root}")
    skill = Path(__file__).resolve().parent.parent
    instruction_files = tuple(dict.fromkeys(args.instruction_file))
    try:
        exit_code, payload = execute_install(
            skill,
            project_root,
            instruction_files,
            dry_run=args.dry_run,
            plan_token=args.plan_token,
            archive_checksum=args.archive_sha256,
        )
    except (InstallSafetyError, InstallConflict, subprocess.CalledProcessError) as exc:
        exit_code = 2
        payload = {"command": "install", "status": "invalid", "error": str(exc)}
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"tracker-install: {payload['status']}")
        for key in sorted(payload):
            if key in {"command", "status"}:
                continue
            print(f"{key}: {json.dumps(payload[key], sort_keys=True)}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

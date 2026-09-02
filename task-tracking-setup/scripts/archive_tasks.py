#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

from install_transaction import (
    InstallLock,
    InstallSafetyError,
    atomic_write,
    canonical_json,
    checksum,
    read_target,
    remove_file,
    safe_path,
)
from task_store import (
    envelope_with_tasks,
    layout_from_script,
    load_sources,
    parse_timestamp,
    read_json,
    tasks_from_source,
    validate_store,
)

ARCHIVE_JOURNAL = ".agents/project_management/tasks/setup/.runtime/archive-journal.json"
ARCHIVE_TRANSACTIONS = ".agents/project_management/tasks/setup/.runtime/archive-transactions"


def archive_relative(layout: Any, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(layout.project_root.resolve()).as_posix()
    except ValueError as exc:
        raise InstallSafetyError(f"Archive target is outside the project: {path}") from exc
    allowed_archive = layout.archive_root.resolve()
    if path.resolve() != layout.active_paths["in_progress"].resolve() and allowed_archive not in path.resolve().parents:
        raise InstallSafetyError(f"Unexpected archive transaction target: {relative}")
    return relative


def cleanup_archive_transaction(layout: Any, journal: dict[str, Any]) -> None:
    for operation in journal["operations"]:
        for key in ("backup", "candidate"):
            relative = operation.get(key)
            if isinstance(relative, str):
                remove_file(layout.project_root, relative)
    transaction_dir = safe_path(
        layout.project_root,
        f"{ARCHIVE_TRANSACTIONS}/{journal['transaction_id']}",
    )
    if transaction_dir.exists():
        transaction_dir.rmdir()


def parse_archive_journal(layout: Any, payload: bytes) -> dict[str, Any]:
    try:
        journal = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallSafetyError("Archive transaction journal is invalid") from exc
    if (
        not isinstance(journal, dict)
        or set(journal) != {"operations", "schema_version", "transaction_id"}
        or journal.get("schema_version") != 1
    ):
        raise InstallSafetyError("Archive transaction journal schema is invalid")
    transaction_id = journal.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id.startswith("archive-"):
        raise InstallSafetyError("Archive transaction id is invalid")
    operations = journal.get("operations")
    if not isinstance(operations, list) or len(operations) < 2:
        raise InstallSafetyError("Archive transaction operations are invalid")
    seen_targets: set[str] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise InstallSafetyError(f"Archive transaction operation {index} is invalid")
        if set(operation) != {
            "after_sha256",
            "backup",
            "before_sha256",
            "candidate",
            "existed",
            "target",
        }:
            raise InstallSafetyError(f"Archive transaction operation {index} fields are invalid")
        target = operation.get("target")
        target_path = safe_path(layout.project_root, target)
        archive_relative(layout, target_path)
        if target in seen_targets:
            raise InstallSafetyError(f"Archive transaction duplicates target: {target}")
        seen_targets.add(target)
        expected_base = f"{ARCHIVE_TRANSACTIONS}/{transaction_id}/{index}"
        if operation.get("candidate") != f"{expected_base}.candidate":
            raise InstallSafetyError("Archive candidate path is invalid")
        existed = operation.get("existed")
        if not isinstance(existed, bool):
            raise InstallSafetyError("Archive transaction existed flag is invalid")
        expected_backup = f"{expected_base}.backup" if existed else None
        if operation.get("backup") != expected_backup:
            raise InstallSafetyError("Archive backup path is invalid")
        if not existed and operation.get("before_sha256") is not None:
            raise InstallSafetyError("Archive transaction before checksum must be null")
        for key in ("before_sha256", "after_sha256"):
            value = operation.get(key)
            if (key == "before_sha256" and not existed and value is None):
                continue
            if not isinstance(value, str) or len(value) != 64:
                raise InstallSafetyError(f"Archive transaction {key} is invalid")
    in_progress_target = archive_relative(layout, layout.active_paths["in_progress"])
    if operations[-1].get("target") != in_progress_target:
        raise InstallSafetyError("Archive transaction commit target must be in-progress.json")
    return journal


def recover_archive_transaction(layout: Any) -> str | None:
    payload = read_target(layout.project_root, ARCHIVE_JOURNAL)
    if payload is None:
        return None
    journal = parse_archive_journal(layout, payload)
    operations = journal["operations"]
    drifted_targets: list[str] = []
    for operation in operations:
        target_payload = read_target(layout.project_root, operation["target"])
        target_checksum = checksum(target_payload) if target_payload is not None else None
        if target_checksum not in {operation["before_sha256"], operation["after_sha256"]}:
            drifted_targets.append(operation["target"])

        candidate = read_target(layout.project_root, operation["candidate"])
        if candidate is None or checksum(candidate) != operation["after_sha256"]:
            raise InstallSafetyError(
                f"Archive recovery candidate is missing or corrupt for {operation['target']}: "
                f"{operation['candidate']}; target, journal, and backups were preserved"
            )
        backup_relative = operation["backup"]
        if operation["existed"]:
            backup = read_target(layout.project_root, backup_relative)
            if backup is None or checksum(backup) != operation["before_sha256"]:
                raise InstallSafetyError(
                    f"Archive recovery backup is missing or corrupt for {operation['target']}: "
                    f"{backup_relative}; target, journal, and candidates were preserved"
                )
    if drifted_targets:
        raise InstallSafetyError(
            "Archive recovery aborted because targets contain post-crash edits; no files were "
            "changed. Reconcile while preserving the journal and transaction artifacts: "
            + ", ".join(sorted(drifted_targets))
        )
    if all(
        (checksum(current) if current is not None else None) == operation["after_sha256"]
        for operation in operations
        for current in [read_target(layout.project_root, operation["target"])]
    ):
        remove_file(layout.project_root, ARCHIVE_JOURNAL)
        cleanup_archive_transaction(layout, journal)
        return "completed"
    for operation in reversed(operations):
        if not operation["existed"]:
            remove_file(layout.project_root, operation["target"])
            continue
        backup = read_target(layout.project_root, operation["backup"])
        assert backup is not None
        atomic_write(layout.project_root, operation["target"], backup)
    remove_file(layout.project_root, ARCHIVE_JOURNAL)
    cleanup_archive_transaction(layout, journal)
    return "rolled_back"


def apply_archive_replacements(
    layout: Any,
    replacements: dict[Path, dict[str, Any]],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> str:
    in_progress = layout.active_paths["in_progress"].resolve()
    ordered = sorted(
        replacements.items(),
        key=lambda item: (item[0] == in_progress, item[0].as_posix()),
    )
    identity = [
        {"target": archive_relative(layout, path), "after_sha256": checksum(canonical_json(value))}
        for path, value in ordered
    ]
    transaction_id = f"archive-{checksum(canonical_json(identity))[:20]}"
    operations: list[dict[str, Any]] = []
    for index, (path, value) in enumerate(ordered):
        target = archive_relative(layout, path)
        before = read_target(layout.project_root, target)
        candidate = canonical_json(value)
        base = f"{ARCHIVE_TRANSACTIONS}/{transaction_id}/{index}"
        backup_path = f"{base}.backup" if before is not None else None
        candidate_path = f"{base}.candidate"
        if before is not None:
            atomic_write(layout.project_root, backup_path, before)
        atomic_write(layout.project_root, candidate_path, candidate)
        operations.append(
            {
                "target": target,
                "existed": before is not None,
                "before_sha256": checksum(before) if before is not None else None,
                "after_sha256": checksum(candidate),
                "backup": backup_path,
                "candidate": candidate_path,
            }
        )
    journal = {"schema_version": 1, "transaction_id": transaction_id, "operations": operations}
    atomic_write(layout.project_root, ARCHIVE_JOURNAL, canonical_json(journal))
    if fault_injector is not None:
        fault_injector("after_journal")
    try:
        for index, operation in enumerate(operations):
            candidate = read_target(layout.project_root, operation["candidate"])
            if candidate is None:
                raise InstallSafetyError(f"Archive candidate is missing: {operation['target']}")
            atomic_write(layout.project_root, operation["target"], candidate)
            if fault_injector is not None:
                fault_injector(f"after_write_{index}")
    except Exception:
        recover_archive_transaction(layout)
        raise
    remove_file(layout.project_root, ARCHIVE_JOURNAL)
    cleanup_archive_transaction(layout, journal)
    return transaction_id


def archive_completed(
    layout: Any, *, fault_injector: Callable[[str], None] | None = None
) -> tuple[int, int]:
    tracker, sources = load_sources(layout)
    tasks = validate_store(layout, tracker, sources)
    in_progress_path = layout.active_paths["in_progress"]
    in_progress_envelope = next(
        envelope for path, envelope in sources if path.resolve() == in_progress_path.resolve()
    )
    in_progress_tasks = tasks_from_source(in_progress_path, in_progress_envelope)
    in_progress_ids = {task["id"] for task in in_progress_tasks}
    by_id = {task["id"]: task for task in tasks}
    children: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        if task["parent_id"]:
            children[task["parent_id"]].append(task["id"])

    def bundle(root_id: str) -> list[str]:
        result = [root_id]
        position = 0
        while position < len(result):
            result.extend(children[result[position]])
            position += 1
        return result

    archive_groups: dict[Path, list[dict]] = defaultdict(list)
    archived_ids: set[str] = set()
    for root in in_progress_tasks:
        if root["status"] != "done" or root["parent_id"]:
            continue
        bundle_ids = bundle(root["id"])
        if not all(
            item in in_progress_ids and by_id[item]["status"] == "done" for item in bundle_ids
        ):
            continue
        completed_at = parse_timestamp(root["completed_at"], f"Task {root['id']} completed_at")
        week = completed_at.isocalendar().week
        archive_path = (
            layout.archive_root
            / f"{completed_at.year:04d}"
            / f"{completed_at.month:02d}"
            / f"week-{week:02d}"
            / "tasks.json"
        )
        archive_groups[archive_path].extend(by_id[item] for item in bundle_ids)
        archived_ids.update(bundle_ids)

    if archive_groups:
        replacements: dict[Path, dict] = {}
        for path, records in archive_groups.items():
            envelope = read_json(path) if path.exists() else {"tasks": []}
            existing = tasks_from_source(path, envelope)
            duplicates = {task["id"] for task in existing} & {task["id"] for task in records}
            if duplicates:
                raise SystemExit(f"Archive already contains IDs: {', '.join(sorted(duplicates))}")
            replacements[path.resolve()] = envelope_with_tasks(envelope, existing + records)
        replacements[in_progress_path.resolve()] = envelope_with_tasks(
            in_progress_envelope,
            [task for task in in_progress_tasks if task["id"] not in archived_ids],
        )
        candidate_sources = [
            (path, replacements.get(path.resolve(), envelope)) for path, envelope in sources
        ]
        known_paths = {path.resolve() for path, _ in candidate_sources}
        candidate_sources.extend(
            (path, envelope) for path, envelope in replacements.items() if path not in known_paths
        )
        validate_store(layout, tracker, candidate_sources)
        apply_archive_replacements(layout, replacements, fault_injector=fault_injector)

    return len(archived_ids), len(archive_groups)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive eligible completed project tasks")
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    layout = layout_from_script(Path(__file__))
    with InstallLock(layout.project_root):
        recover_archive_transaction(layout)
        archived_count, group_count = archive_completed(layout)

        if not args.no_render:
            subprocess.run(
                [sys.executable, str(layout.scripts_root / "render_tasks.py")],
                cwd=layout.project_root,
                check=True,
            )
    print(f"Archived {archived_count} tasks across {group_count} weekly files")


if __name__ == "__main__":
    main()

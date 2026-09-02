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
from typing import Any, Callable


LOCK_RELATIVE = ".agents/project_management/setup/.runtime/coordinator.lock"
JOURNAL_RELATIVE = ".agents/project_management/setup/.runtime/tracker-install-journal.json"
STATE_RELATIVE = ".agents/project_management/setup/tracker-install-state.json"
RECEIPT_ROOT = ".agents/project_management/setup/receipts"


class InstallSafetyError(Exception):
    pass


class InstallConflict(InstallSafetyError):
    pass


class InstallLockBusy(InstallSafetyError):
    pass


class InjectedInstallFault(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallOperation:
    artifact_id: str
    policy: str
    action: str
    target: str
    candidate: Path | None
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    transaction_id: str

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "transaction_id": self.transaction_id}


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_relative(raw: Any, *, label: str = "path") -> str:
    if not isinstance(raw, str) or not raw or raw == ".":
        raise InstallSafetyError(f"{label} must be a non-empty project-relative path")
    if "\x00" in raw or "\\" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise InstallSafetyError(f"{label} is unsafe: {raw!r}")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise InstallSafetyError(f"{label} is not normalized: {raw!r}")
    if PurePosixPath(raw).as_posix() != raw:
        raise InstallSafetyError(f"{label} is not normalized: {raw!r}")
    return raw


def assert_root(root: Path) -> None:
    if not root.is_absolute():
        raise InstallSafetyError(f"Target root must be absolute: {root}")
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallSafetyError(f"Target root must be a non-symlink directory: {root}")


def safe_path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    assert_root(root)
    relative = parse_relative(relative)
    current = root
    missing = False
    for position, part in enumerate(PurePosixPath(relative).parts):
        current = current / part
        if missing:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise InstallSafetyError(f"Required path does not exist: {current}")
            missing = True
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise InstallSafetyError(f"Symlinked path component is not allowed: {current}")
        if position < len(PurePosixPath(relative).parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise InstallSafetyError(f"Non-directory path component: {current}")
    return current


def read_file(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallSafetyError(f"Expected non-symlink regular file: {path}")
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def read_target(root: Path, relative: str) -> bytes | None:
    path = safe_path(root, relative)
    if not path.exists():
        return None
    return read_file(path)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_parent(root: Path, relative: str) -> Path:
    target = safe_path(root, relative)
    missing: list[Path] = []
    parent = target.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if parent != root:
        safe_path(root, parent.relative_to(root).as_posix(), allow_missing=False)
    for directory in reversed(missing):
        directory.mkdir()
        fsync_directory(directory.parent)
    safe_path(root, relative)
    return target


def atomic_write(root: Path, relative: str, payload: bytes) -> None:
    target = ensure_parent(root, relative)
    safe_path(root, relative)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        safe_path(root, relative)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_file(root: Path, relative: str) -> None:
    target = safe_path(root, relative)
    if not target.exists():
        return
    if not target.is_file():
        raise InstallSafetyError(f"Refusing to remove non-file: {target}")
    target.unlink()
    fsync_directory(target.parent)


def remove_empty_runtime_parents(root: Path, relative: str) -> None:
    stop = safe_path(root, ".agents/project_management/setup/.runtime")
    current = safe_path(root, relative).parent
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        fsync_directory(current.parent)
        current = current.parent


def cleanup_transaction_files(root: Path, journal: dict[str, Any]) -> None:
    cleanup_paths: list[str] = []
    for operation in journal.get("operations", []):
        for key in ("backup", "candidate"):
            value = operation.get(key)
            if isinstance(value, str):
                cleanup_paths.append(value)
    receipt = journal.get("receipt", {})
    state = journal.get("state", {})
    for record in (receipt, state):
        for key in ("backup", "candidate"):
            if isinstance(record.get(key), str):
                cleanup_paths.append(record[key])
    for relative in sorted(set(cleanup_paths), reverse=True):
        remove_file(root, relative)
        remove_empty_runtime_parents(root, relative)


def target_checksum(root: Path, relative: str) -> str | None:
    payload = read_target(root, relative)
    return checksum(payload) if payload is not None else None


def parse_journal_target(
    record: Any, *, label: str
) -> tuple[str, str | None, str | None]:
    if not isinstance(record, dict):
        raise InstallSafetyError(f"Tracker install journal {label} is invalid")
    target = parse_relative(record.get("target"), label=f"journal {label} target")
    before = record.get("before_sha256")
    after = record.get("after_sha256")
    for state_name, value in (("before", before), ("after", after)):
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        ):
            raise InstallSafetyError(
                f"Tracker install journal {label} {state_name} checksum is invalid"
            )
    return target, before, after


def assert_recoverable_targets(root: Path, records: list[tuple[str, Any]]) -> None:
    conflicts: list[str] = []
    seen_targets: set[str] = set()
    for label, record in records:
        target, before, after = parse_journal_target(record, label=label)
        if target in seen_targets:
            raise InstallSafetyError(f"Tracker install journal duplicates target: {target}")
        seen_targets.add(target)
        if target_checksum(root, target) not in {before, after}:
            conflicts.append(target)
    if conflicts:
        raise InstallConflict(
            "Tracker install recovery aborted because targets changed after the crash: "
            + ", ".join(sorted(conflicts))
        )


def assert_recovery_material(root: Path, journal: dict[str, Any]) -> None:
    transaction = journal["transaction_id"]
    operations = journal["operations"]
    for index, operation in enumerate(operations, 1):
        _, before, after = parse_journal_target(operation, label=f"operation {index}")
        if not isinstance(operation.get("artifact_id"), str) or not operation["artifact_id"]:
            raise InstallSafetyError(
                f"Tracker install journal operation {index} artifact id is invalid"
            )
        if not isinstance(operation.get("policy"), str) or not operation["policy"]:
            raise InstallSafetyError(
                f"Tracker install journal operation {index} policy is invalid"
            )
        if operation.get("existed") is not (before is not None):
            raise InstallSafetyError(
                f"Tracker install journal operation {index} existence metadata is invalid"
            )
        backup = operation.get("backup")
        expected_base = (
            f".agents/project_management/setup/.runtime/tracker-transactions/{transaction}"
        )
        if before is not None:
            if backup != f"{expected_base}/backup/{operation['target']}":
                raise InstallSafetyError(
                    f"Tracker install recovery backup path is invalid: {backup}"
                )
            backup_path = safe_path(root, backup, allow_missing=False)
            if checksum(read_file(backup_path)) != before:
                raise InstallConflict(
                    f"Tracker install recovery backup changed after the crash: {backup}"
                )
        elif backup is not None:
            raise InstallSafetyError(
                f"Tracker install recovery has an unexpected backup for {operation['target']}"
            )
        candidate = operation.get("candidate")
        if operation.get("action") == "write":
            if candidate != f"{expected_base}/candidate/{operation['target']}":
                raise InstallSafetyError(
                    f"Tracker install recovery candidate path is invalid: {candidate}"
                )
            candidate_path = safe_path(root, candidate, allow_missing=False)
            if checksum(read_file(candidate_path)) != after:
                raise InstallConflict(
                    f"Tracker install recovery candidate changed after the crash: {candidate}"
                )
        elif operation.get("action") != "delete" or candidate is not None:
            raise InstallSafetyError(
                f"Tracker install journal operation {index} action metadata is invalid"
            )

    receipt = journal["receipt"]
    receipt_target, receipt_before, receipt_after = parse_journal_target(receipt, label="receipt")
    expected_receipt = f"{RECEIPT_ROOT}/{transaction}.json"
    if receipt_target != expected_receipt:
        raise InstallSafetyError("Tracker install journal receipt target is invalid")
    if receipt.get("existed") is not (receipt_before is not None):
        raise InstallSafetyError("Tracker install journal receipt existence metadata is invalid")
    if receipt_before is not None:
        expected_receipt_backup = (
            f".agents/project_management/setup/.runtime/tracker-transactions/{transaction}/"
            "backup/receipt.json"
        )
        if receipt.get("backup") != expected_receipt_backup:
            raise InstallSafetyError("Tracker install journal receipt backup path is invalid")
        receipt_backup = safe_path(root, receipt.get("backup"), allow_missing=False)
        if checksum(read_file(receipt_backup)) != receipt_before:
            raise InstallConflict(
                "Tracker install recovery receipt backup changed after the crash"
            )
    elif receipt.get("backup") is not None:
        raise InstallSafetyError("Tracker install journal has an unexpected receipt backup")
    receipt_candidate = receipt.get("candidate")
    expected_receipt_candidate = (
        f".agents/project_management/setup/.runtime/tracker-transactions/{transaction}/"
        "candidate/receipt.json"
    )
    if receipt_candidate != expected_receipt_candidate:
        raise InstallSafetyError("Tracker install journal receipt candidate path is invalid")
    candidate_payload = read_target(root, receipt_candidate)
    if candidate_payload is None or checksum(candidate_payload) != receipt_after:
        raise InstallConflict("Tracker install recovery receipt candidate changed after the crash")

    state_record = journal["state"]
    state_target, state_before, state_after = parse_journal_target(state_record, label="state")
    if state_target != STATE_RELATIVE:
        raise InstallSafetyError("Tracker install journal state target is invalid")
    expected_state_candidate = (
        f".agents/project_management/setup/.runtime/tracker-transactions/{transaction}/"
        "candidate/state.json"
    )
    if state_record.get("candidate") != expected_state_candidate:
        raise InstallSafetyError("Tracker install journal state candidate path is invalid")
    state_candidate = read_target(root, expected_state_candidate)
    if state_candidate is None or checksum(state_candidate) != state_after:
        raise InstallConflict("Tracker install recovery state candidate changed after the crash")
    previous = journal.get("previous_state")
    if previous is None:
        if state_before is not None:
            raise InstallSafetyError("Tracker install journal previous state is missing")
        return
    try:
        previous_payload = base64.b64decode(previous, validate=True)
    except ValueError as exc:
        raise InstallSafetyError("Tracker install journal previous state is corrupt") from exc
    if checksum(previous_payload) != state_before:
        raise InstallConflict("Tracker install recovery previous state changed after the crash")


class InstallLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.descriptor: int | None = None

    def __enter__(self) -> "InstallLock":
        path = ensure_parent(self.root, LOCK_RELATIVE)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.descriptor = os.open(path, flags, 0o600)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.descriptor)
            self.descriptor = None
            raise InstallLockBusy(f"Setup lock is held: {path}") from exc
        os.ftruncate(self.descriptor, 0)
        os.write(self.descriptor, canonical_json({"pid": os.getpid(), "owner": "tracker-installer"}))
        os.fsync(self.descriptor)
        fsync_directory(path.parent)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def load_install_state(root: Path) -> dict[str, Any] | None:
    path = safe_path(root, STATE_RELATIVE)
    if not path.exists():
        return None
    try:
        value = json.loads(read_file(path))
    except json.JSONDecodeError as exc:
        raise InstallSafetyError(f"Tracker install state is corrupt: {exc}") from exc
    if not isinstance(value, dict) or value.get("state_schema_version") != 1:
        raise InstallSafetyError("Tracker install state schema is invalid")
    return value


def transaction_id(
    operations: list[InstallOperation],
    version: str,
    state: dict[str, Any] | None = None,
) -> str:
    durable_state = dict(state or {})
    durable_state.pop("approved_plan_token", None)
    durable_state.pop("last_successful_transaction", None)
    payload = {
        "version": version,
        "operations": [
            {
                "id": item.artifact_id,
                "policy": item.policy,
                "action": item.action,
                "target": item.target,
                "before": item.before_sha256,
                "after": item.after_sha256,
            }
            for item in operations
        ],
        "durable_state_sha256": checksum(canonical_json(durable_state)) if state is not None else None,
    }
    return f"tracker-{checksum(canonical_json(payload))[:20]}"


def apply_install_transaction(
    root: Path,
    operations: list[InstallOperation],
    state: dict[str, Any],
    version: str,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> str:
    tx_id = transaction_id(operations, version, state)
    if state.get("last_successful_transaction") != tx_id:
        raise InstallSafetyError(
            "Tracker durable state transaction id does not match the transaction being applied"
        )
    receipt_relative = f"{RECEIPT_ROOT}/{tx_id}.json"
    journal_path = safe_path(root, JOURNAL_RELATIVE)
    if journal_path.exists():
        raise InstallSafetyError("Tracker install journal requires recovery")
    previous_state = load_install_state(root)
    runtime_records: list[dict[str, Any]] = []
    for operation in operations:
        current = read_target(root, operation.target)
        current_checksum = checksum(current) if current is not None else None
        if current_checksum != operation.before_sha256:
            raise InstallConflict(
                f"Target changed after planning: {operation.target}"
            )
        backup_relative = (
            f".agents/project_management/setup/.runtime/tracker-transactions/{tx_id}/"
            f"backup/{operation.target}"
        )
        if current is not None:
            atomic_write(root, backup_relative, current)
        candidate_relative = None
        if operation.candidate is not None:
            candidate_relative = (
                f".agents/project_management/setup/.runtime/tracker-transactions/{tx_id}/"
                f"candidate/{operation.target}"
            )
            atomic_write(root, candidate_relative, read_file(operation.candidate))
        runtime_records.append(
            {
                "artifact_id": operation.artifact_id,
                "policy": operation.policy,
                "action": operation.action,
                "target": operation.target,
                "before_sha256": operation.before_sha256,
                "after_sha256": operation.after_sha256,
                "backup": backup_relative if current is not None else None,
                "candidate": candidate_relative,
                "existed": current is not None,
            }
        )
    previous_receipt = read_target(root, receipt_relative)
    receipt_backup_relative = (
        f".agents/project_management/setup/.runtime/tracker-transactions/{tx_id}/"
        "backup/receipt.json"
    )
    if previous_receipt is not None:
        atomic_write(root, receipt_backup_relative, previous_receipt)
    previous_payload = canonical_json(previous_state) if previous_state is not None else None
    receipt = {
        "receipt_schema_version": 1,
        "transaction_id": tx_id,
        "component": state.get("component"),
        "component_version": version,
        "bundle_version": state.get("bundle_version"),
        "manifest_schema_version": state.get("manifest_schema_version"),
        "tracker_data_schema_version": state.get("tracker_data_schema_version"),
        "board_data_version": state.get("board_data_version"),
        "artifact_policies": state.get("artifact_policies"),
        "source_identity": state.get("source_identity"),
        "artifacts": state.get("artifacts"),
        "migrations": state.get("migrations"),
        "approved_plan_token": state.get("approved_plan_token"),
        "operations": [
            {
                "artifact_id": operation["artifact_id"],
                "policy": operation["policy"],
                "action": operation["action"],
                "target": operation["target"],
                "before_sha256": operation["before_sha256"],
                "after_sha256": operation["after_sha256"],
            }
            for operation in runtime_records
        ],
    }
    receipt_payload = canonical_json(receipt)
    state_payload = canonical_json(state)
    receipt_candidate_relative = (
        f".agents/project_management/setup/.runtime/tracker-transactions/{tx_id}/"
        "candidate/receipt.json"
    )
    state_candidate_relative = (
        f".agents/project_management/setup/.runtime/tracker-transactions/{tx_id}/"
        "candidate/state.json"
    )
    atomic_write(root, receipt_candidate_relative, receipt_payload)
    atomic_write(root, state_candidate_relative, state_payload)
    journal = {
        "journal_schema_version": 2,
        "transaction_id": tx_id,
        "operations": runtime_records,
        "applied": 0,
        "previous_state": base64.b64encode(previous_payload).decode() if previous_payload else None,
        "state": {
            "target": STATE_RELATIVE,
            "before_sha256": checksum(previous_payload) if previous_payload is not None else None,
            "after_sha256": checksum(state_payload),
            "candidate": state_candidate_relative,
        },
        "receipt": {
            "target": receipt_relative,
            "existed": previous_receipt is not None,
            "backup": receipt_backup_relative if previous_receipt is not None else None,
            "before_sha256": checksum(previous_receipt) if previous_receipt is not None else None,
            "after_sha256": checksum(receipt_payload),
            "candidate": receipt_candidate_relative,
        },
    }
    atomic_write(root, JOURNAL_RELATIVE, canonical_json(journal))
    if fault_injector:
        fault_injector("after_journal")
    try:
        for index, operation in enumerate(runtime_records, start=1):
            if operation["action"] == "delete":
                remove_file(root, operation["target"])
            else:
                candidate = safe_path(root, operation["candidate"], allow_missing=False)
                atomic_write(root, operation["target"], read_file(candidate))
            journal["applied"] = index
            atomic_write(root, JOURNAL_RELATIVE, canonical_json(journal))
            if fault_injector:
                fault_injector(f"after_operation:{index}")
        receipt_candidate = read_target(root, receipt_candidate_relative)
        if receipt_candidate is None or checksum(receipt_candidate) != checksum(receipt_payload):
            raise InstallConflict("Tracker receipt candidate changed during transaction")
        atomic_write(root, receipt_relative, receipt_candidate)
        if fault_injector:
            fault_injector("before_state")
        state_candidate = read_target(root, state_candidate_relative)
        if state_candidate is None or checksum(state_candidate) != checksum(state_payload):
            raise InstallConflict("Tracker state candidate changed during transaction")
        atomic_write(root, STATE_RELATIVE, state_candidate)
        if fault_injector:
            fault_injector("after_state")
            fault_injector("before_cleanup")
        remove_file(root, JOURNAL_RELATIVE)
        cleanup_transaction_files(root, journal)
        if fault_injector:
            fault_injector("after_cleanup")
    except Exception:
        if not fault_injector:
            recover_install_transaction(root)
        raise
    return tx_id


def recover_install_transaction(root: Path) -> RecoveryResult | None:
    path = safe_path(root, JOURNAL_RELATIVE)
    if not path.exists():
        return None
    try:
        journal = json.loads(read_file(path))
    except json.JSONDecodeError as exc:
        raise InstallSafetyError(f"Tracker install journal is corrupt: {exc}") from exc
    if (
        not isinstance(journal, dict)
        or set(journal)
        != {
            "applied",
            "journal_schema_version",
            "operations",
            "previous_state",
            "receipt",
            "state",
            "transaction_id",
        }
        or journal.get("journal_schema_version") != 2
    ):
        raise InstallSafetyError("Tracker install journal schema is invalid")
    transaction = journal.get("transaction_id")
    if not isinstance(transaction, str) or not re.fullmatch(r"tracker-[0-9a-f]{20}", transaction):
        raise InstallSafetyError("Tracker install journal transaction id is invalid")
    operations = journal.get("operations")
    receipt = journal.get("receipt")
    state = journal.get("state")
    if not isinstance(operations, list):
        raise InstallSafetyError("Tracker install journal operations are invalid")
    for index, operation in enumerate(operations, 1):
        if not isinstance(operation, dict) or set(operation) != {
            "action",
            "after_sha256",
            "artifact_id",
            "backup",
            "before_sha256",
            "candidate",
            "existed",
            "policy",
            "target",
        }:
            raise InstallSafetyError(
                f"Tracker install journal operation {index} fields are invalid"
            )
    if not isinstance(receipt, dict) or set(receipt) != {
        "after_sha256",
        "backup",
        "before_sha256",
        "candidate",
        "existed",
        "target",
    }:
        raise InstallSafetyError("Tracker install journal receipt fields are invalid")
    if not isinstance(state, dict) or set(state) != {
        "after_sha256",
        "before_sha256",
        "candidate",
        "target",
    }:
        raise InstallSafetyError("Tracker install journal state fields are invalid")
    applied = journal.get("applied")
    if not isinstance(applied, int) or isinstance(applied, bool) or not 0 <= applied <= len(operations):
        raise InstallSafetyError("Tracker install journal applied count is invalid")
    recovery_targets = [(f"operation {index}", operation) for index, operation in enumerate(operations, 1)]
    recovery_targets.extend((("receipt", receipt), ("state", state)))
    assert_recoverable_targets(root, recovery_targets)
    assert_recovery_material(root, journal)

    _, _, committed_state_checksum = parse_journal_target(state, label="state")
    if target_checksum(root, STATE_RELATIVE) == committed_state_checksum:
        for label, record in recovery_targets:
            target, _, after = parse_journal_target(record, label=label)
            if target_checksum(root, target) != after:
                raise InstallConflict(
                    "Tracker install recovery found committed state with an incomplete target: "
                    f"{target}"
                )
        cleanup_transaction_files(root, journal)
        remove_file(root, JOURNAL_RELATIVE)
        return RecoveryResult("completed", transaction)
    for operation in reversed(operations):
        target, _, _ = parse_journal_target(operation, label="operation")
        if operation.get("existed"):
            backup = safe_path(root, operation.get("backup"), allow_missing=False)
            atomic_write(root, target, read_file(backup))
        else:
            remove_file(root, target)
    receipt_target, _, _ = parse_journal_target(receipt, label="receipt")
    if receipt.get("existed"):
        receipt_backup = safe_path(root, receipt.get("backup"), allow_missing=False)
        atomic_write(root, receipt_target, read_file(receipt_backup))
    else:
        remove_file(root, receipt_target)
    previous = journal.get("previous_state")
    if previous is None:
        remove_file(root, STATE_RELATIVE)
    else:
        try:
            atomic_write(root, STATE_RELATIVE, base64.b64decode(previous, validate=True))
        except ValueError as exc:
            raise InstallSafetyError("Tracker install journal previous state is corrupt") from exc
    cleanup_transaction_files(root, journal)
    remove_file(root, JOURNAL_RELATIVE)
    return RecoveryResult("restored", transaction)

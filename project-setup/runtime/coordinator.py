#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

from update_core import (
    CoordinatorLock,
    FaultInjected,
    LockBusy,
    PlannedOperation,
    ReleaseManifest,
    UpdateConflict,
    UpdateError,
    UpdatePlan,
    ValidationError,
    approved_update_plan_token,
    apply_transaction,
    assert_no_symlink_components,
    assert_root_directory,
    canonical_json_bytes,
    current_file_payload,
    load_state,
    parse_project_relative_path,
    parse_release_manifest,
    plan_release_update,
    plan_to_json,
    read_json_object,
    read_regular_file,
    recover_interrupted_transaction,
    sha256_bytes,
    stage_release_candidates,
    transaction_id_for,
    validate_sha256,
    write_conflict_report,
)


EXIT_SUCCESS = 0
EXIT_CONFLICT = 1
EXIT_INVALID = 2
EXIT_LOCKED = 3
EXIT_RECOVERY = 4
EXIT_UNHEALTHY = 5
LOCK_PATH = ".agents/project_management/setup/.runtime/coordinator.lock"
JOURNAL_PATH = ".agents/project_management/setup/.runtime/journal.json"
TRACKER_STATE_PATH = ".agents/project_management/setup/tracker-install-state.json"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
SEMANTIC_VERSION_PATTERN = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class Coordinator:
    def __init__(
        self,
        root: Path,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.root = assert_root_directory(root, label="target root")
        self.fault_injector = fault_injector

    def status(self) -> tuple[int, dict[str, Any]]:
        if self._journal_exists():
            return EXIT_UNHEALTHY, {
                "command": "status",
                "status": "recovery_required",
                "journal": JOURNAL_PATH,
            }
        instruction_bundles, instruction_findings = self._instruction_status()
        tracker, tracker_findings = self._tracker_status()
        integration_findings = instruction_findings + tracker_findings
        state = load_state(self.root)
        if state is None:
            return (EXIT_CONFLICT if integration_findings else EXIT_SUCCESS), {
                "command": "status",
                "status": "modified" if integration_findings else "not_installed",
                "instruction_bundles": instruction_bundles,
                "tracker": tracker,
                "conflicts": integration_findings,
            }
        receipt_findings = self._receipt_findings(state)
        drift = (
            receipt_findings
            if receipt_findings
            else self._state_drift(state)
        ) + integration_findings
        if drift:
            return EXIT_CONFLICT, {
                "command": "status",
                "status": "modified",
                "conflicts": drift,
                "bundle_version": state.get("bundle_version"),
                "tracker": tracker,
            }
        return EXIT_SUCCESS, {
            "command": "status",
            "status": "ok",
            "bundle_version": state.get("bundle_version"),
            "component_versions": state.get("component_versions", {}),
            "manifest_schema_version": state.get("manifest_schema_version"),
            "source_identity": state.get("source_identity"),
            "last_successful_transaction": state.get("last_successful_transaction"),
            "instruction_bundles": instruction_bundles,
            "tracker": tracker,
        }

    def plan_update(
        self,
        source_root: Path,
        version: str | None = None,
        expected_manifest_sha256: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self._journal_exists():
            return EXIT_UNHEALTHY, {
                "command": "plan-update",
                "status": "recovery_required",
                "journal": JOURNAL_PATH,
            }
        version, expected_manifest_sha256 = self._resolve_release_request(
            source_root, version, expected_manifest_sha256
        )
        manifest, manifest_sha256 = self._load_verified_manifest(
            source_root, version, expected_manifest_sha256
        )
        state = load_state(self.root)
        receipt_findings = self._receipt_findings(state) if state is not None else []
        if receipt_findings:
            plan = self._receipt_blocked_plan(receipt_findings)
            return EXIT_CONFLICT, {
                "command": "plan-update",
                "status": "conflict",
                "bundle_version": manifest.bundle_version,
                "manifest_sha256": manifest_sha256,
                "plan_token": approved_update_plan_token(
                    self.root, source_root, manifest_sha256, manifest, plan
                ),
                **plan_to_json(plan),
            }
        self._reject_historical_downgrade(state, manifest.bundle_version)
        with tempfile.TemporaryDirectory(prefix="project-setup-plan-") as temporary:
            candidates = stage_release_candidates(source_root, manifest, Path(temporary))
            plan = self._with_instruction_conflicts(
                plan_release_update(self.root, manifest, candidates, state)
            )
            plan_token = approved_update_plan_token(
                self.root, source_root, manifest_sha256, manifest, plan
            )
        payload = {
            "command": "plan-update",
            "status": "conflict" if plan.conflicts else "ready",
            "bundle_version": manifest.bundle_version,
            "manifest_sha256": manifest_sha256,
            "plan_token": plan_token,
            **plan_to_json(plan),
        }
        return (EXIT_CONFLICT if plan.conflicts else EXIT_SUCCESS), payload

    def update(
        self,
        source_root: Path,
        version: str | None,
        expected_manifest_sha256: str | None,
        plan_token: str,
    ) -> tuple[int, dict[str, Any]]:
        if not re.fullmatch(r"setup-plan-[0-9a-f]{24}", plan_token):
            raise ValidationError("update requires an exact --plan-token from plan-update")
        version, expected_manifest_sha256 = self._resolve_release_request(
            source_root, version, expected_manifest_sha256
        )
        preflight_state = load_state(self.root)
        preflight_findings = (
            self._receipt_findings(preflight_state) if preflight_state is not None else []
        )
        if preflight_findings:
            manifest, manifest_sha256 = self._load_verified_manifest(
                source_root, version, expected_manifest_sha256
            )
            blocked_plan = self._receipt_blocked_plan(preflight_findings)
            return EXIT_CONFLICT, self._invalid_receipt_update_payload(
                blocked_plan,
                plan_token,
                approved_update_plan_token(
                    self.root,
                    source_root,
                    manifest_sha256,
                    manifest,
                    blocked_plan,
                ),
                None,
            )
        with CoordinatorLock(self.root, LOCK_PATH):
            with tempfile.TemporaryDirectory(prefix="project-setup-update-") as temporary:
                recovery = recover_interrupted_transaction(self.root)
                manifest, manifest_sha256 = self._load_verified_manifest(
                    source_root, version, expected_manifest_sha256
                )
                candidates = stage_release_candidates(source_root, manifest, Path(temporary))
                state = load_state(self.root)
                receipt_findings = self._receipt_findings(state) if state is not None else []
                if receipt_findings:
                    blocked_plan = self._receipt_blocked_plan(receipt_findings)
                    return EXIT_CONFLICT, self._invalid_receipt_update_payload(
                        blocked_plan,
                        plan_token,
                        approved_update_plan_token(
                            self.root,
                            source_root,
                            manifest_sha256,
                            manifest,
                            blocked_plan,
                        ),
                        recovery,
                    )
                self._reject_historical_downgrade(state, manifest.bundle_version)
                plan = self._with_instruction_conflicts(
                    plan_release_update(self.root, manifest, candidates, state)
                )
                recalculated_token = approved_update_plan_token(
                    self.root, source_root, manifest_sha256, manifest, plan
                )
                if plan.conflicts:
                    transaction_id = transaction_id_for(manifest_sha256, plan.operations, state)
                    report = write_conflict_report(
                        self.root,
                        transaction_id,
                        plan,
                        candidates,
                    )
                    return EXIT_CONFLICT, {
                        "command": "update",
                        "status": "conflict",
                        "transaction_id": transaction_id,
                        "plan_token": recalculated_token,
                        "approved_plan_token": plan_token,
                        "recovery": recovery,
                        "conflict_report": report,
                        **plan_to_json(plan),
                    }
                if recalculated_token != plan_token:
                    if (
                        recovery is not None
                        and recovery.startswith("completed:")
                        and self._is_current_release(
                            state, manifest, manifest_sha256, source_root, plan
                        )
                        and not self._receipt_findings(state or {})
                    ):
                        return EXIT_SUCCESS, {
                            "command": "update",
                            "status": "current",
                            "plan_token": plan_token,
                            "transaction_id": state.get("last_successful_transaction"),
                            "recovery": recovery,
                            **plan_to_json(plan),
                        }
                    return EXIT_CONFLICT, {
                        "command": "update",
                        "status": "conflict",
                        "plan_token": recalculated_token,
                        "approved_plan_token": plan_token,
                        "recovery": recovery,
                        "conflicts": [
                            {
                                "artifact_id": "approved-plan",
                                "target": ".agents/project_management/setup",
                                "reason": "target or release source changed after plan approval",
                            }
                        ],
                        "operations": plan_to_json(plan)["operations"],
                    }
                transaction_id = transaction_id_for(manifest_sha256, plan.operations, state)

                if self._is_current_release(state, manifest, manifest_sha256, source_root, plan):
                    receipt_findings = self._receipt_findings(state or {})
                    if receipt_findings:
                        return EXIT_CONFLICT, self._invalid_current_receipt_payload(
                            plan,
                            plan_token,
                            recovery,
                            receipt_findings,
                        )
                    return EXIT_SUCCESS, {
                        "command": "update",
                        "status": "current",
                        "transaction_id": state.get("last_successful_transaction"),
                        "plan_token": plan_token,
                        "recovery": recovery,
                        **plan_to_json(plan),
                    }

                new_state = self._build_state(
                    manifest,
                    manifest_sha256,
                    source_root,
                    plan,
                    state,
                    transaction_id,
                    plan_token,
                )
                receipt = self._build_receipt(
                    manifest,
                    manifest_sha256,
                    source_root,
                    plan,
                    state,
                    transaction_id,
                    plan_token,
                    kind="update",
                )
                apply_transaction(
                    self.root,
                    transaction_id,
                    plan.operations,
                    new_state,
                    receipt,
                    fault_injector=self.fault_injector,
                )
                return EXIT_SUCCESS, {
                    "command": "update",
                    "status": "updated",
                    "transaction_id": transaction_id,
                    "plan_token": plan_token,
                    "recovery": recovery,
                    **plan_to_json(plan),
                }

    def rollback(self) -> tuple[int, dict[str, Any]]:
        preflight_state = load_state(self.root)
        if preflight_state is not None:
            preflight_findings = self._receipt_findings(preflight_state)
            if preflight_findings:
                raise UpdateConflict(
                    "Rollback requires a canonical receipt matching the current durable state: "
                    + ", ".join(item["code"] for item in preflight_findings)
                )
        with CoordinatorLock(self.root, LOCK_PATH):
            recovery = recover_interrupted_transaction(self.root)
            state = load_state(self.root)
            if state is None:
                raise UpdateConflict("Rollback requires installed coordinator state")
            receipt_findings = self._receipt_findings(state)
            if receipt_findings:
                raise UpdateConflict(
                    "Rollback requires a canonical receipt matching the current durable state: "
                    + ", ".join(item["code"] for item in receipt_findings)
                )
            state_drift = self._state_drift(state)
            if state_drift:
                raise UpdateConflict(
                    "Rollback refused because installed artifacts changed after update: "
                    + ", ".join(item.get("path", item["code"]) for item in state_drift)
                )
            last_transaction = state.get("last_transaction")
            if not isinstance(last_transaction, dict) or not state.get("rollback_available"):
                raise UpdateConflict("No one-release rollback is available")
            operations_raw = last_transaction.get("operations")
            if not isinstance(operations_raw, list):
                raise ValidationError("Last transaction operations are corrupt")

            rollback_operations: list[PlannedOperation] = []
            for record in operations_raw:
                rollback_operations.append(self._plan_rollback_operation(record))
            previous_state = self._decode_previous_state(last_transaction)
            rolled_back_state = previous_state or {
                "state_schema_version": 1,
                "bundle_version": None,
                "component_versions": {},
                "tracker_data_schema_version": state.get("tracker_data_schema_version"),
                "board_data_version": state.get("board_data_version"),
                "manifest_schema_version": state.get("manifest_schema_version"),
                "source_identity": None,
                "artifacts": [],
                "migrations": [],
            }
            rollback_identity = sha256_bytes(
                canonical_json_bytes(
                    {
                        "kind": "rollback",
                        "transaction": last_transaction.get("transaction_id"),
                    }
                )
            )
            rollback_id = f"rollback-{rollback_identity[:20]}"
            receipt = {
                "receipt_schema_version": 1,
                "kind": "rollback",
                "transaction_id": rollback_id,
                "rolled_back_transaction": last_transaction.get("transaction_id"),
                "operations": [self._operation_record(item) for item in rollback_operations],
                "restored_state_sha256": sha256_bytes(canonical_json_bytes(rolled_back_state)),
            }
            apply_transaction(
                self.root,
                rollback_id,
                tuple(rollback_operations),
                rolled_back_state,
                receipt,
                fault_injector=self.fault_injector,
            )
            return EXIT_SUCCESS, {
                "command": "rollback",
                "status": "rolled_back",
                "transaction_id": rollback_id,
                "recovery": recovery,
                "operations": [self._operation_record(item) for item in rollback_operations],
            }

    def doctor(self) -> tuple[int, dict[str, Any]]:
        findings: list[dict[str, str]] = []
        try:
            if self._journal_exists():
                findings.append({"code": "interrupted_transaction", "path": JOURNAL_PATH})
            if CoordinatorLock.is_held(self.root, LOCK_PATH):
                findings.append({"code": "coordinator_locked", "path": LOCK_PATH})
        except ValidationError as exc:
            findings.append({"code": "unsafe_runtime_path", "detail": str(exc)})
        try:
            state = load_state(self.root)
        except ValidationError as exc:
            findings.append({"code": "corrupt_state", "detail": str(exc)})
            state = None
        if state is not None:
            receipt_findings = self._receipt_findings(state)
            findings.extend(receipt_findings)
            if not receipt_findings:
                findings.extend(self._state_drift(state))
        _, instruction_findings = self._instruction_status()
        findings.extend(instruction_findings)
        tracker, tracker_findings = self._tracker_status()
        findings.extend(tracker_findings)
        return (
            EXIT_UNHEALTHY if findings else EXIT_SUCCESS,
            {
                "command": "doctor",
                "status": "unhealthy" if findings else "healthy",
                "findings": findings,
                "tracker": tracker,
            },
        )

    def _load_verified_manifest(
        self,
        source_root: Path,
        version: str,
        expected_manifest_sha256: str,
    ) -> tuple[ReleaseManifest, str]:
        assert_root_directory(source_root, label="release source")
        if not VERSION_PATTERN.fullmatch(version) or "/" in version or ".." in version:
            raise ValidationError(f"Invalid release version: {version}")
        expected = validate_sha256(expected_manifest_sha256, label="manifest checksum")
        manifest_relative = parse_project_relative_path(
            f"releases/{version}/install-manifest.json",
            label="release manifest path",
        )
        manifest_path = assert_no_symlink_components(
            source_root,
            manifest_relative,
            allow_missing=False,
            require_final_file=True,
        )
        manifest_payload = read_regular_file(manifest_path)
        actual = sha256_bytes(manifest_payload)
        if actual != expected:
            raise ValidationError(
                f"Release manifest checksum mismatch: expected {expected}, got {actual}"
            )
        try:
            raw = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Release manifest is invalid JSON: {manifest_path}") from exc
        if not isinstance(raw, dict):
            raise ValidationError("Release manifest must contain an object")
        manifest = parse_release_manifest(raw)
        if manifest.bundle_version != version:
            raise ValidationError(
                f"Requested version {version} does not match manifest {manifest.bundle_version}"
            )
        self._assert_release_index_consistency(
            source_root, version, actual, manifest.raw
        )
        return manifest, actual

    def _resolve_release_request(
        self,
        source_root: Path,
        version: str | None,
        expected_manifest_sha256: str | None,
    ) -> tuple[str, str]:
        assert_root_directory(source_root, label="release source")
        if (version is None) != (expected_manifest_sha256 is None):
            raise ValidationError(
                "Provide both --version and --manifest-sha256, or omit both to use releases/index.json"
            )
        if version is not None and expected_manifest_sha256 is not None:
            return version, expected_manifest_sha256
        index = self._load_release_index(source_root, required=True)
        selected_version = index.get("stable_version") or index.get("current_source_version")
        if not isinstance(selected_version, str):
            raise ValidationError("Release index does not select a usable release version")
        releases = index.get("releases")
        matches = [
            release
            for release in releases
            if isinstance(release, dict) and release.get("version") == selected_version
        ]
        if len(matches) != 1:
            raise ValidationError("Release index must contain exactly one selected release entry")
        checksum = validate_sha256(
            matches[0].get("manifest_sha256"), label="release index manifest checksum"
        )
        return selected_version, checksum

    def _load_release_index(
        self, source_root: Path, *, required: bool
    ) -> dict[str, Any] | None:
        index_path = assert_no_symlink_components(
            source_root,
            "releases/index.json",
            allow_missing=not required,
            require_final_file=required,
        )
        if not index_path.exists():
            return None
        index = read_json_object(index_path, label="release index")
        if index.get("release_index_schema_version") != 1:
            raise ValidationError("Release index schema is invalid")
        if not isinstance(index.get("releases"), list):
            raise ValidationError("Release index releases must be an array")
        return index

    def _assert_release_index_consistency(
        self,
        source_root: Path,
        version: str,
        manifest_sha256: str,
        manifest: dict[str, Any],
    ) -> None:
        index = self._load_release_index(source_root, required=False)
        if index is None:
            return
        releases = index["releases"]
        matches = [
            release
            for release in releases
            if isinstance(release, dict) and release.get("version") == version
        ]
        if len(matches) != 1:
            raise ValidationError("Release index does not uniquely identify the requested version")
        release = matches[0]
        source_identity = manifest.get("source_identity")
        if not isinstance(source_identity, dict):
            raise ValidationError("Release manifest source identity is invalid")
        expected = {
            "version": manifest["bundle_version"],
            "status": manifest["release_status"],
            "published_tag": manifest.get("published_tag"),
            "immutable_commit": manifest.get("immutable_commit"),
            "source_checkout_commit": source_identity.get("source_checkout_commit"),
            "manifest_sha256": manifest_sha256,
            "manifest_path": f"releases/{version}/install-manifest.json",
        }
        drifted = [key for key, value in expected.items() if release.get(key) != value]
        if drifted:
            raise ValidationError(
                "Release index and manifest identity differ: " + ", ".join(sorted(drifted))
            )
        duplicated_identity = {
            "published_tag": source_identity.get("published_tag"),
            "immutable_commit": source_identity.get("immutable_commit"),
        }
        duplicated_drift = [
            key for key, value in duplicated_identity.items() if manifest.get(key) != value
        ]
        if duplicated_drift:
            raise ValidationError(
                "Release manifest duplicated identity fields differ: "
                + ", ".join(sorted(duplicated_drift))
            )
        if manifest["release_status"] == "stable" and index.get("stable_version") != version:
            raise ValidationError("Stable release index does not select the stable manifest version")

    def _build_state(
        self,
        manifest: ReleaseManifest,
        manifest_sha256: str,
        source_root: Path,
        plan: UpdatePlan,
        previous_state: dict[str, Any] | None,
        transaction_id: str,
        plan_token: str,
    ) -> dict[str, Any]:
        operations = [
            {
                **self._operation_record(operation),
                "backup": self._backup_record(transaction_id, operation),
            }
            for operation in plan.operations
        ]
        previous_payload = canonical_json_bytes(previous_state) if previous_state is not None else None
        state = {
            "state_schema_version": 1,
            "bundle_version": manifest.bundle_version,
            "component_versions": manifest.component_versions,
            "manifest_schema_version": manifest.manifest_schema_version,
            "source_identity": {
                "kind": "verified_local",
                "local_path": str(source_root),
                "manifest_sha256": manifest_sha256,
                "immutable_commit": manifest.raw.get("source_identity", {}).get("immutable_commit"),
                "published_tag": manifest.raw.get("source_identity", {}).get("published_tag"),
                "release_status": manifest.release_status,
                "source_checkout_commit": manifest.raw.get("source_identity", {}).get(
                    "source_checkout_commit"
                ),
            },
            "artifacts": list(plan.artifact_state),
            "migrations": list(manifest.migrations),
            "last_successful_transaction": transaction_id,
            "approved_plan_token": plan_token,
            "rollback_available": True,
            "last_transaction": {
                "transaction_id": transaction_id,
                "operations": operations,
                "previous_state_base64": base64.b64encode(previous_payload).decode("ascii")
                if previous_payload is not None
                else None,
            },
            "install_manifest": manifest.raw,
        }
        if manifest.tracker_data_schema_version is not None:
            state["tracker_data_schema_version"] = manifest.tracker_data_schema_version
            state["board_data_version"] = manifest.board_data_version
        return state

    def _reject_historical_downgrade(
        self,
        state: dict[str, Any] | None,
        candidate_version: str,
    ) -> None:
        if state is None or not isinstance(state.get("bundle_version"), str):
            return
        current_version = state["bundle_version"]
        if semantic_version_key(candidate_version) < semantic_version_key(current_version):
            raise ValidationError(
                f"Historical downgrade from {current_version} to {candidate_version} is not supported; "
                "use one-release rollback"
            )

    def _is_current_release(
        self,
        state: dict[str, Any] | None,
        manifest: ReleaseManifest,
        manifest_sha256: str,
        source_root: Path,
        plan: UpdatePlan,
    ) -> bool:
        if state is None or plan.operations:
            return False
        source_identity = state.get("source_identity")
        if not isinstance(source_identity, dict):
            return False
        expected_source_identity = {
            "kind": "verified_local",
            "local_path": str(source_root),
            "manifest_sha256": manifest_sha256,
            "immutable_commit": manifest.raw.get("source_identity", {}).get("immutable_commit"),
            "published_tag": manifest.raw.get("source_identity", {}).get("published_tag"),
            "release_status": manifest.release_status,
            "source_checkout_commit": manifest.raw.get("source_identity", {}).get(
                "source_checkout_commit"
            ),
        }
        expected = {
            "bundle_version": manifest.bundle_version,
            "component_versions": manifest.component_versions,
            "manifest_schema_version": manifest.manifest_schema_version,
            "source_identity": expected_source_identity,
            "artifacts": list(plan.artifact_state),
            "migrations": list(manifest.migrations),
            "install_manifest": manifest.raw,
        }
        if manifest.tracker_data_schema_version is not None:
            expected["tracker_data_schema_version"] = manifest.tracker_data_schema_version
            expected["board_data_version"] = manifest.board_data_version
        elif "tracker_data_schema_version" in state or "board_data_version" in state:
            return False
        return all(state.get(key) == value for key, value in expected.items())

    def _build_receipt(
        self,
        manifest: ReleaseManifest,
        manifest_sha256: str,
        source_root: Path,
        plan: UpdatePlan,
        previous_state: dict[str, Any] | None,
        transaction_id: str,
        plan_token: str,
        *,
        kind: str,
    ) -> dict[str, Any]:
        receipt = {
            "receipt_schema_version": 1,
            "kind": kind,
            "transaction_id": transaction_id,
            "approved_plan_token": plan_token,
            "source_identity": {
                "kind": "verified_local",
                "local_path": str(source_root),
                "manifest_sha256": manifest_sha256,
                "immutable_commit": manifest.raw.get("source_identity", {}).get("immutable_commit"),
                "published_tag": manifest.raw.get("source_identity", {}).get("published_tag"),
                "release_status": manifest.release_status,
                "source_checkout_commit": manifest.raw.get("source_identity", {}).get(
                    "source_checkout_commit"
                ),
            },
            "bundle_version": manifest.bundle_version,
            "component_versions": manifest.component_versions,
            "manifest_schema_version": manifest.manifest_schema_version,
            "migrations": list(manifest.migrations),
            "rollback": manifest.rollback,
            "artifacts": list(plan.artifact_state),
            "operations": [self._operation_record(item) for item in plan.operations],
            "previous_state_sha256": sha256_bytes(canonical_json_bytes(previous_state))
            if previous_state is not None
            else None,
            "previous_state_base64": base64.b64encode(canonical_json_bytes(previous_state)).decode("ascii")
            if previous_state is not None
            else None,
            "install_manifest": manifest.raw,
        }
        if manifest.tracker_data_schema_version is not None:
            receipt["tracker_data_schema_version"] = manifest.tracker_data_schema_version
            receipt["board_data_version"] = manifest.board_data_version
        return receipt

    def _backup_record(
        self,
        transaction_id: str,
        operation: PlannedOperation,
    ) -> dict[str, Any]:
        return {
            "target": operation.target,
            "existed": operation.before_sha256 is not None,
            "sha256": operation.before_sha256,
            "backup": (
                f".agents/project_management/setup/.runtime/transactions/{transaction_id}/"
                f"backup/{operation.target}"
                if operation.before_sha256 is not None
                else None
            ),
        }

    def _plan_rollback_operation(self, record: Any) -> PlannedOperation:
        if not isinstance(record, dict):
            raise ValidationError("Last transaction contains an invalid operation")
        target = parse_project_relative_path(record.get("target"), label="rollback target")
        expected_after = record.get("after_sha256")
        current_payload = current_file_payload(self.root, target)
        current_checksum = sha256_bytes(current_payload) if current_payload is not None else None
        if current_checksum != expected_after:
            raise UpdateConflict(f"Rollback refused because {target} changed after update")
        backup = record.get("backup")
        if not isinstance(backup, dict):
            raise ValidationError(f"Rollback backup metadata is missing for {target}")
        if backup.get("existed") is False:
            return PlannedOperation(
                artifact_id=str(record.get("artifact_id")),
                policy=str(record.get("policy")),
                action="delete",
                target=target,
                candidate_path=None,
                before_sha256=current_checksum,
                after_sha256=None,
            )
        backup_relative = parse_project_relative_path(backup.get("backup"), label="rollback backup")
        backup_path = assert_no_symlink_components(
            self.root,
            backup_relative,
            allow_missing=False,
            require_final_file=True,
        )
        backup_checksum = validate_sha256(backup.get("sha256"), label="rollback backup checksum")
        if sha256_bytes(read_regular_file(backup_path)) != backup_checksum:
            raise ValidationError(f"Rollback backup checksum mismatch for {target}")
        return PlannedOperation(
            artifact_id=str(record.get("artifact_id")),
            policy=str(record.get("policy")),
            action="write",
            target=target,
            candidate_path=backup_path,
            before_sha256=current_checksum,
            after_sha256=backup_checksum,
        )

    def _decode_previous_state(self, transaction: dict[str, Any]) -> dict[str, Any] | None:
        encoded = transaction.get("previous_state_base64")
        if encoded is None:
            return None
        if not isinstance(encoded, str):
            raise ValidationError("Rollback previous state is invalid")
        try:
            payload = base64.b64decode(encoded, validate=True)
            value = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Rollback previous state is corrupt") from exc
        if not isinstance(value, dict):
            raise ValidationError("Rollback previous state must be an object")
        return value

    def _operation_record(self, operation: PlannedOperation) -> dict[str, Any]:
        return {
            "artifact_id": operation.artifact_id,
            "policy": operation.policy,
            "action": operation.action,
            "target": operation.target,
            "before_sha256": operation.before_sha256,
            "after_sha256": operation.after_sha256,
        }

    def _state_drift(self, state: dict[str, Any]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for record in state.get("artifacts", []):
            target = record["target"]
            try:
                payload = current_file_payload(self.root, target)
            except ValidationError as exc:
                findings.append({"code": "unsafe_path", "path": target, "detail": str(exc)})
                continue
            actual = sha256_bytes(payload) if payload is not None else None
            if record.get("policy") == "managed_block":
                manifest_artifacts = state.get("install_manifest", {}).get("artifacts", [])
                source_record = next(
                    (item for item in manifest_artifacts if item.get("id") == record.get("id")),
                    None,
                )
                if payload is None or not isinstance(source_record, dict):
                    actual = None
                else:
                    try:
                        from update_core import ManagedBlock, managed_block_bytes

                        block_raw = source_record.get("block", {})
                        block = ManagedBlock(
                            start_marker=block_raw["start_marker"],
                            end_marker=block_raw["end_marker"],
                        )
                        actual = sha256_bytes(
                            managed_block_bytes(payload, block, label=f"State artifact {record['id']}")
                        )
                    except (KeyError, UpdateError):
                        actual = None
            if actual != record.get("installed_sha256"):
                findings.append(
                    {
                        "code": "artifact_modified",
                        "path": target,
                        "artifact_id": record["id"],
                    }
                )
        return findings

    def _instruction_status(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        try:
            from integrate_instructions import InstructionError, inspect_instruction_state
        except ImportError as exc:
            return [], [{"code": "instruction_state_invalid", "detail": str(exc)}]
        try:
            return inspect_instruction_state(self.root)
        except (InstructionError, UpdateError, OSError, ValueError) as exc:
            return [], [{"code": "instruction_state_invalid", "detail": str(exc)}]

    def _tracker_status(self) -> tuple[dict[str, Any], list[dict[str, str]]]:
        state_payload = current_file_payload(self.root, TRACKER_STATE_PATH)
        if state_payload is None:
            return {"status": "not_installed"}, []
        try:
            state = json.loads(state_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "invalid"}, [
                {"code": "tracker_state_invalid", "path": TRACKER_STATE_PATH, "detail": str(exc)}
            ]
        if not isinstance(state, dict) or state.get("component") != "task-tracking-setup":
            return {"status": "invalid"}, [
                {"code": "tracker_state_invalid", "path": TRACKER_STATE_PATH}
            ]
        summary = {
            "status": "ok",
            "component_version": state.get("component_version"),
            "tracker_data_schema_version": state.get("tracker_data_schema_version"),
            "board_data_version": state.get("board_data_version"),
        }
        try:
            expected = self._tracker_contract()
        except ValidationError as exc:
            summary["status"] = "invalid"
            return summary, [{"code": "tracker_contract_invalid", "detail": str(exc)}]
        obsolete = [key for key, value in expected.items() if state.get(key) != value]
        if obsolete:
            summary["status"] = "obsolete"
            return summary, [
                {
                    "code": "tracker_obsolete",
                    "path": TRACKER_STATE_PATH,
                    "detail": f"Unexpected tracker fields: {', '.join(obsolete)}",
                }
            ]
        artifacts = state.get("artifacts")
        if not isinstance(artifacts, list):
            summary["status"] = "invalid"
            return summary, [{"code": "tracker_state_invalid", "path": TRACKER_STATE_PATH}]
        findings = self._tracker_receipt_findings(state)
        if findings:
            summary["status"] = "modified"
            return summary, findings
        for record in artifacts:
            if not isinstance(record, dict):
                findings.append({"code": "tracker_state_invalid", "path": TRACKER_STATE_PATH})
                continue
            target = record.get("target")
            installed_sha256 = record.get("installed_sha256")
            if not isinstance(target, str) or not isinstance(installed_sha256, str):
                findings.append({"code": "tracker_state_invalid", "path": TRACKER_STATE_PATH})
                continue
            if record.get("policy") in {"seed", "user_data", "generated"}:
                continue
            payload = current_file_payload(self.root, target)
            actual = sha256_bytes(payload) if payload is not None else None
            if record.get("policy") == "managed_block" and payload is not None:
                start = payload.find(b"<!-- task-tracker:start -->")
                end_marker = b"<!-- task-tracker:end -->"
                end = payload.find(end_marker)
                actual = (
                    sha256_bytes(payload[start : end + len(end_marker)])
                    if start >= 0 and end >= start
                    else None
                )
            if actual != installed_sha256:
                findings.append(
                    {
                        "code": "tracker_artifact_modified",
                        "path": target,
                        "artifact_id": str(record.get("id", "unknown")),
                    }
                )
        if findings:
            summary["status"] = "modified"
        return summary, findings

    def _tracker_contract(self) -> dict[str, Any]:
        script = Path(__file__).resolve()
        candidates = (
            script.parent / "assets/tracker-integration.json",
            script.parent.parent / "assets/tracker-integration.json",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ValidationError("Tracker integration metadata is unavailable")
        integration = read_json_object(path, label="tracker integration metadata")
        expected = {
            "component_version": integration.get("required_component_version"),
            "tracker_data_schema_version": integration.get(
                "required_tracker_data_schema_version"
            ),
            "board_data_version": integration.get("required_board_data_version"),
        }
        if not isinstance(expected["component_version"], str):
            raise ValidationError("Tracker integration component version is invalid")
        if not all(isinstance(expected[key], int) for key in expected if key != "component_version"):
            raise ValidationError("Tracker integration schema versions are invalid")
        return expected

    def _tracker_receipt_findings(
        self, state: dict[str, Any]
    ) -> list[dict[str, str]]:
        transaction_id = state.get("last_successful_transaction")
        if not isinstance(transaction_id, str) or not transaction_id:
            return [{"code": "tracker_receipt_missing", "path": TRACKER_STATE_PATH}]
        receipt_relative = f".agents/project_management/setup/receipts/{transaction_id}.json"
        try:
            receipt = read_json_object(
                assert_no_symlink_components(
                    self.root,
                    receipt_relative,
                    allow_missing=False,
                    require_final_file=True,
                ),
                label="tracker receipt",
            )
        except ValidationError as exc:
            return [
                {
                    "code": "tracker_receipt_invalid",
                    "path": receipt_relative,
                    "detail": str(exc),
                }
            ]
        operations = receipt.get("operations")
        operation_fields = {
            "artifact_id",
            "policy",
            "action",
            "target",
            "before_sha256",
            "after_sha256",
        }
        if not isinstance(operations, list) or any(
            not isinstance(operation, dict) or set(operation) != operation_fields
            for operation in operations
        ):
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_relative}]
        expected = {
            "receipt_schema_version": 1,
            "transaction_id": transaction_id,
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
            "operations": operations,
        }
        durable_state = dict(state)
        durable_state.pop("approved_plan_token", None)
        durable_state.pop("last_successful_transaction", None)
        identity = {
            "version": state.get("component_version"),
            "operations": [
                {
                    "id": operation.get("artifact_id"),
                    "policy": operation.get("policy"),
                    "action": operation.get("action"),
                    "target": operation.get("target"),
                    "before": operation.get("before_sha256"),
                    "after": operation.get("after_sha256"),
                }
                for operation in operations
            ],
            "durable_state_sha256": sha256_bytes(canonical_json_bytes(durable_state)),
        }
        calculated_transaction = f"tracker-{sha256_bytes(canonical_json_bytes(identity))[:20]}"
        if receipt != expected or calculated_transaction != transaction_id:
            return [{"code": "tracker_receipt_state_mismatch", "path": receipt_relative}]
        return []

    def _with_instruction_conflicts(self, plan: UpdatePlan) -> UpdatePlan:
        _, findings = self._instruction_status()
        if not findings:
            return plan
        instruction_conflicts = tuple(
            {
                "artifact_id": finding.get("artifact_id", "instruction-state"),
                "target": finding.get(
                    "path", ".agents/project_management/setup/instruction-state.json"
                ),
                "reason": finding.get("detail", finding.get("code", "instruction drift")),
            }
            for finding in findings
        )
        conflicts = tuple(
            sorted(
                (*plan.conflicts, *instruction_conflicts),
                key=lambda item: (item["target"].casefold(), item["artifact_id"]),
            )
        )
        return UpdatePlan(
            operations=plan.operations,
            conflicts=conflicts,
            artifact_state=plan.artifact_state,
        )

    def _receipt_findings(self, state: dict[str, Any]) -> list[dict[str, str]]:
        transaction_id = state.get("last_successful_transaction")
        if not isinstance(transaction_id, str) or not transaction_id:
            return [{"code": "missing_transaction_identity"}]
        receipt_relative = f".agents/project_management/setup/receipts/{transaction_id}.json"
        try:
            receipt_path = assert_no_symlink_components(
                self.root,
                receipt_relative,
                allow_missing=False,
                require_final_file=True,
            )
            receipt = read_json_object(receipt_path, label="coordinator receipt")
        except ValidationError as exc:
            return [{"code": "invalid_receipt", "path": receipt_relative, "detail": str(exc)}]
        if receipt.get("kind") != "update":
            return [{"code": "receipt_kind_mismatch", "path": receipt_relative}]
        last_transaction = state.get("last_transaction")
        if not isinstance(last_transaction, dict):
            return [{"code": "state_transaction_missing", "path": receipt_relative}]
        if last_transaction.get("transaction_id") != transaction_id:
            return [{"code": "state_transaction_mismatch", "path": receipt_relative}]
        operations = last_transaction.get("operations")
        if not isinstance(operations, list):
            return [{"code": "state_transaction_invalid", "path": receipt_relative}]
        previous_state_base64 = last_transaction.get("previous_state_base64")
        try:
            previous_state = self._decode_previous_state(last_transaction)
        except ValidationError as exc:
            return [{"code": "state_transaction_invalid", "path": receipt_relative, "detail": str(exc)}]
        expected_receipt = {
            "receipt_schema_version": 1,
            "kind": "update",
            "transaction_id": transaction_id,
            "approved_plan_token": state.get("approved_plan_token"),
            "source_identity": state.get("source_identity"),
            "bundle_version": state.get("bundle_version"),
            "component_versions": state.get("component_versions"),
            "manifest_schema_version": state.get("manifest_schema_version"),
            "migrations": state.get("migrations"),
            "rollback": (state.get("install_manifest") or {}).get("rollback"),
            "artifacts": state.get("artifacts"),
            "operations": [
                {key: value for key, value in operation.items() if key != "backup"}
                if isinstance(operation, dict)
                else operation
                for operation in operations
            ],
            "previous_state_sha256": sha256_bytes(canonical_json_bytes(previous_state))
            if previous_state is not None
            else None,
            "previous_state_base64": previous_state_base64,
            "install_manifest": state.get("install_manifest"),
        }
        if "tracker_data_schema_version" in state:
            expected_receipt["tracker_data_schema_version"] = state.get(
                "tracker_data_schema_version"
            )
            expected_receipt["board_data_version"] = state.get("board_data_version")
        if receipt != expected_receipt:
            return [{"code": "receipt_state_mismatch", "path": receipt_relative}]
        return []

    def _invalid_current_receipt_payload(
        self,
        plan: UpdatePlan,
        plan_token: str,
        recovery: str | None,
        findings: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "command": "update",
            "status": "conflict",
            "plan_token": plan_token,
            "recovery": recovery,
            "conflicts": [
                {
                    "artifact_id": "coordinator-receipt",
                    "target": finding.get(
                        "path", ".agents/project_management/setup/receipts"
                    ),
                    "reason": (
                        "durable state does not have a canonical matching receipt; "
                        "receipt repair requires a separately approved state-only repair"
                    ),
                    "code": finding["code"],
                }
                for finding in findings
            ],
            "operations": plan_to_json(plan)["operations"],
        }

    def _receipt_blocked_plan(
        self, findings: list[dict[str, str]]
    ) -> UpdatePlan:
        conflicts = tuple(
            {
                "artifact_id": "coordinator-receipt",
                "target": finding.get(
                    "path", ".agents/project_management/setup/receipts"
                ),
                "reason": (
                    "durable state does not have a canonical matching receipt; "
                    "receipt repair requires a separately approved state-only repair"
                ),
                "code": finding["code"],
            }
            for finding in findings
        )
        return UpdatePlan(operations=(), conflicts=conflicts, artifact_state=())

    def _invalid_receipt_update_payload(
        self,
        plan: UpdatePlan,
        approved_plan_token: str,
        recalculated_plan_token: str,
        recovery: str | None,
    ) -> dict[str, Any]:
        return {
            "command": "update",
            "status": "conflict",
            "plan_token": recalculated_plan_token,
            "approved_plan_token": approved_plan_token,
            "recovery": recovery,
            **plan_to_json(plan),
        }

    def _journal_exists(self) -> bool:
        return assert_no_symlink_components(self.root, JOURNAL_PATH, allow_missing=True).exists()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-setup-coordinator",
        description="Deterministic offline project setup coordinator",
    )
    parser.add_argument("--root", required=True, help="Exact absolute target project root")
    parser.add_argument("--json", action="store_true", dest="as_json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("rollback")
    commands.add_parser("doctor")
    for command_name in ("plan-update", "update"):
        command = commands.add_parser(command_name)
        command.add_argument("--source", required=True, help="Verified absolute local release source")
        command.add_argument(
            "--version",
            help="Exact bundle version; omit with --manifest-sha256 to derive both from the local index",
        )
        command.add_argument(
            "--manifest-sha256",
            help="Expected SHA-256 of releases/<version>/install-manifest.json",
        )
        if command_name == "update":
            command.add_argument(
                "--plan-token",
                required=True,
                help="Exact canonical token returned by plan-update",
            )
    return parser


def semantic_version_key(version: str) -> tuple[Any, ...]:
    match = SEMANTIC_VERSION_PATTERN.fullmatch(version)
    if not match:
        raise ValidationError(f"Bundle version is not semantic-version compatible: {version}")
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        prerelease_key: tuple[Any, ...] = (1,)
    else:
        identifiers: list[tuple[int, Any]] = []
        for identifier in prerelease.split("."):
            identifiers.append((0, int(identifier)) if identifier.isdigit() else (1, identifier))
        prerelease_key = (0, *identifiers)
    return int(major), int(minor), int(patch), prerelease_key


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(f"{payload.get('command')}: {payload.get('status')}")
    for key in sorted(payload):
        if key in {"command", "status"}:
            continue
        print(f"{key}: {json.dumps(payload[key], sort_keys=True)}")


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = Path(args.root)
        coordinator = Coordinator(root)
        if args.command == "status":
            exit_code, payload = coordinator.status()
        elif args.command == "plan-update":
            exit_code, payload = coordinator.plan_update(
                Path(args.source), args.version, args.manifest_sha256
            )
        elif args.command == "update":
            exit_code, payload = coordinator.update(
                Path(args.source), args.version, args.manifest_sha256, args.plan_token
            )
        elif args.command == "rollback":
            exit_code, payload = coordinator.rollback()
        else:
            exit_code, payload = coordinator.doctor()
    except LockBusy as exc:
        exit_code = EXIT_LOCKED
        payload = {"command": args.command, "status": "locked", "error": str(exc)}
    except UpdateConflict as exc:
        exit_code = EXIT_CONFLICT
        payload = {"command": args.command, "status": "conflict", "error": str(exc)}
    except FaultInjected as exc:
        exit_code = EXIT_RECOVERY
        payload = {"command": args.command, "status": "interrupted", "error": str(exc)}
    except ValidationError as exc:
        exit_code = EXIT_INVALID
        payload = {"command": args.command, "status": "invalid", "error": str(exc)}
    except OSError as exc:
        exit_code = EXIT_RECOVERY
        payload = {"command": args.command, "status": "io_error", "error": str(exc)}
    emit(payload, as_json=args.as_json)
    return exit_code


if __name__ == "__main__":
    sys.exit(run())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

from update_core import (
    CoordinatorLock,
    LockBusy,
    ValidationError,
    assert_no_symlink_components,
    assert_root_directory,
    atomic_write_under_root,
    canonical_json_bytes,
    current_file_payload,
    read_regular_file,
    sha256_bytes,
    unlink_under_root,
    validate_sha256,
)


CATALOG_SCHEMA_VERSION = 1
CAPABILITY_SCHEMA_VERSION = 2
INSTRUCTION_STATE_SCHEMA_VERSION = 1
DESTINATIONS = ("AGENTS.md", "CLAUDE.md")
LOCK_PATH = ".agents/project_management/setup/.runtime/coordinator.lock"
STATE_PATH = ".agents/project_management/setup/instruction-state.json"
RECEIPT_ROOT = ".agents/project_management/setup/instruction-receipts"
CONFLICT_ROOT = ".agents/project_management/setup/.runtime/instructions/conflicts"
JOURNAL_PATH = ".agents/project_management/setup/.runtime/instruction-journal.json"
TRANSACTION_ROOT = ".agents/project_management/setup/.runtime/instruction-transactions"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
MANAGED_MARKER_PATTERN = re.compile(
    rb"<!--\s*((?:project-setup:[a-z][a-z0-9-]*)|task-tracker):(start|end)\s*-->"
)


class InstructionError(Exception):
    """Instruction catalog, selection, or target state is invalid."""


@dataclass(frozen=True)
class MarkerPair:
    start: str
    end: str


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    version: str
    purpose: str
    applicability: dict[str, tuple[str, ...]]
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    destinations: tuple[str, ...]
    markers: dict[str, MarkerPair]
    source_asset: Path
    source_sha256: str
    content: dict[str, str]


@dataclass(frozen=True)
class InstructionCatalog:
    version: str
    checksum: str
    bundles: tuple[Bundle, ...]


@dataclass(frozen=True)
class ManagedRange:
    bundle_id: str
    start: int
    end: int


@dataclass(frozen=True)
class DestinationPlan:
    destination: str
    current: bytes | None
    candidate: bytes | None
    action: str
    block_artifacts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class IntegrationPlan:
    mode: str
    catalog: InstructionCatalog
    destinations: tuple[str, ...]
    bundle_ids: tuple[str, ...]
    capabilities: tuple[dict[str, Any], ...]
    suggestions: tuple[dict[str, Any], ...]
    destination_plans: tuple[DestinationPlan, ...]
    conflicts: tuple[dict[str, str], ...]
    state: dict[str, Any] | None
    token: str
    capability_catalog_checksum: str
    bundle_source_checksums: tuple[tuple[str, str], ...]


def parse_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstructionError(f"{label} must be a non-empty string")
    return value


def parse_string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InstructionError(f"{label} must be an array")
    parsed = tuple(parse_string(item, f"{label} entry") for item in value)
    if len(parsed) != len(set(parsed)):
        raise InstructionError(f"{label} contains duplicates")
    return parsed


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_file(path).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InstructionError(f"{label} is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InstructionError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstructionError(f"{label} must contain an object: {path}")
    return value


def source_catalog_paths(script_path: Path) -> tuple[Path, Path]:
    installed_catalog = script_path.parent / "instructions/catalog.json"
    installed_capabilities = script_path.parent / "capabilities/catalog.yaml"
    if installed_catalog.is_file() and installed_capabilities.is_file():
        return installed_catalog, installed_capabilities
    package_root = script_path.parent.parent
    return (
        package_root / "assets/instructions/catalog.json",
        package_root / "assets/capabilities/catalog.yaml",
    )


def parse_markers(raw: Any, bundle_id: str, destinations: tuple[str, ...]) -> dict[str, MarkerPair]:
    if not isinstance(raw, dict) or set(raw) != set(destinations):
        raise InstructionError(f"Bundle {bundle_id} markers must cover every destination")
    parsed: dict[str, MarkerPair] = {}
    for destination in destinations:
        marker = raw[destination]
        if not isinstance(marker, dict):
            raise InstructionError(f"Bundle {bundle_id} marker for {destination} must be an object")
        start = parse_string(marker.get("start"), f"Bundle {bundle_id} start marker")
        end = parse_string(marker.get("end"), f"Bundle {bundle_id} end marker")
        start_match = MANAGED_MARKER_PATTERN.fullmatch(start.encode("utf-8"))
        end_match = MANAGED_MARKER_PATTERN.fullmatch(end.encode("utf-8"))
        if (
            start == end
            or start_match is None
            or end_match is None
            or start_match.group(2) != b"start"
            or end_match.group(2) != b"end"
            or start_match.group(1) != end_match.group(1)
        ):
            raise InstructionError(f"Bundle {bundle_id} has invalid bounded markers")
        parsed[destination] = MarkerPair(start=start, end=end)
    return parsed


def parse_bundle_asset(
    catalog_root: Path,
    raw: dict[str, Any],
) -> Bundle:
    bundle_id = parse_string(raw.get("id"), "Bundle id")
    if not ID_PATTERN.fullmatch(bundle_id):
        raise InstructionError(f"Bundle id is invalid: {bundle_id}")
    version = parse_string(raw.get("version"), f"Bundle {bundle_id} version")
    purpose = parse_string(raw.get("purpose"), f"Bundle {bundle_id} purpose")
    destinations = parse_string_array(raw.get("destinations"), f"Bundle {bundle_id} destinations")
    if not destinations or any(item not in DESTINATIONS for item in destinations):
        raise InstructionError(f"Bundle {bundle_id} has unsupported destinations")
    dependencies = parse_string_array(raw.get("dependencies"), f"Bundle {bundle_id} dependencies")
    conflicts = parse_string_array(raw.get("conflicts"), f"Bundle {bundle_id} conflicts")
    markers = parse_markers(raw.get("markers"), bundle_id, destinations)
    applicability_raw = raw.get("applicability")
    if not isinstance(applicability_raw, dict):
        raise InstructionError(f"Bundle {bundle_id} applicability must be an object")
    required_hints = {"path_exists_any", "selected_capabilities_any", "coverage_terms_any"}
    if set(applicability_raw) != required_hints:
        raise InstructionError(f"Bundle {bundle_id} applicability hints are incomplete")
    applicability = {
        key: parse_string_array(applicability_raw[key], f"Bundle {bundle_id} {key}")
        for key in sorted(required_hints)
    }
    source_relative = parse_string(raw.get("source_asset"), f"Bundle {bundle_id} source asset")
    if (
        Path(source_relative).is_absolute()
        or ".." in Path(source_relative).parts
        or "\\" in source_relative
    ):
        raise InstructionError(f"Bundle {bundle_id} source asset must be catalog-relative")
    source_asset = catalog_root / source_relative
    asset = read_json_object(source_asset, f"Bundle {bundle_id} asset")
    if asset.get("bundle_asset_schema_version") != 1:
        raise InstructionError(f"Bundle {bundle_id} asset schema is invalid")
    if asset.get("id") != bundle_id or asset.get("version") != version:
        raise InstructionError(f"Bundle {bundle_id} asset identity/version does not match catalog")
    content_raw = asset.get("content")
    if not isinstance(content_raw, dict) or set(content_raw) != set(destinations):
        raise InstructionError(f"Bundle {bundle_id} content must cover every destination")
    content = {
        destination: parse_string(content_raw[destination], f"Bundle {bundle_id} {destination} content")
        for destination in destinations
    }
    return Bundle(
        bundle_id=bundle_id,
        version=version,
        purpose=purpose,
        applicability=applicability,
        dependencies=dependencies,
        conflicts=conflicts,
        destinations=destinations,
        markers=markers,
        source_asset=source_asset,
        source_sha256=sha256_bytes(read_regular_file(source_asset)),
        content=content,
    )


def load_catalog(path: Path) -> InstructionCatalog:
    payload = read_regular_file(path)
    raw = read_json_object(path, "Instruction bundle catalog")
    if raw.get("catalog_schema_version") != CATALOG_SCHEMA_VERSION:
        raise InstructionError("Instruction bundle catalog schema is invalid")
    version = parse_string(raw.get("catalog_version"), "Instruction catalog version")
    bundles_raw = raw.get("bundles")
    if not isinstance(bundles_raw, list) or not bundles_raw:
        raise InstructionError("Instruction catalog bundles must be a non-empty array")
    bundles = tuple(parse_bundle_asset(path.parent, item) for item in bundles_raw if isinstance(item, dict))
    if len(bundles) != len(bundles_raw):
        raise InstructionError("Every instruction catalog bundle must be an object")
    by_id = {bundle.bundle_id: bundle for bundle in bundles}
    if len(by_id) != len(bundles):
        raise InstructionError("Instruction catalog has duplicate bundle ids")
    marker_owners: dict[tuple[str, str], str] = {}
    for bundle in bundles:
        for related in (*bundle.dependencies, *bundle.conflicts):
            if related not in by_id or related == bundle.bundle_id:
                raise InstructionError(f"Bundle {bundle.bundle_id} references invalid related bundle {related}")
        for destination, pair in bundle.markers.items():
            for marker in (pair.start, pair.end):
                owner = marker_owners.get((destination, marker))
                if owner:
                    raise InstructionError(
                        f"Bundles {owner} and {bundle.bundle_id} share a {destination} marker"
                    )
                marker_owners[(destination, marker)] = bundle.bundle_id
    return InstructionCatalog(version=version, checksum=sha256_bytes(payload), bundles=bundles)


def load_capabilities(path: Path, selected_ids: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    raw = read_json_object(path, "Capability catalog")
    if raw.get("schema_version") != CAPABILITY_SCHEMA_VERSION:
        raise InstructionError("Capability catalog schema is invalid")
    records = raw.get("capabilities")
    if not isinstance(records, list):
        raise InstructionError("Capability catalog capabilities must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise InstructionError("Every capability must be an object")
        capability_id = parse_string(record.get("id"), "Capability id")
        if capability_id in by_id:
            raise InstructionError(f"Duplicate capability id: {capability_id}")
        by_id[capability_id] = record
    selected: list[dict[str, Any]] = []
    for capability_id in selected_ids:
        record = by_id.get(capability_id)
        if record is None:
            raise InstructionError(f"Unknown selected capability: {capability_id}")
        for field in ("name", "scope", "source", "version", "reference"):
            parse_string(record.get(field), f"Capability {capability_id} {field}")
        selected.append(
            {
                "id": capability_id,
                "name": record["name"],
                "kind": record.get("kind"),
                "scope": record["scope"],
                "source": record["source"],
                "version": record["version"],
                "reference": record["reference"],
            }
        )
    return tuple(sorted(selected, key=lambda item: item["id"]))


def bundle_by_id(catalog: InstructionCatalog) -> dict[str, Bundle]:
    return {bundle.bundle_id: bundle for bundle in catalog.bundles}


def validate_selection(catalog: InstructionCatalog, bundle_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not bundle_ids:
        raise InstructionError("Manual mode requires at least one --bundle")
    if len(bundle_ids) != len(set(bundle_ids)):
        raise InstructionError("Bundle selection contains duplicates")
    bundles = bundle_by_id(catalog)
    unknown = sorted(set(bundle_ids) - set(bundles))
    if unknown:
        raise InstructionError(f"Unknown bundle selection: {', '.join(unknown)}")
    selected = set(bundle_ids)
    for bundle_id in bundle_ids:
        bundle = bundles[bundle_id]
        missing = sorted(set(bundle.dependencies) - selected)
        if missing:
            raise InstructionError(
                f"Bundle {bundle_id} requires selected dependencies: {', '.join(missing)}"
            )
        conflicts = sorted(set(bundle.conflicts) & selected)
        reverse_conflicts = sorted(
            item.bundle_id for item in catalog.bundles if bundle_id in item.conflicts and item.bundle_id in selected
        )
        combined = sorted(set(conflicts + reverse_conflicts))
        if combined:
            raise InstructionError(f"Bundle {bundle_id} conflicts with: {', '.join(combined)}")
    order = {bundle.bundle_id: index for index, bundle in enumerate(catalog.bundles)}
    return tuple(sorted(bundle_ids, key=order.__getitem__))


def render_capability_inventory(capabilities: tuple[dict[str, Any], ...]) -> str:
    if not capabilities:
        raise InstructionError(
            "tools-capability-inventory requires at least one explicit --capability"
        )
    return "\n".join(
        f"- **{item['name']}** (`{item['id']}`) — scope `{item['scope']}`; "
        f"version `{item['version']}`; source `{item['source']}`; reference `{item['reference']}`."
        for item in capabilities
    )


def render_block(bundle: Bundle, destination: str, capabilities: tuple[dict[str, Any], ...]) -> bytes:
    content = bundle.content[destination]
    placeholder = "{{CAPABILITY_INVENTORY}}"
    if bundle.bundle_id == "tools-capability-inventory":
        content = content.replace(placeholder, render_capability_inventory(capabilities))
    elif placeholder in content:
        raise InstructionError(f"Bundle {bundle.bundle_id} has an unsupported capability placeholder")
    pair = bundle.markers[destination]
    return f"{pair.start}\n{content.rstrip()}\n{pair.end}".encode("utf-8")


def marker_lookup(catalog: InstructionCatalog, destination: str) -> dict[bytes, tuple[str, str]]:
    lookup: dict[bytes, tuple[str, str]] = {}
    for bundle in catalog.bundles:
        if destination not in bundle.destinations:
            continue
        pair = bundle.markers[destination]
        lookup[pair.start.encode()] = (bundle.bundle_id, "start")
        lookup[pair.end.encode()] = (bundle.bundle_id, "end")
    return lookup


def parse_managed_ranges(
    payload: bytes,
    catalog: InstructionCatalog,
    destination: str,
) -> dict[str, ManagedRange]:
    lookup = marker_lookup(catalog, destination)
    seen: set[tuple[str, str]] = set()
    ranges: dict[str, ManagedRange] = {}
    opened: tuple[str, int] | None = None
    matches = list(MANAGED_MARKER_PATTERN.finditer(payload))
    managed_prefix_count = payload.count(b"<!-- project-setup:") + payload.count(
        b"<!-- task-tracker:"
    )
    if managed_prefix_count != len(matches):
        raise InstructionError(f"{destination} contains a malformed managed marker")
    for match in matches:
        marker = match.group(0)
        identity = lookup.get(marker)
        if identity is None:
            raise InstructionError(
                f"{destination} contains an unknown or destination-incompatible managed marker: "
                f"{marker.decode('utf-8', errors='replace')}"
            )
        bundle_id, kind = identity
        if identity in seen:
            raise InstructionError(f"{destination} contains duplicate {kind} marker for {bundle_id}")
        seen.add(identity)
        if kind == "start":
            if opened is not None:
                raise InstructionError(
                    f"{destination} contains nested managed blocks: {opened[0]} and {bundle_id}"
                )
            opened = (bundle_id, match.start())
            continue
        if opened is None:
            raise InstructionError(f"{destination} has an end marker before a start marker for {bundle_id}")
        if opened[0] != bundle_id:
            raise InstructionError(
                f"{destination} closes {bundle_id} while {opened[0]} is open"
            )
        ranges[bundle_id] = ManagedRange(bundle_id, opened[1], match.end())
        opened = None
    if opened is not None:
        raise InstructionError(f"{destination} is missing the end marker for {opened[0]}")
    return ranges


def append_block(payload: bytes, block: bytes) -> bytes:
    if not payload:
        return block + b"\n"
    if payload.endswith(b"\n\n"):
        separator = b""
    elif payload.endswith(b"\n"):
        separator = b"\n"
    else:
        separator = b"\n\n"
    return payload + separator + block + b"\n"


def compose_destination(
    current: bytes,
    ranges: dict[str, ManagedRange],
    selected: tuple[Bundle, ...],
    rendered_blocks: dict[str, bytes],
) -> bytes:
    replacements = {
        bundle.bundle_id: rendered_blocks[bundle.bundle_id]
        for bundle in selected
        if bundle.bundle_id in ranges
    }
    segments: list[bytes] = []
    cursor = 0
    for managed_range in sorted(ranges.values(), key=lambda item: item.start):
        if managed_range.bundle_id not in replacements:
            continue
        segments.append(current[cursor : managed_range.start])
        segments.append(replacements[managed_range.bundle_id])
        cursor = managed_range.end
    segments.append(current[cursor:])
    candidate = b"".join(segments)
    for bundle in selected:
        if bundle.bundle_id not in ranges:
            candidate = append_block(candidate, rendered_blocks[bundle.bundle_id])
    return candidate


def instruction_artifact_id(destination: str, bundle_id: str) -> str:
    return f"instruction:{destination.casefold()}:{bundle_id}"


def load_instruction_state(root: Path) -> dict[str, Any] | None:
    state_path = assert_no_symlink_components(root, STATE_PATH, allow_missing=True)
    if not state_path.exists():
        return None
    state = read_json_object(state_path, "Instruction integration state")
    if state.get("instruction_state_schema_version") != INSTRUCTION_STATE_SCHEMA_VERSION:
        raise InstructionError("Instruction state schema is unsupported")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, list):
        raise InstructionError("Instruction state artifacts must be an array")
    ids: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise InstructionError("Instruction state artifact must be an object")
        artifact_id = parse_string(artifact.get("id"), "Instruction state artifact id")
        if artifact_id in ids:
            raise InstructionError(f"Duplicate instruction state artifact: {artifact_id}")
        ids.add(artifact_id)
        if artifact.get("policy") != "managed_block":
            raise InstructionError(f"Instruction state artifact {artifact_id} has invalid policy")
        if artifact.get("destination") not in DESTINATIONS:
            raise InstructionError(f"Instruction state artifact {artifact_id} has invalid destination")
        validate_sha256(artifact.get("installed_sha256"), label=f"{artifact_id} installed checksum")
        validate_sha256(artifact.get("source_sha256"), label=f"{artifact_id} source checksum")
        markers = artifact.get("markers")
        if not isinstance(markers, dict) or set(markers) != {"start", "end"}:
            raise InstructionError(f"Instruction state artifact {artifact_id} has invalid markers")
        parse_string(markers["start"], f"Instruction state artifact {artifact_id} start marker")
        parse_string(markers["end"], f"Instruction state artifact {artifact_id} end marker")
    return state


def recorded_block_bytes(payload: bytes, artifact: dict[str, Any]) -> bytes:
    markers = artifact["markers"]
    start = markers["start"].encode("utf-8")
    end = markers["end"].encode("utf-8")
    if payload.count(start) != 1 or payload.count(end) != 1:
        raise InstructionError(
            f"Recorded block {artifact['bundle_id']} requires exactly one marker pair"
        )
    start_at = payload.index(start)
    end_at = payload.index(end)
    if end_at < start_at:
        raise InstructionError(f"Recorded block {artifact['bundle_id']} markers are reversed")
    return payload[start_at : end_at + len(end)]


def inspect_instruction_state(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    state = load_instruction_state(root)
    findings: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []
    payload_by_destination: dict[str, bytes | None] = {}
    if state is not None:
        findings.extend(instruction_receipt_findings(root, state))
        if findings:
            return summaries, findings
    dedicated_artifacts = (state or {}).get("artifacts", [])
    for artifact in dedicated_artifacts:
        destination = artifact["destination"]
        if destination not in payload_by_destination:
            payload_by_destination[destination] = current_file_payload(root, destination)
        payload = payload_by_destination[destination]
        try:
            actual = sha256_bytes(recorded_block_bytes(payload or b"", artifact))
        except InstructionError as exc:
            actual = None
            findings.append(
                {
                    "code": "instruction_block_invalid",
                    "path": destination,
                    "artifact_id": artifact["id"],
                    "detail": str(exc),
                }
            )
        if actual is not None and actual != artifact["installed_sha256"]:
            findings.append(
                {
                    "code": "instruction_block_modified",
                    "path": destination,
                    "artifact_id": artifact["id"],
                }
            )
        summaries.append(
            {
                "bundle_id": artifact["bundle_id"],
                "bundle_version": artifact["bundle_version"],
                "destination": destination,
                "policy": "managed_block",
            }
        )
    dedicated_destinations = {
        artifact["destination"]
        for artifact in dedicated_artifacts
        if artifact.get("bundle_id") == "task-tracking"
    }
    tracker_state_path = assert_no_symlink_components(
        root,
        ".agents/project_management/setup/tracker-install-state.json",
        allow_missing=True,
    )
    if tracker_state_path.exists():
        tracker_state = read_json_object(tracker_state_path, "Tracker install state")
        tracker_artifacts = tracker_state.get("artifacts", [])
        if not isinstance(tracker_artifacts, list):
            raise InstructionError("Tracker install state artifacts must be an array")
        for record in tracker_artifacts:
            if not isinstance(record, dict) or record.get("policy") != "managed_block":
                continue
            destination = record.get("target")
            if destination not in DESTINATIONS or destination in dedicated_destinations:
                continue
            normalized = {
                "id": f"tracker:{record.get('id')}",
                "bundle_id": "task-tracking",
                "bundle_version": record.get(
                    "component_version", tracker_state.get("component_version", "unknown")
                ),
                "destination": destination,
                "installed_sha256": record.get("installed_sha256"),
                "markers": {
                    "start": "<!-- task-tracker:start -->",
                    "end": "<!-- task-tracker:end -->",
                },
            }
            validate_sha256(
                normalized["installed_sha256"],
                label=f"Tracker instruction block {destination} checksum",
            )
            payload = current_file_payload(root, destination)
            try:
                actual = sha256_bytes(recorded_block_bytes(payload or b"", normalized))
            except InstructionError as exc:
                actual = None
                findings.append(
                    {
                        "code": "instruction_block_invalid",
                        "path": destination,
                        "artifact_id": normalized["id"],
                        "detail": str(exc),
                    }
                )
            if actual is not None and actual != normalized["installed_sha256"]:
                findings.append(
                    {
                        "code": "instruction_block_modified",
                        "path": destination,
                        "artifact_id": normalized["id"],
                    }
                )
            summaries.append(
                {
                    "bundle_id": "task-tracking",
                    "bundle_version": normalized["bundle_version"],
                    "destination": destination,
                    "policy": "managed_block",
                }
            )
        tracker_transaction = tracker_state.get("last_successful_transaction")
        if any(
            isinstance(record, dict) and record.get("policy") == "managed_block"
            for record in tracker_artifacts
        ):
            if not isinstance(tracker_transaction, str) or not tracker_transaction:
                findings.append({"code": "tracker_instruction_receipt_missing"})
            else:
                tracker_receipt_path = f".agents/project_management/setup/receipts/{tracker_transaction}.json"
                try:
                    tracker_receipt = read_json_object(
                        assert_no_symlink_components(
                            root,
                            tracker_receipt_path,
                            allow_missing=False,
                            require_final_file=True,
                        ),
                        "Tracker instruction receipt",
                    )
                    expected_tracker_receipt = {
                        "receipt_schema_version": 1,
                        "transaction_id": tracker_transaction,
                        "component": tracker_state.get("component"),
                        "component_version": tracker_state.get("component_version"),
                        "bundle_version": tracker_state.get("bundle_version"),
                        "manifest_schema_version": tracker_state.get("manifest_schema_version"),
                        "tracker_data_schema_version": tracker_state.get(
                            "tracker_data_schema_version"
                        ),
                        "board_data_version": tracker_state.get("board_data_version"),
                        "artifact_policies": tracker_state.get("artifact_policies"),
                        "source_identity": tracker_state.get("source_identity"),
                        "artifacts": tracker_state.get("artifacts"),
                        "migrations": tracker_state.get("migrations"),
                        "approved_plan_token": tracker_state.get("approved_plan_token"),
                        "operations": tracker_receipt.get("operations"),
                    }
                    if tracker_receipt != expected_tracker_receipt:
                        findings.append(
                            {
                                "code": "tracker_instruction_receipt_mismatch",
                                "path": tracker_receipt_path,
                            }
                        )
                except (InstructionError, ValidationError) as exc:
                    findings.append(
                        {
                            "code": "tracker_instruction_receipt_invalid",
                            "path": tracker_receipt_path,
                            "detail": str(exc),
                        }
                    )
    summaries.sort(key=lambda item: (item["destination"], item["bundle_id"]))
    return summaries, findings


def instruction_receipt_findings(
    root: Path, state: dict[str, Any]
) -> list[dict[str, str]]:
    transaction_id = state.get("last_successful_transaction")
    if not isinstance(transaction_id, str) or not transaction_id:
        return [{"code": "instruction_receipt_missing"}]
    receipt_path = f"{RECEIPT_ROOT}/{transaction_id}.json"
    try:
        receipt = read_json_object(
            assert_no_symlink_components(
                root, receipt_path, allow_missing=False, require_final_file=True
            ),
            "Instruction receipt",
        )
    except (InstructionError, ValidationError) as exc:
        return [
            {
                "code": "instruction_receipt_invalid",
                "path": receipt_path,
                "detail": str(exc),
            }
        ]
    last_transaction = state.get("last_transaction")
    if not isinstance(last_transaction, dict):
        return [{"code": "instruction_receipt_state_mismatch", "path": receipt_path}]
    expected = {
        "instruction_receipt_schema_version": 1,
        "transaction_id": transaction_id,
        "approved_plan_token": transaction_id,
        "catalog_version": state.get("catalog_version"),
        "catalog_sha256": state.get("catalog_sha256"),
        "capability_catalog_sha256": state.get("capability_catalog_sha256"),
        "bundle_source_sha256": state.get("bundle_source_sha256"),
        "bundle_ids": state.get("approved_bundle_ids"),
        "destinations": last_transaction.get("destinations"),
        "artifacts": state.get("artifacts"),
        "capabilities": state.get("capabilities"),
    }
    if last_transaction.get("transaction_id") != transaction_id or receipt != expected:
        return [{"code": "instruction_receipt_state_mismatch", "path": receipt_path}]
    return []


def selected_capability_ids(capabilities: tuple[dict[str, Any], ...]) -> set[str]:
    return {item["id"] for item in capabilities}


def bundle_evidence(
    root: Path,
    catalog: InstructionCatalog,
    bundle: Bundle,
    capabilities: tuple[dict[str, Any], ...],
    destination_payloads: dict[str, bytes],
) -> tuple[str, ...]:
    evidence: list[str] = []
    for relative in bundle.applicability["path_exists_any"]:
        path = assert_no_symlink_components(root, relative, allow_missing=True)
        if path.exists():
            evidence.append(f"path:{relative}")
    capability_hints = bundle.applicability["selected_capabilities_any"]
    selected_ids = selected_capability_ids(capabilities)
    if "*" in capability_hints and selected_ids:
        evidence.append("selected-capabilities")
    for capability_id in sorted(selected_ids & set(capability_hints)):
        evidence.append(f"capability:{capability_id}")
    for destination, payload in destination_payloads.items():
        ranges = parse_managed_ranges(payload, catalog, destination)
        if bundle.bundle_id in ranges:
            evidence.append(f"managed:{destination}")
    return tuple(evidence)


def destination_has_coverage(
    payload: bytes,
    catalog: InstructionCatalog,
    bundle: Bundle,
    destination: str,
) -> bool:
    ranges = parse_managed_ranges(payload, catalog, destination)
    if bundle.bundle_id in ranges:
        return True
    try:
        text = payload.decode("utf-8").casefold()
    except UnicodeDecodeError as exc:
        raise InstructionError(f"{destination} must be UTF-8") from exc
    return any(term.casefold() in text for term in bundle.applicability["coverage_terms_any"])


def auto_suggestions(
    root: Path,
    catalog: InstructionCatalog,
    destinations: tuple[str, ...],
    capabilities: tuple[dict[str, Any], ...],
    payloads: dict[str, bytes],
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    suggestions: dict[str, dict[str, Any]] = {}
    for bundle in catalog.bundles:
        evidence = bundle_evidence(root, catalog, bundle, capabilities, payloads)
        if not evidence:
            continue
        uncovered = [
            destination
            for destination in destinations
            if not destination_has_coverage(
                payloads[destination], catalog, bundle, destination
            )
        ]
        if not uncovered:
            continue
        suggestions[bundle.bundle_id] = {
            "bundle_id": bundle.bundle_id,
            "evidence": list(evidence),
            "uncovered_destinations": uncovered,
        }
    by_id = bundle_by_id(catalog)
    pending = list(suggestions)
    while pending:
        bundle_id = pending.pop()
        for dependency in by_id[bundle_id].dependencies:
            if dependency in suggestions:
                continue
            suggestions[dependency] = {
                "bundle_id": dependency,
                "evidence": [f"dependency:{bundle_id}"],
                "uncovered_destinations": list(destinations),
            }
            pending.append(dependency)
    order = {bundle.bundle_id: index for index, bundle in enumerate(catalog.bundles)}
    bundle_ids = tuple(sorted(suggestions, key=order.__getitem__))
    return bundle_ids, tuple(suggestions[bundle_id] for bundle_id in bundle_ids)


def build_plan(
    root: Path,
    catalog: InstructionCatalog,
    capability_catalog_path: Path,
    destinations: tuple[str, ...],
    requested_bundle_ids: tuple[str, ...],
    selected_capability_names: tuple[str, ...],
    *,
    auto: bool,
) -> IntegrationPlan:
    capabilities = load_capabilities(capability_catalog_path, selected_capability_names)
    payloads = {
        destination: current_file_payload(root, destination) or b""
        for destination in destinations
    }
    for destination, payload in payloads.items():
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InstructionError(f"{destination} must be UTF-8") from exc
    if auto:
        bundle_ids, suggestions = auto_suggestions(
            root, catalog, destinations, capabilities, payloads
        )
        if bundle_ids:
            bundle_ids = validate_selection(catalog, bundle_ids)
    else:
        bundle_ids = validate_selection(catalog, requested_bundle_ids)
        suggestions = ()
    selected = tuple(bundle_by_id(catalog)[bundle_id] for bundle_id in bundle_ids)
    suggestion_destinations = {
        item["bundle_id"]: set(item["uncovered_destinations"])
        for item in suggestions
    }
    if any(destination not in bundle.destinations for bundle in selected for destination in destinations):
        raise InstructionError("A selected bundle does not support every selected destination")
    if "tools-capability-inventory" in bundle_ids and not capabilities:
        raise InstructionError(
            "tools-capability-inventory requires at least one explicit --capability"
        )
    state = load_instruction_state(root)
    receipt_findings = instruction_receipt_findings(root, state) if state is not None else []
    if receipt_findings:
        capability_catalog_checksum = sha256_bytes(
            read_regular_file(capability_catalog_path)
        )
        bundle_source_checksums = tuple(
            sorted(
                (bundle.bundle_id, bundle.source_sha256)
                for bundle in catalog.bundles
            )
        )
        conflicts = tuple(
            {
                "code": finding["code"],
                "destination": finding.get("path", RECEIPT_ROOT),
                "reason": (
                    "instruction durable state does not have a canonical matching receipt; "
                    "receipt repair requires a separately approved state-only repair"
                ),
            }
            for finding in receipt_findings
        )
        destination_plans = tuple(
            DestinationPlan(
                destination,
                current_file_payload(root, destination),
                current_file_payload(root, destination),
                "unchanged",
                (),
            )
            for destination in destinations
        )
        token_identity = {
            "target_root": str(root.resolve(strict=True)),
            "catalog_sha256": catalog.checksum,
            "capability_catalog_sha256": capability_catalog_checksum,
            "bundle_source_sha256": dict(bundle_source_checksums),
            "bundle_ids": list(bundle_ids),
            "capability_ids": [item["id"] for item in capabilities],
            "receipt_conflicts": list(conflicts),
            "destinations": [
                {
                    "path": item.destination,
                    "current_sha256": sha256_bytes(item.current)
                    if item.current is not None
                    else None,
                }
                for item in destination_plans
            ],
        }
        return IntegrationPlan(
            mode="auto" if auto else "manual",
            catalog=catalog,
            destinations=destinations,
            bundle_ids=bundle_ids,
            capabilities=capabilities,
            suggestions=suggestions,
            destination_plans=destination_plans,
            conflicts=conflicts,
            state=None,
            token=f"instruction-{sha256_bytes(canonical_json_bytes(token_identity))[:20]}",
            capability_catalog_checksum=capability_catalog_checksum,
            bundle_source_checksums=bundle_source_checksums,
        )
    previous_artifacts = {
        item["id"]: item for item in (state or {}).get("artifacts", [])
    }
    destination_plans: list[DestinationPlan] = []
    conflicts: list[dict[str, str]] = []
    next_artifacts: list[dict[str, Any]] = []
    for destination in destinations:
        destination_selected = (
            tuple(
                bundle
                for bundle in selected
                if destination in suggestion_destinations[bundle.bundle_id]
            )
            if auto
            else selected
        )
        current_or_empty = payloads[destination]
        current = current_file_payload(root, destination)
        if not destination_selected:
            destination_plans.append(
                DestinationPlan(destination, current, current, "unchanged", ())
            )
            continue
        try:
            ranges = parse_managed_ranges(current_or_empty, catalog, destination)
        except InstructionError as exc:
            conflicts.append({"destination": destination, "reason": str(exc)})
            destination_plans.append(
                DestinationPlan(destination, current, None, "conflict", ())
            )
            continue
        rendered = {
            bundle.bundle_id: render_block(bundle, destination, capabilities)
            for bundle in destination_selected
        }
        block_artifacts: list[dict[str, Any]] = []
        destination_conflicted = False
        for bundle in destination_selected:
            artifact_id = instruction_artifact_id(destination, bundle.bundle_id)
            previous = previous_artifacts.get(artifact_id)
            managed_range = ranges.get(bundle.bundle_id)
            current_block = (
                current_or_empty[managed_range.start : managed_range.end]
                if managed_range
                else None
            )
            candidate_block = rendered[bundle.bundle_id]
            if previous is not None and current_block is None:
                conflicts.append(
                    {
                        "destination": destination,
                        "bundle_id": bundle.bundle_id,
                        "reason": "recorded managed block is missing",
                    }
                )
                destination_conflicted = True
                continue
            if previous is not None:
                current_checksum = sha256_bytes(current_block or b"")
                if current_checksum != previous.get("installed_sha256"):
                    conflicts.append(
                        {
                            "destination": destination,
                            "bundle_id": bundle.bundle_id,
                            "reason": "managed block was customized after installation",
                        }
                    )
                    destination_conflicted = True
                    continue
            elif current_block is not None and sha256_bytes(current_block) != sha256_bytes(candidate_block):
                conflicts.append(
                    {
                        "destination": destination,
                        "bundle_id": bundle.bundle_id,
                        "reason": "existing managed block has no recognized ownership baseline",
                    }
                )
                destination_conflicted = True
                continue
            pair = bundle.markers[destination]
            block_artifacts.append(
                {
                    "id": artifact_id,
                    "bundle_id": bundle.bundle_id,
                    "bundle_version": bundle.version,
                    "destination": destination,
                    "target": destination,
                    "policy": "managed_block",
                    "markers": {"start": pair.start, "end": pair.end},
                    "installed_sha256": sha256_bytes(candidate_block),
                    "source_sha256": bundle.source_sha256,
                    "rendered_sha256": sha256_bytes(candidate_block),
                }
            )
        if destination_conflicted:
            candidate = compose_destination(
                current_or_empty, ranges, destination_selected, rendered
            )
            destination_plans.append(
                DestinationPlan(
                    destination,
                    current,
                    candidate,
                    "conflict",
                    tuple(block_artifacts),
                )
            )
            continue
        candidate = compose_destination(
            current_or_empty, ranges, destination_selected, rendered
        )
        action = "create" if current is None else ("update" if candidate != current else "unchanged")
        destination_plans.append(
            DestinationPlan(destination, current, candidate, action, tuple(block_artifacts))
        )
        next_artifacts.extend(block_artifacts)
    selected_artifact_ids = {
        instruction_artifact_id(destination, bundle_id)
        for destination in destinations
        for bundle_id in bundle_ids
        if not auto or destination in suggestion_destinations[bundle_id]
    }
    for artifact in (state or {}).get("artifacts", []):
        if artifact["id"] not in selected_artifact_ids:
            next_artifacts.append(artifact)
    next_artifacts.sort(key=lambda item: item["id"])
    token_payload = {
        "target_root": str(root.resolve(strict=True)),
        "catalog_sha256": catalog.checksum,
        "capability_catalog_sha256": sha256_bytes(read_regular_file(capability_catalog_path)),
        "bundle_source_sha256": {
            bundle.bundle_id: bundle.source_sha256 for bundle in catalog.bundles
        },
        "destinations": [
            {
                "path": plan.destination,
                "action": plan.action,
                "current_sha256": sha256_bytes(plan.current) if plan.current is not None else None,
                "candidate_sha256": sha256_bytes(plan.candidate) if plan.candidate is not None else None,
            }
            for plan in destination_plans
        ],
        "bundle_ids": list(bundle_ids),
        "capability_ids": [item["id"] for item in capabilities],
    }
    token = f"instruction-{sha256_bytes(canonical_json_bytes(token_payload))[:20]}"
    persisted_capabilities = (
        list(capabilities)
        if "tools-capability-inventory" in bundle_ids
        else list((state or {}).get("capabilities", []))
    )
    next_state = {
        "instruction_state_schema_version": INSTRUCTION_STATE_SCHEMA_VERSION,
        "catalog_version": catalog.version,
        "catalog_sha256": catalog.checksum,
        "capability_catalog_sha256": token_payload["capability_catalog_sha256"],
        "bundle_source_sha256": token_payload["bundle_source_sha256"],
        "approved_bundle_ids": list(bundle_ids),
        "artifacts": next_artifacts,
        "capabilities": persisted_capabilities,
        "last_successful_transaction": (state or {}).get("last_successful_transaction"),
        "last_transaction": (state or {}).get("last_transaction"),
    }
    return IntegrationPlan(
        mode="auto" if auto else "manual",
        catalog=catalog,
        destinations=destinations,
        bundle_ids=bundle_ids,
        capabilities=capabilities,
        suggestions=suggestions,
        destination_plans=tuple(destination_plans),
        conflicts=tuple(conflicts),
        state=next_state,
        token=token,
        capability_catalog_checksum=token_payload["capability_catalog_sha256"],
        bundle_source_checksums=tuple(
            sorted(token_payload["bundle_source_sha256"].items())
        ),
    )


def plan_payload(plan: IntegrationPlan, *, action: str, dry_run: bool) -> dict[str, Any]:
    return {
        "action": action,
        "status": "conflict" if plan.conflicts else ("suggestions" if plan.mode == "auto" else "ready"),
        "dry_run": dry_run,
        "mode": plan.mode,
        "plan_token": plan.token,
        "catalog_version": plan.catalog.version,
        "catalog_sha256": plan.catalog.checksum,
        "capability_catalog_sha256": plan.capability_catalog_checksum,
        "bundle_source_sha256": dict(plan.bundle_source_checksums),
        "bundle_ids": list(plan.bundle_ids),
        "capabilities": list(plan.capabilities),
        "suggestions": list(plan.suggestions),
        "conflicts": list(plan.conflicts),
        "destinations": [
            {
                "path": item.destination,
                "action": item.action,
                "before_sha256": sha256_bytes(item.current) if item.current is not None else None,
                "after_sha256": sha256_bytes(item.candidate) if item.candidate is not None else None,
                "candidate_content": item.candidate.decode("utf-8") if item.candidate is not None else None,
                "managed_blocks": list(item.block_artifacts),
            }
            for item in plan.destination_plans
        ],
    }


def write_conflict_report(root: Path, plan: IntegrationPlan) -> str:
    report_root = f"{CONFLICT_ROOT}/{plan.token}"
    payload = plan_payload(plan, action="apply", dry_run=False)
    for item in plan.destination_plans:
        if item.candidate is not None:
            atomic_write_under_root(
                root,
                f"{report_root}/candidate/{item.destination}",
                item.candidate,
            )
    selected = bundle_by_id(plan.catalog)
    for destination in plan.destinations:
        for bundle_id in plan.bundle_ids:
            block = render_block(selected[bundle_id], destination, plan.capabilities)
            atomic_write_under_root(
                root,
                f"{report_root}/candidate-blocks/{destination}/{bundle_id}.md",
                block + b"\n",
            )
    report_path = f"{report_root}/report.json"
    atomic_write_under_root(root, report_path, canonical_json_bytes(payload))
    return report_path


def restore_payload(root: Path, destination: str, payload: bytes | None) -> None:
    if payload is None:
        unlink_under_root(root, destination)
        return
    atomic_write_under_root(root, destination, payload)


def transaction_backup_path(transaction_id: str, index: int) -> str:
    return f"{TRANSACTION_ROOT}/{transaction_id}/backups/{index}.bin"


def transaction_candidate_path(transaction_id: str, index: int) -> str:
    return f"{TRANSACTION_ROOT}/{transaction_id}/candidates/{index}.bin"


def instruction_target_checksum(root: Path, relative: str) -> str | None:
    payload = current_file_payload(root, relative)
    return sha256_bytes(payload) if payload is not None else None


def remove_empty_transaction_dirs(root: Path, transaction_id: str) -> None:
    for relative in (
        f"{TRANSACTION_ROOT}/{transaction_id}/backups",
        f"{TRANSACTION_ROOT}/{transaction_id}/candidates",
        f"{TRANSACTION_ROOT}/{transaction_id}",
    ):
        path = assert_no_symlink_components(root, relative, allow_missing=True)
        if path.exists():
            try:
                path.rmdir()
            except OSError:
                pass


def cleanup_instruction_transaction(
    root: Path, transaction_id: str, entries: list[dict[str, Any]]
) -> None:
    for entry in entries:
        for key in ("backup_path", "candidate_path"):
            relative = entry.get(key)
            if isinstance(relative, str):
                unlink_under_root(root, relative)
    remove_empty_transaction_dirs(root, transaction_id)


def recover_instruction_transaction(root: Path) -> str | None:
    journal_payload = current_file_payload(root, JOURNAL_PATH)
    if journal_payload is None:
        return None
    try:
        journal = json.loads(journal_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstructionError("Instruction transaction journal is invalid") from exc
    if (
        not isinstance(journal, dict)
        or set(journal) != {"entries", "schema_version", "transaction_id"}
        or journal.get("schema_version") != 2
    ):
        raise InstructionError("Instruction transaction journal schema is invalid")
    transaction_id = parse_string(journal.get("transaction_id"), "journal transaction_id")
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise InstructionError("Instruction transaction journal entries are invalid")
    if not re.fullmatch(r"instruction-[0-9a-f]{20}", transaction_id):
        raise InstructionError("Instruction transaction journal id is invalid")
    entries: list[dict[str, Any]] = []
    allowed_destinations = {
        *DESTINATIONS,
        STATE_PATH,
        f"{RECEIPT_ROOT}/{transaction_id}.json",
    }
    seen_destinations: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise InstructionError(f"Instruction journal entry {index} is invalid")
        if set(raw_entry) != {
            "after_sha256",
            "backup_path",
            "before_sha256",
            "candidate_path",
            "destination",
            "existed",
        }:
            raise InstructionError(f"Instruction journal entry {index} fields are invalid")
        destination = parse_string(
            raw_entry.get("destination"), f"journal entry {index} destination"
        )
        if destination not in allowed_destinations:
            raise InstructionError(f"Instruction journal destination is invalid: {destination}")
        if destination in seen_destinations:
            raise InstructionError(f"Instruction journal duplicates destination: {destination}")
        seen_destinations.add(destination)
        existed = raw_entry.get("existed")
        if not isinstance(existed, bool):
            raise InstructionError(f"Instruction journal entry {index} existed must be boolean")
        backup_path = raw_entry.get("backup_path")
        expected_backup = transaction_backup_path(transaction_id, index)
        if existed and backup_path != expected_backup:
            raise InstructionError(f"Instruction journal backup path is invalid: {backup_path}")
        if not existed and backup_path is not None:
            raise InstructionError(f"Instruction journal backup must be null: {destination}")
        before_sha256 = raw_entry.get("before_sha256")
        if existed:
            validate_sha256(before_sha256, label=f"journal entry {index} before_sha256")
        elif before_sha256 is not None:
            raise InstructionError(f"Instruction journal before checksum must be null: {destination}")
        validate_sha256(
            raw_entry.get("after_sha256"),
            label=f"journal entry {index} after_sha256",
        )
        candidate_path = raw_entry.get("candidate_path")
        expected_candidate = transaction_candidate_path(transaction_id, index)
        if candidate_path != expected_candidate:
            raise InstructionError(
                f"Instruction journal candidate path is invalid: {candidate_path}"
            )
        entries.append(raw_entry)

    expected_final_destinations = [
        f"{RECEIPT_ROOT}/{transaction_id}.json",
        STATE_PATH,
    ]
    if [entry["destination"] for entry in entries[-2:]] != expected_final_destinations:
        raise InstructionError("Instruction journal receipt/state ordering is invalid")

    drifted: list[str] = []
    for entry in entries:
        destination = entry["destination"]
        current_checksum = instruction_target_checksum(root, destination)
        if current_checksum not in {entry.get("before_sha256"), entry.get("after_sha256")}:
            drifted.append(destination)

        candidate = current_file_payload(root, entry["candidate_path"])
        if candidate is None or sha256_bytes(candidate) != entry["after_sha256"]:
            raise InstructionError(
                f"Instruction recovery candidate is missing or corrupt: {entry['candidate_path']}; "
                "journal, backups, and targets were preserved"
            )
        if entry["existed"]:
            backup = current_file_payload(root, entry["backup_path"])
            if backup is None or sha256_bytes(backup) != entry["before_sha256"]:
                raise InstructionError(
                    f"Instruction recovery backup is missing or corrupt: {entry['backup_path']}; "
                    "journal, backups, and targets were preserved"
                )
    if drifted:
        raise InstructionError(
            "Instruction recovery aborted because targets contain post-crash edits; "
            "no files were changed. Reconcile while preserving the journal and transaction materials: "
            + ", ".join(sorted(drifted))
        )
    if all(
        instruction_target_checksum(root, entry["destination"]) == entry["after_sha256"]
        for entry in entries
    ):
        unlink_under_root(root, JOURNAL_PATH)
        cleanup_instruction_transaction(root, transaction_id, entries)
        return "completed"
    for index, entry in reversed(list(enumerate(entries))):
        destination = parse_string(entry.get("destination"), f"journal entry {index} destination")
        existed = entry.get("existed")
        if not isinstance(existed, bool):
            raise InstructionError(f"Instruction journal entry {index} existed must be boolean")
        if not existed:
            unlink_under_root(root, destination)
            continue
        backup_path = parse_string(entry.get("backup_path"), f"journal entry {index} backup_path")
        backup = current_file_payload(root, backup_path)
        assert backup is not None
        atomic_write_under_root(root, destination, backup)
    unlink_under_root(root, JOURNAL_PATH)
    cleanup_instruction_transaction(root, transaction_id, entries)
    return "rolled_back"


def apply_plan(
    root: Path,
    plan: IntegrationPlan,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    if plan.conflicts:
        if any(
            str(item.get("code", "")).startswith("instruction_receipt_")
            for item in plan.conflicts
        ):
            return "conflict", None
        return "conflict", write_conflict_report(root, plan)
    if plan.state is None:
        raise InstructionError("Instruction plan has no durable state")
    previous_state_payload = current_file_payload(root, STATE_PATH)
    next_state = dict(plan.state)
    changed_destinations = [
        item for item in plan.destination_plans if item.action in {"create", "update"}
    ]
    state_without_transaction = dict(next_state)
    current_state = load_instruction_state(root)
    if not changed_destinations and current_state == state_without_transaction:
        receipt_findings = instruction_receipt_findings(root, current_state)
        if receipt_findings:
            raise InstructionError(
                "Instruction durable state does not have a canonical matching receipt; "
                "receipt repair requires a separately approved state-only repair"
            )
        return "current", None
    next_state["last_successful_transaction"] = plan.token
    destination_records = [
        {
            "path": item.destination,
            "action": item.action,
            "before_sha256": sha256_bytes(item.current) if item.current is not None else None,
            "after_sha256": sha256_bytes(item.candidate) if item.candidate is not None else None,
        }
        for item in plan.destination_plans
    ]
    next_state["last_transaction"] = {
        "transaction_id": plan.token,
        "destinations": destination_records,
    }
    receipt_path = f"{RECEIPT_ROOT}/{plan.token}.json"
    receipt_payload = {
        "instruction_receipt_schema_version": 1,
        "transaction_id": plan.token,
        "approved_plan_token": plan.token,
        "catalog_version": plan.catalog.version,
        "catalog_sha256": plan.catalog.checksum,
        "capability_catalog_sha256": plan.capability_catalog_checksum,
        "bundle_source_sha256": dict(plan.bundle_source_checksums),
        "bundle_ids": list(plan.bundle_ids),
        "destinations": destination_records,
        "artifacts": next_state["artifacts"],
        "capabilities": next_state["capabilities"],
    }
    receipt_before = current_file_payload(root, receipt_path)
    writes = [
        (item.destination, item.current, item.candidate)
        for item in changed_destinations
        if item.candidate is not None
    ] + [
        (receipt_path, receipt_before, canonical_json_bytes(receipt_payload)),
        (STATE_PATH, previous_state_payload, canonical_json_bytes(next_state)),
    ]
    for destination, before, _after in writes:
        current = current_file_payload(root, destination)
        if current != before:
            raise InstructionError(f"Instruction target changed after planning: {destination}")
    entries: list[dict[str, Any]] = []
    for index, (destination, before, after) in enumerate(writes):
        backup_path = transaction_backup_path(plan.token, index)
        candidate_path = transaction_candidate_path(plan.token, index)
        if before is not None:
            atomic_write_under_root(root, backup_path, before)
        atomic_write_under_root(root, candidate_path, after)
        entries.append(
            {
                "destination": destination,
                "existed": before is not None,
                "before_sha256": sha256_bytes(before) if before is not None else None,
                "after_sha256": sha256_bytes(after),
                "backup_path": backup_path if before is not None else None,
                "candidate_path": candidate_path,
            }
        )
    journal = {
        "schema_version": 2,
        "transaction_id": plan.token,
        "entries": entries,
    }
    atomic_write_under_root(root, JOURNAL_PATH, canonical_json_bytes(journal))
    if fault_injector is not None:
        fault_injector("after_journal")
    try:
        for index, (destination, _before, _after) in enumerate(writes):
            candidate = current_file_payload(root, entries[index]["candidate_path"])
            if candidate is None or sha256_bytes(candidate) != entries[index]["after_sha256"]:
                raise InstructionError(
                    f"Instruction candidate changed during transaction: {destination}"
                )
            atomic_write_under_root(root, destination, candidate)
            if fault_injector is not None:
                fault_injector(f"after_write_{index}")
    except Exception:
        recover_instruction_transaction(root)
        raise
    unlink_under_root(root, JOURNAL_PATH)
    cleanup_instruction_transaction(root, plan.token, entries)
    return "applied", receipt_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply deterministic managed instruction bundles"
    )
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--root", required=True, help="Exact absolute target project root")
    parser.add_argument(
        "--destination",
        action="append",
        choices=DESTINATIONS,
        required=True,
        help="Explicit approved instruction destination; repeat for both",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bundle", action="append", dest="bundles")
    mode.add_argument("--auto", action="store_true")
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Explicit selected/installed capability id for inventory rendering",
    )
    parser.add_argument("--dry-run", action="store_true", help="Read-only plan alias")
    parser.add_argument("--plan-token", help="Exact token returned by the approved plan")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(f"instruction-integration: {payload['status']}")
    print(f"plan token: {payload.get('plan_token', '-')}")
    print(f"bundles: {', '.join(payload.get('bundle_ids', [])) or 'none'}")
    for item in payload.get("destinations", []):
        print(f"{item['action']}: {item['path']}")
        if item.get("candidate_content") is not None:
            print(item["candidate_content"])
    for conflict in payload.get("conflicts", []):
        print(f"conflict: {conflict}")


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.action == "apply" and args.auto:
            raise InstructionError(
                "--auto is preview-only; apply the suggested set with explicit --bundle options"
            )
        if args.action == "apply" and args.dry_run:
            raise InstructionError("apply cannot be combined with --dry-run; use the plan action")
        if args.action == "apply" and not args.plan_token:
            raise InstructionError("apply requires --plan-token from an approved plan")
        if args.action == "plan" and args.plan_token:
            raise InstructionError("--plan-token is only valid with apply")
        root = assert_root_directory(Path(args.root), label="target root")
        destinations = tuple(dict.fromkeys(args.destination))
        if len(destinations) != len(args.destination):
            raise InstructionError("Destination selection contains duplicates")
        selected_capabilities = tuple(dict.fromkeys(args.capability))
        if len(selected_capabilities) != len(args.capability):
            raise InstructionError("Capability selection contains duplicates")
        script = Path(__file__).resolve()
        catalog_path, capability_catalog_path = source_catalog_paths(script)
        requested = tuple(args.bundles or ())
        if args.action == "plan" or args.dry_run:
            catalog = load_catalog(catalog_path)
            plan = build_plan(
                root,
                catalog,
                capability_catalog_path,
                destinations,
                requested,
                selected_capabilities,
                auto=args.auto,
            )
            payload = plan_payload(plan, action="plan", dry_run=True)
            emit(payload, args.as_json)
            return 1 if plan.conflicts else 0
        preflight_state = load_instruction_state(root)
        if preflight_state is not None and instruction_receipt_findings(
            root, preflight_state
        ):
            catalog = load_catalog(catalog_path)
            plan = build_plan(
                root,
                catalog,
                capability_catalog_path,
                destinations,
                requested,
                selected_capabilities,
                auto=False,
            )
            payload = plan_payload(plan, action="apply", dry_run=False)
            payload["status"] = "conflict"
            payload["recovery"] = None
            emit(payload, args.as_json)
            return 1
        with CoordinatorLock(root, LOCK_PATH):
            recovery = recover_instruction_transaction(root)
            catalog = load_catalog(catalog_path)
            plan = build_plan(
                root,
                catalog,
                capability_catalog_path,
                destinations,
                requested,
                selected_capabilities,
                auto=False,
            )
            if plan.conflicts:
                status, report = apply_plan(root, plan)
                payload = plan_payload(plan, action="apply", dry_run=False)
                payload["status"] = status
                payload["recovery"] = recovery
                payload["conflict_report"] = report
                emit(payload, args.as_json)
                return 1
            if plan.token != args.plan_token:
                recovered_state = load_instruction_state(root)
                if (
                    recovery == "completed"
                    and recovered_state is not None
                    and recovered_state == plan.state
                    and not inspect_instruction_state(root)[1]
                ):
                    payload = plan_payload(plan, action="apply", dry_run=False)
                    payload["status"] = "current"
                    payload["recovery"] = recovery
                    payload["receipt"] = f"{RECEIPT_ROOT}/{args.plan_token}.json"
                    emit(payload, args.as_json)
                    return 0
                payload = plan_payload(plan, action="apply", dry_run=False)
                payload["status"] = "conflict"
                payload["conflicts"] = [
                    {
                        "reason": "target or instruction source changed after the approved plan"
                    }
                ]
                payload["recovery"] = recovery
                emit(payload, args.as_json)
                return 1
            status, report_or_receipt = apply_plan(root, plan)
        payload = plan_payload(plan, action="apply", dry_run=False)
        payload["status"] = status
        payload["recovery"] = recovery
        if status == "conflict":
            payload["conflict_report"] = report_or_receipt
        elif report_or_receipt:
            payload["receipt"] = report_or_receipt
        emit(payload, args.as_json)
        return 1 if status == "conflict" else 0
    except LockBusy as exc:
        payload = {"action": args.action, "status": "locked", "error": str(exc)}
        emit(payload, args.as_json)
        return 3
    except (InstructionError, ValidationError, OSError) as exc:
        payload = {"action": args.action, "status": "invalid", "error": str(exc)}
        emit(payload, args.as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())

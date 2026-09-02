#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True


class VendoredTrackerError(Exception):
    """The packaged tracker integration is incomplete or unsafe."""


def parse_package_relative(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VendoredTrackerError(f"{label} must be a non-empty package-relative path")
    path = PurePosixPath(value)
    if value.startswith("/") or any(part in {"", ".", ".."} for part in path.parts):
        raise VendoredTrackerError(f"{label} is unsafe: {value!r}")
    return value


def read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VendoredTrackerError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VendoredTrackerError(f"{label} must contain a JSON object: {path}")
    return value


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_package_release_identity(package_root: Path) -> dict[str, Any]:
    version_path = package_root / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise VendoredTrackerError(f"Package VERSION is unreadable: {exc}") from exc
    index = read_json_object(package_root / "releases/index.json", label="Release index")
    if index.get("release_index_schema_version") != 1:
        raise VendoredTrackerError("Release index schema is invalid")
    selected = index.get("stable_version") or index.get("current_source_version")
    if selected != version:
        raise VendoredTrackerError("Release index and package VERSION differ")
    releases = index.get("releases")
    matches = [
        item
        for item in releases
        if isinstance(item, dict) and item.get("version") == version
    ] if isinstance(releases, list) else []
    if len(matches) != 1:
        raise VendoredTrackerError("Release index must contain one selected release")
    release = matches[0]
    info = read_json_object(
        package_root / f"releases/{version}/release-info.json",
        label="Release information",
    )
    expected = {
        "version": version,
        "release_status": release.get("status"),
        "published_tag": release.get("published_tag"),
        "immutable_commit": release.get("immutable_commit"),
        "source_checkout_commit": release.get("source_checkout_commit"),
    }
    info_values = {
        "version": info.get("bundle_version"),
        "release_status": info.get("release_status"),
        "published_tag": info.get("published_tag"),
        "immutable_commit": info.get("immutable_commit"),
        "source_checkout_commit": info.get("source_checkout_commit"),
    }
    if info_values != expected:
        raise VendoredTrackerError("Release index and release information identity differ")
    if expected["release_status"] == "stable":
        commit = expected["immutable_commit"]
        tag = expected["published_tag"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise VendoredTrackerError("Stable package commit identity is invalid")
        if expected["source_checkout_commit"] != commit:
            raise VendoredTrackerError("Stable source checkout identity does not match commit")
        if not isinstance(tag, str) or not tag or "PLACEHOLDER" in tag.upper():
            raise VendoredTrackerError("Stable package tag identity is invalid")
    elif expected != {
        "version": version,
        "release_status": "unreleased",
        "published_tag": None,
        "immutable_commit": None,
        "source_checkout_commit": None,
    }:
        raise VendoredTrackerError("Unreleased package identity is invalid")
    return expected


def validate_archive_member(member: zipfile.ZipInfo, expected_root: str) -> None:
    name = member.filename
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != expected_root
    ):
        raise VendoredTrackerError(f"Unsafe tracker archive member: {name!r}")
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise VendoredTrackerError(f"Symlinked tracker archive member is not allowed: {name}")


def load_integration(package_root: Path) -> tuple[Path, str, str]:
    package_identity = load_package_release_identity(package_root)
    integration_path = package_root / "assets/tracker-integration.json"
    integration = read_json_object(integration_path, label="Tracker integration metadata")
    expected_fields = {
        "automatic_install",
        "capability_id",
        "installation_policy",
        "installer_entrypoint",
        "integration_schema_version",
        "immutable_commit",
        "published_tag",
        "release_status",
        "required_board_data_version",
        "required_component_version",
        "required_tracker_data_schema_version",
        "skill_entrypoint_in_archive",
        "source_checkout_commit",
        "vendored_archive",
        "vendored_archive_sha256",
    }
    if set(integration) != expected_fields or integration.get("integration_schema_version") != 2:
        raise VendoredTrackerError("Tracker integration metadata schema is invalid")
    if integration.get("capability_id") != "task-tracking-setup":
        raise VendoredTrackerError("Tracker integration capability id is invalid")
    expected_contract = {
        "automatic_install": False,
        "installation_policy": "checksum_verified_vendored_archive",
        "installer_entrypoint": "runtime/install_tracker.py",
        "release_status": package_identity["release_status"],
        "required_board_data_version": 1,
        "required_component_version": package_identity["version"],
        "required_tracker_data_schema_version": 4,
        "published_tag": package_identity["published_tag"],
        "immutable_commit": package_identity["immutable_commit"],
        "source_checkout_commit": package_identity["source_checkout_commit"],
    }
    drifted = [
        key for key, expected in expected_contract.items() if integration.get(key) != expected
    ]
    if drifted:
        raise VendoredTrackerError(
            f"Tracker integration contract drifted: {', '.join(sorted(drifted))}"
        )
    archive_relative = parse_package_relative(
        integration.get("vendored_archive"), label="Vendored archive path"
    )
    archive_checksum = integration.get("vendored_archive_sha256")
    entrypoint = parse_package_relative(
        integration.get("skill_entrypoint_in_archive"),
        label="Vendored tracker skill entrypoint",
    )
    if not isinstance(archive_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", archive_checksum):
        raise VendoredTrackerError("Vendored tracker archive checksum is invalid")
    archive_path = package_root / archive_relative
    if archive_path.is_symlink() or not archive_path.is_file():
        raise VendoredTrackerError(f"Vendored tracker archive is missing: {archive_path}")
    actual_checksum = checksum(archive_path)
    if actual_checksum != archive_checksum:
        raise VendoredTrackerError(
            f"Vendored tracker checksum mismatch: expected {archive_checksum}, got {actual_checksum}"
        )
    return archive_path, entrypoint, archive_checksum


def extracted_tracker_root(package_root: Path, temporary_root: Path) -> tuple[Path, str]:
    archive_path, entrypoint, archive_checksum = load_integration(package_root)
    expected_root = PurePosixPath(entrypoint).parts[0]
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members:
            raise VendoredTrackerError("Vendored tracker archive is empty")
        for member in members:
            validate_archive_member(member, expected_root)
        if entrypoint not in {member.filename for member in members}:
            raise VendoredTrackerError(f"Vendored tracker entrypoint is missing: {entrypoint}")
        archive.extractall(temporary_root)
    skill_root = temporary_root / expected_root
    installer = skill_root / "scripts/setup_project.py"
    if not installer.is_file() or not (temporary_root / entrypoint).is_file():
        raise VendoredTrackerError("Vendored tracker package is incomplete")
    return skill_root, archive_checksum


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the checksum-verified tracker vendored with project-setup"
    )
    parser.add_argument("--root", required=True, help="Exact absolute target project root")
    parser.add_argument(
        "--instruction-file",
        action="append",
        choices=("AGENTS.md", "CLAUDE.md"),
        default=[],
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plan-token")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parent.parent
    try:
        with tempfile.TemporaryDirectory(prefix="project-setup-tracker-") as temporary:
            skill_root, archive_checksum = extracted_tracker_root(package_root, Path(temporary))
            command = [
                sys.executable,
                str(skill_root / "scripts/setup_project.py"),
                "--root",
                args.root,
                "--archive-sha256",
                archive_checksum,
            ]
            for destination in args.instruction_file:
                command.extend(("--instruction-file", destination))
            if args.dry_run:
                command.append("--dry-run")
            if args.plan_token:
                command.extend(("--plan-token", args.plan_token))
            if args.as_json:
                command.append("--json")
            completed = subprocess.run(command, check=False)
            return completed.returncode
    except VendoredTrackerError as exc:
        payload = {"command": "install", "status": "invalid", "error": str(exc)}
        if args.as_json:
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        else:
            print(f"tracker-install: invalid\nerror: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_MARKER = ".skill-eval-fixtures.json"
WORKSPACE_MARKER = ".skill-eval-workspace.json"
CONFIGURATIONS = ("with_skill", "without_skill")
SKILL_NAMES = ("project-setup", "task-tracking-setup")
EVAL_NAMES = {
    "project-setup": {
        1: "target-confirmation-first",
        2: "task-only-dual-instruction-preview",
        3: "unavailable-pin-refusal",
    },
    "task-tracking-setup": {
        1: "target-confirmation-first",
        2: "dual-instruction-dry-run",
        3: "existing-tracker-upgrade-preview",
    },
}
EXPECTED_CHANGED_PATHS: dict[tuple[str, int], list[str]] = {}
COORDINATOR_LOCK_PATH = ".agents/project_management/setup/.runtime/coordinator.lock"
EXPECTED_RUNTIME_PATHS: dict[tuple[str, int], list[str]] = {}
PERMITTED_RUNTIME_PATHS = frozenset({COORDINATOR_LOCK_PATH})
RUNTIME_PATH_PREFIXES = (
    ".agents/project_management/setup/.runtime/",
    ".agents/project_management/tasks/setup/.runtime/",
)
EXPECTED_OUTPUT_NAMES = {
    ("project-setup", 2): ["transcript.md", "preview.json"],
    ("task-tracking-setup", 2): ["transcript.md", "preview.json"],
}


@dataclass(frozen=True)
class EvalDefinition:
    skill_name: str
    eval_id: int
    eval_name: str
    prompt_template: str
    expected_output: str
    expectations: tuple[str, ...]

    @property
    def response_only(self) -> bool:
        return self.eval_id == 1


@dataclass(frozen=True)
class EvalRun:
    definition: EvalDefinition
    configuration: str
    fixture_path: Path
    eval_directory: Path

    @property
    def run_directory(self) -> Path:
        return self.eval_directory / self.configuration / "run-1"

    @property
    def output_path(self) -> Path:
        return self.run_directory / "outputs"

    @property
    def run_id(self) -> str:
        return (
            f"{self.definition.skill_name}:"
            f"{self.definition.eval_id}:{self.configuration}"
        )


def parse_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid JSON object at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Expected a JSON object at {path}")
    return value


def parse_evals(skill_name: str) -> tuple[EvalDefinition, ...]:
    path = REPOSITORY_ROOT / skill_name / "evals" / "evals.json"
    payload = parse_json_object(path)
    if set(payload) != {"skill_name", "evals"}:
        raise SystemExit(f"Unexpected top-level eval schema keys at {path}")
    if payload["skill_name"] != skill_name:
        raise SystemExit(f"Eval skill_name does not match {skill_name}: {path}")
    raw_evals = payload["evals"]
    if not isinstance(raw_evals, list) or len(raw_evals) != 3:
        raise SystemExit(f"{path} must contain exactly three evals")

    definitions: list[EvalDefinition] = []
    seen_ids: set[int] = set()
    expected_keys = {"id", "prompt", "expected_output", "files", "expectations"}
    for raw_eval in raw_evals:
        if not isinstance(raw_eval, dict) or set(raw_eval) != expected_keys:
            raise SystemExit(f"Eval entries in {path} must use {sorted(expected_keys)}")
        eval_id = raw_eval["id"]
        if not isinstance(eval_id, int) or isinstance(eval_id, bool) or eval_id in seen_ids:
            raise SystemExit(f"Eval ids in {path} must be unique integers")
        seen_ids.add(eval_id)
        files = raw_eval["files"]
        expectations = raw_eval["expectations"]
        string_fields = (raw_eval["prompt"], raw_eval["expected_output"])
        if any(not isinstance(value, str) or not value.strip() for value in string_fields):
            raise SystemExit(f"Eval {eval_id} in {path} has an empty text field")
        if not isinstance(files, list) or any(not isinstance(value, str) for value in files):
            raise SystemExit(f"Eval {eval_id} in {path} has invalid files")
        if not isinstance(expectations, list) or not expectations or any(
            not isinstance(value, str) or not value.strip() for value in expectations
        ):
            raise SystemExit(f"Eval {eval_id} in {path} has invalid expectations")
        definitions.append(
            EvalDefinition(
                skill_name=skill_name,
                eval_id=eval_id,
                eval_name=EVAL_NAMES[skill_name][eval_id],
                prompt_template=raw_eval["prompt"],
                expected_output=raw_eval["expected_output"],
                expectations=tuple(expectations),
            )
        )
    if seen_ids != {1, 2, 3}:
        raise SystemExit(f"{path} must use eval ids 1, 2, and 3")
    return tuple(definitions)


def validate_external_fixture_root(fixtures_root: Path) -> Path:
    if not fixtures_root.is_absolute():
        raise SystemExit("--fixtures-root must be an exact absolute path")
    resolved = fixtures_root.resolve()
    repository = REPOSITORY_ROOT.resolve()
    if resolved == repository or repository in resolved.parents:
        raise SystemExit("Fixture root must be outside the source repository")
    if resolved == resolved.parent or resolved == Path.home().resolve():
        raise SystemExit(f"Refusing unsafe fixture root: {resolved}")
    if not resolved.parent.is_dir():
        raise SystemExit(f"Fixture root parent must already exist: {resolved.parent}")
    return resolved


def reset_generated_directory(path: Path, marker_name: str, marker_value: dict[str, Any]) -> None:
    if path.exists():
        marker = path / marker_name
        if not marker.is_file() or parse_json_object(marker) != marker_value:
            raise SystemExit(f"Refusing to replace unmarked generated directory: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)
    write_json(path / marker_name, marker_value)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def create_confirmation_fixture(target: Path, skill_name: str) -> None:
    write_text(
        target / "PRIVATE_TARGET.txt",
        f"This {skill_name} confirmation fixture must not be inspected before confirmation.\n",
    )


def create_dual_instruction_fixture(target: Path, project_name: str) -> None:
    write_text(
        target / "AGENTS.md",
        "# Existing agent guidance\n\nKeep this unmanaged AGENTS content byte-for-byte.\n",
    )
    write_text(
        target / "CLAUDE.md",
        "# Existing Claude guidance\n\nKeep this unmanaged CLAUDE content byte-for-byte.\n",
    )
    write_json(
        target / "package.json",
        {
            "name": project_name,
            "private": True,
            "scripts": {"check": "node --check app.js"},
        },
    )
    write_text(target / "app.js", "export const fixture = true;\n")


def create_selected_skills_fixture(target: Path) -> None:
    write_text(target / "README.md", "# Capability pin safety fixture\n")
    write_json(target / "package.json", {"name": "pin-safety-fixture", "private": True})


def bug_0042() -> dict[str, Any]:
    return {
        "id": "BUG-0042",
        "type": "bug",
        "title": "OAuth callback fails in sandbox",
        "section": "Authentication",
        "status": "ready",
        "priority": "P1",
        "urgency": "high",
        "owner": "unassigned",
        "parent_id": "",
        "depends_on": [],
        "planning_docs": [],
        "tags": ["#oauth", "#sandbox"],
        "description": "Sandbox OAuth callback cannot exchange its authorization code.",
        "acceptance": "A sandbox login completes and records a verified callback.",
        "notes": "Reproduced against the sandbox tenant.",
        "reproduction": "Start sandbox login and complete provider consent.",
        "expected": "The callback exchanges the code and creates a session.",
        "actual": "The callback reports that sandbox credentials are missing.",
        "created_at": "2026-08-26T09:00:00Z",
        "updated_at": "2026-08-26T10:30:00Z",
        "completed_at": "",
        "triage_context": {
            "customer_tier": "internal-sandbox",
            "evidence_ids": ["AUTH-LOG-17"],
        },
    }


def copy_installed_tracker_files(target: Path) -> None:
    tracker_source = REPOSITORY_ROOT / "task-tracking-setup"
    tasks_root = target / ".agents" / "project_management" / "tasks"
    tracking_root = tasks_root / "task_tracking"
    setup_root = tasks_root / "setup"

    for name in ("backlog.json", "blocked.json", "in-progress.json", "ready.json"):
        source = tracker_source / "assets" / "project" / "tasks" / "task_tracking" / name
        destination = tracking_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(
        tracker_source
        / "assets"
        / "project"
        / "tasks"
        / "task_tracking"
        / "open_task_board.html",
        tracking_root / "open_task_board.html",
    )
    for name in ("README.md", "feature-plan.template.md"):
        source = tracker_source / "assets" / "project" / "tasks" / "ideation" / name
        destination = tasks_root / "ideation" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for name in ("task_store.py", "archive_tasks.py", "render_tasks.py", "install_transaction.py"):
        source = tracker_source / "scripts" / name
        destination = setup_root / "scripts" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for name in ("task_board.css", "task_board.js"):
        source = (
            tracker_source
            / "assets"
            / "project"
            / "tasks"
            / "setup"
            / "task_board"
            / name
        )
        destination = setup_root / "task_board" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    setup_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        tracker_source / "assets" / "project" / "tasks" / "setup" / "tracker.json",
        setup_root / "tracker.json",
    )


def create_installed_tracker_fixture(target: Path) -> None:
    copy_installed_tracker_files(target)
    tracking_root = target / ".agents" / "project_management" / "tasks" / "task_tracking"
    ready_path = tracking_root / "ready.json"
    blocked_path = tracking_root / "blocked.json"
    ready = parse_json_object(ready_path)
    blocked = parse_json_object(blocked_path)
    ready["fixture_envelope_metadata"] = {
        "preserve": True,
        "source": "ready-fixture",
    }
    blocked["fixture_envelope_metadata"] = {
        "preserve": True,
        "source": "blocked-fixture",
    }
    ready["tasks"] = [bug_0042()]
    write_json(ready_path, ready)
    write_json(blocked_path, blocked)
    render_command = [
        sys.executable,
        str(
            target
            / ".agents"
            / "project_management"
            / "tasks"
            / "setup"
            / "scripts"
            / "render_tasks.py"
        ),
    ]
    result = subprocess.run(render_command, cwd=target, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"Installed tracker fixture render failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )


def create_fixture(target: Path, skill_name: str, eval_id: int) -> None:
    target.mkdir(parents=True)
    if eval_id == 1:
        create_confirmation_fixture(target, skill_name)
        return
    if skill_name == "project-setup" and eval_id == 2:
        create_dual_instruction_fixture(target, "project-setup-task-only-fixture")
        return
    if skill_name == "project-setup" and eval_id == 3:
        create_selected_skills_fixture(target)
        return
    if skill_name == "task-tracking-setup" and eval_id == 2:
        create_dual_instruction_fixture(target, "tracker-dual-instruction-fixture")
        return
    if skill_name == "task-tracking-setup" and eval_id == 3:
        create_installed_tracker_fixture(target)
        return
    raise SystemExit(f"No fixture builder for {skill_name} eval {eval_id}")


def filesystem_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        snapshot[relative] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return snapshot


def snapshot_document(fixture_path: Path) -> dict[str, Any]:
    return {
        "snapshot_schema_version": 1,
        "fixture_path": str(fixture_path),
        "files": filesystem_snapshot(fixture_path),
    }


def render_prompt(definition: EvalDefinition, fixture_path: Path) -> str:
    has_target_placeholder = "{{TARGET}}" in definition.prompt_template
    if definition.response_only and has_target_placeholder:
        raise SystemExit(f"Response-only eval unexpectedly exposes a target: {definition.eval_name}")
    if not definition.response_only and not has_target_placeholder:
        raise SystemExit(f"Fixture eval has no target placeholder: {definition.eval_name}")
    return definition.prompt_template.replace("{{TARGET}}", str(fixture_path))


def expected_output_names(definition: EvalDefinition) -> list[str]:
    return EXPECTED_OUTPUT_NAMES.get(
        (definition.skill_name, definition.eval_id), ["transcript.md"]
    )


def dispatch_prompt(run: EvalRun, prompt: str, skill_path: Path | None) -> str:
    skill_line = str(skill_path) if skill_path is not None else "none (baseline run)"
    artifacts = ", ".join(expected_output_names(run.definition))
    input_line = (
        "none; do not inspect any target before confirmation"
        if run.definition.response_only
        else f"fixture target at {run.fixture_path}"
    )
    return (
        "Execute this task exactly as an independent skill evaluation run:\n"
        f"- Skill path: {skill_line}\n"
        f"- Task: {prompt}\n"
        f"- Input files: {input_line}\n"
        f"- Save outputs to: {run.output_path}\n"
        f"- Outputs to save: {artifacts}\n"
        "- Save transcript.md as the complete user-visible response plus an ordered record of "
        "tool calls and commands. Save preview.json only when a JSON preview is produced.\n"
        "- Treat the mapped fixture as the only mutable target. Do not modify the skill source or "
        "the evaluation workspace outside the assigned output directory.\n"
        "- Do not invoke external model CLIs. Do not initialize or mutate Git or GitHub."
    )


def eval_metadata(definition: EvalDefinition, prompt: str) -> dict[str, Any]:
    return {
        "eval_id": definition.eval_id,
        "eval_name": definition.eval_name,
        "prompt": prompt,
        "assertions": list(definition.expectations),
    }


def prepare_workspace_root(skill_name: str) -> Path:
    workspace_root = REPOSITORY_ROOT / f"{skill_name}-workspace"
    marker_value = {
        "generated_by": "tests/build_eval_fixtures.py",
        "purpose": "non-release skill evaluation workspace",
        "skill_name": skill_name,
    }
    reset_generated_directory(workspace_root, WORKSPACE_MARKER, marker_value)
    iteration_root = workspace_root / "iteration-1"
    iteration_root.mkdir()
    return iteration_root


def prepare_environment(fixtures_root: Path) -> list[dict[str, Any]]:
    fixture_marker = {
        "generated_by": str(Path(__file__).resolve()),
        "purpose": "independent external skill evaluation fixtures",
    }
    reset_generated_directory(fixtures_root, FIXTURE_MARKER, fixture_marker)
    definitions_by_skill = {skill: parse_evals(skill) for skill in SKILL_NAMES}
    combined_runs: list[dict[str, Any]] = []

    for skill_name in SKILL_NAMES:
        iteration_root = prepare_workspace_root(skill_name)
        skill_path = REPOSITORY_ROOT / skill_name
        skill_runs: list[dict[str, Any]] = []
        for definition in definitions_by_skill[skill_name]:
            eval_directory = iteration_root / f"eval-{definition.eval_id}-{definition.eval_name}"
            eval_directory.mkdir()
            write_json(
                eval_directory / "eval_metadata.json",
                eval_metadata(definition, definition.prompt_template),
            )
            for configuration in CONFIGURATIONS:
                fixture_path = (
                    fixtures_root
                    / skill_name
                    / f"eval-{definition.eval_id}-{definition.eval_name}"
                    / configuration
                )
                create_fixture(fixture_path, skill_name, definition.eval_id)
                run = EvalRun(definition, configuration, fixture_path, eval_directory)
                run.output_path.mkdir(parents=True)
                prompt = render_prompt(definition, fixture_path)
                run_skill_path = skill_path if configuration == "with_skill" else None
                write_json(run.run_directory / "eval_metadata.json", eval_metadata(definition, prompt))
                snapshot_path = run.run_directory / "fixture_snapshot.before.json"
                allowlist_path = run.run_directory / "expected_changed_paths.json"
                write_json(snapshot_path, snapshot_document(fixture_path))
                write_json(
                    allowlist_path,
                    {
                        "allowlist_schema_version": 2,
                        "comparison": "exact",
                        "fixture_path": str(fixture_path),
                        "expected_changed_paths": EXPECTED_CHANGED_PATHS.get(
                            (skill_name, definition.eval_id), []
                        ),
                        "expected_runtime_paths": EXPECTED_RUNTIME_PATHS.get(
                            (skill_name, definition.eval_id), []
                        ),
                    },
                )
                expected_artifacts = [
                    str(run.output_path / name) for name in expected_output_names(definition)
                ]
                run_record = {
                    "run_id": run.run_id,
                    "eval_id": definition.eval_id,
                    "eval_name": definition.eval_name,
                    "configuration": configuration,
                    "prompt": prompt,
                    "expected_output": definition.expected_output,
                    "dispatch_prompt": dispatch_prompt(run, prompt, run_skill_path),
                    "skill_path": str(run_skill_path) if run_skill_path is not None else None,
                    "fixture_path": str(fixture_path),
                    "working_directory": str(fixture_path),
                    "output_path": str(run.output_path),
                    "expected_saved_artifacts": expected_artifacts,
                    "expected_changed_paths": EXPECTED_CHANGED_PATHS.get(
                        (skill_name, definition.eval_id), []
                    ),
                    "expected_runtime_paths": EXPECTED_RUNTIME_PATHS.get(
                        (skill_name, definition.eval_id), []
                    ),
                    "baseline_snapshot_path": str(snapshot_path),
                    "expected_changed_paths_path": str(allowlist_path),
                    "response_only": definition.response_only,
                }
                skill_runs.append(run_record)
                combined_runs.append(run_record)
        write_json(
            iteration_root / "eval_run_manifest.json",
            {
                "manifest_schema_version": 1,
                "skill_name": skill_name,
                "skill_path": str(skill_path),
                "iteration_root": str(iteration_root),
                "runs": skill_runs,
            },
        )
    write_json(
        fixtures_root / "eval_run_manifest.json",
        {
            "manifest_schema_version": 1,
            "repository_root": str(REPOSITORY_ROOT),
            "fixtures_root": str(fixtures_root),
            "runs": combined_runs,
        },
    )
    return combined_runs


def fixture_fingerprint(fixtures_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in fixtures_root.rglob("*") if item.is_file()):
        relative = path.relative_to(fixtures_root).as_posix()
        if relative == "eval_run_manifest.json" or relative == FIXTURE_MARKER:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def changed_paths(before: dict[str, Any], fixture_path: Path) -> list[str]:
    before_files = before.get("files")
    if not isinstance(before_files, dict):
        raise SystemExit("Baseline snapshot has no files object")
    after_files = filesystem_snapshot(fixture_path)
    return sorted(
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    )


def parse_expected_paths(
    allowlist: dict[str, Any], key: str, path: Path, *, default: list[str] | None = None
) -> list[str]:
    expected = allowlist.get(key, default)
    if not isinstance(expected, list) or any(
        not isinstance(item, str) or not item for item in expected
    ):
        raise SystemExit(f"Invalid {key} allowlist: {path}")
    if len(expected) != len(set(expected)) or expected != sorted(expected):
        raise SystemExit(f"{key} must contain unique paths in sorted order: {path}")
    return expected


def is_runtime_path(path: str) -> bool:
    return path.startswith(RUNTIME_PATH_PREFIXES)


def verify_run(run_record: dict[str, Any]) -> dict[str, Any]:
    snapshot_path = Path(run_record["baseline_snapshot_path"])
    allowlist_path = Path(run_record["expected_changed_paths_path"])
    fixture_path = Path(run_record["fixture_path"])
    baseline = parse_json_object(snapshot_path)
    allowlist = parse_json_object(allowlist_path)
    schema_version = allowlist.get("allowlist_schema_version")
    if schema_version not in {1, 2} or allowlist.get("comparison") != "exact":
        raise SystemExit(f"Invalid changed-path allowlist schema: {allowlist_path}")
    if allowlist.get("fixture_path") != str(fixture_path):
        raise SystemExit(f"Changed-path allowlist fixture mismatch: {allowlist_path}")
    if schema_version == 2 and "expected_runtime_paths" not in allowlist:
        raise SystemExit(f"Changed-path allowlist has no expected_runtime_paths: {allowlist_path}")
    actual = changed_paths(baseline, fixture_path)
    expected = parse_expected_paths(allowlist, "expected_changed_paths", allowlist_path)
    expected_runtime = parse_expected_paths(
        allowlist, "expected_runtime_paths", allowlist_path, default=[]
    )
    misclassified_runtime = [path for path in expected if is_runtime_path(path)]
    if misclassified_runtime:
        raise SystemExit(
            f"Runtime paths must use expected_runtime_paths at {allowlist_path}: "
            f"{misclassified_runtime}"
        )
    unsupported_runtime = sorted(set(expected_runtime) - PERMITTED_RUNTIME_PATHS)
    if unsupported_runtime:
        raise SystemExit(
            f"Unsupported expected runtime paths at {allowlist_path}: {unsupported_runtime}"
        )
    actual_runtime = [path for path in actual if is_runtime_path(path)]
    actual_durable = [path for path in actual if not is_runtime_path(path)]
    unexpected_runtime = sorted(set(actual_runtime) - set(expected_runtime))
    return {
        "run_id": run_record["run_id"],
        "fixture_path": str(fixture_path),
        "expected_changed_paths": expected,
        "actual_durable_changed_paths": actual_durable,
        "expected_runtime_paths": expected_runtime,
        "actual_runtime_paths": actual_runtime,
        "unexpected_runtime_paths": unexpected_runtime,
        "actual_changed_paths": actual,
        "passed": actual_durable == expected and not unexpected_runtime,
    }


def check_environment(fixtures_root: Path) -> None:
    first_runs = prepare_environment(fixtures_root)
    first_fingerprint = fixture_fingerprint(fixtures_root)
    first_run_ids = [run["run_id"] for run in first_runs]
    second_runs = prepare_environment(fixtures_root)
    second_fingerprint = fixture_fingerprint(fixtures_root)
    second_run_ids = [run["run_id"] for run in second_runs]
    if first_run_ids != second_run_ids or first_fingerprint != second_fingerprint:
        raise SystemExit("Fixture generation is not deterministic")
    pristine_failures = []
    for run in second_runs:
        result = verify_run(run)
        if result["actual_changed_paths"]:
            pristine_failures.append(result)
    if pristine_failures:
        raise SystemExit(
            "Fresh fixture snapshots do not match generated fixtures:\n"
            + json.dumps(pristine_failures, indent=2)
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "eval_schema_files": [
                    str(REPOSITORY_ROOT / skill / "evals" / "evals.json")
                    for skill in SKILL_NAMES
                ],
                "fixture_fingerprint": second_fingerprint,
                "fixtures_root": str(fixtures_root),
                "runs": len(second_runs),
                "snapshots": "pristine",
            },
            indent=2,
        )
    )


def load_combined_manifest(fixtures_root: Path) -> dict[str, Any]:
    manifest = parse_json_object(fixtures_root / "eval_run_manifest.json")
    if manifest.get("manifest_schema_version") != 1 or not isinstance(
        manifest.get("runs"), list
    ):
        raise SystemExit("Generated eval run manifest schema is invalid")
    return manifest


def grade_named_run(fixtures_root: Path, run_id: str) -> None:
    manifest = load_combined_manifest(fixtures_root)
    matches = [run for run in manifest["runs"] if run.get("run_id") == run_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one manifest run for {run_id!r}")
    result = verify_run(matches[0])
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


def print_dispatch_prompts(fixtures_root: Path) -> None:
    manifest = load_combined_manifest(fixtures_root)
    for run in manifest["runs"]:
        print(f"=== {run['run_id']} ===")
        print(run["dispatch_prompt"])
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build deterministic, independent skill-evaluation fixtures outside the source repo."
    )
    parser.add_argument("--fixtures-root", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--grade-run", metavar="SKILL:EVAL_ID:CONFIGURATION")
    action.add_argument("--print-dispatch-prompts", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fixtures_root = validate_external_fixture_root(args.fixtures_root)
    if args.check:
        check_environment(fixtures_root)
        return
    if args.grade_run:
        grade_named_run(fixtures_root, args.grade_run)
        return
    if args.print_dispatch_prompts:
        print_dispatch_prompts(fixtures_root)
        return
    runs = prepare_environment(fixtures_root)
    print(
        json.dumps(
            {
                "status": "prepared",
                "fixtures_root": str(fixtures_root),
                "manifest": str(fixtures_root / "eval_run_manifest.json"),
                "runs": len(runs),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

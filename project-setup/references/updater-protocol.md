# Coordinator and update protocol

The coordinator is installed at `.agents/project_management/setup/coordinator.py`. It supports Python 3.10+ and the standard library only.

## Commands

Every command requires an explicit absolute `--root`. There are exactly five user-facing commands:

```bash
python3 .agents/project_management/setup/coordinator.py --root "/absolute/project" status
python3 .agents/project_management/setup/coordinator.py --root "/absolute/project" plan-update --source "/absolute/verified-release-source"
python3 .agents/project_management/setup/coordinator.py --root "/absolute/project" update --source "/absolute/verified-release-source" --plan-token "<exact plan_token>"
python3 .agents/project_management/setup/coordinator.py --root "/absolute/project" rollback
python3 .agents/project_management/setup/coordinator.py --root "/absolute/project" doctor
```

Place `--json` before the command for canonical single-line JSON. No command aliases exist.

`status`, `plan-update`, and `doctor` are read-only and do not create target entries or Python bytecode. `plan-update` derives the selected version and manifest checksum from the supplied local source's verified `releases/index.json`. For compatibility, callers may provide both `--version` and `--manifest-sha256`; providing only one is invalid. It verifies index/manifest identity, the manifest checksum, stable tag/commit/source-checkout identity, and every artifact checksum, stages candidates outside the target, then reports exact operations, candidate hashes, conflicts, and a canonical plan token. `update` requires that token and recalculates it under the advisory lock, rejecting target or source drift. Stable remote discovery is deliberately deferred; no command resolves or executes floating remote code.

The release also installs `integrate_instructions.py`. Its `plan` action is read-only; `apply` requires the exact canonical plan token and shares the coordinator lock. Destination writes, receipt, and state are one fsynced journaled transaction with startup recovery. Instruction bundles have separate durable state and receipts so multiple `managed_block` artifacts may safely share `AGENTS.md` or `CLAUDE.md`. Coordinator `status` reports installed bundle versions/destinations; `doctor` verifies each recorded block and its latest receipt.

The coordinator transaction owns only artifacts listed in its release manifest. It installs the tracker wrapper/archive and instruction integrator/catalog as tools, but does not install tracker target files or apply instruction blocks. Those remain separate preview-and-token transactions with separate durable state and receipts.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success, healthy, ready, current, updated, or rolled back. |
| `1` | Customization, ownership, retirement, or rollback conflict. |
| `2` | Invalid arguments, path, manifest, checksum, or durable state. |
| `3` | Another mutating coordinator process holds the lock. |
| `4` | Filesystem failure or test-injected interruption requiring recovery. |
| `5` | Diagnostic failure or an interrupted journal detected by a read-only command. |

## Transaction and recovery contract

The coordinator parses manifest paths as strict project-relative paths and rejects absolute, empty, dot, parent, NUL, duplicate, case-fold-colliding, and symlinked paths. Candidate files are fully staged and validated before target writes. Mutating commands then acquire a process-scoped OS advisory lock, back up every touched existing file, fsync a journal, apply atomic same-filesystem replacements, write a receipt, and write durable state last. The kernel releases the lock on normal exit or hard process termination; a leftover lock path is not ownership.

If startup finds a journal, the next mutating command restores the complete previous state unless the new state was already written, in which case it completes the new state. Read-only commands report recovery as required without changing files.

Rollback covers one successful release only and refuses when any touched target differs from its recorded post-update checksum. Retirements can remove only exact manifest-listed, receipt-owned files whose current checksum equals the baseline. Unknown files are never synchronized, merged, or deleted.

`update` rejects historical version downgrades. The only supported backward transition is the one-release `rollback`; arbitrary historical selection is not implemented.

Customized managed files or managed blocks abort the whole update before project writes. The candidate and deterministic conflict report are retained under `.agents/project_management/setup/.runtime/conflicts/<transaction>/`.

## Filesystem constraint

Python does not expose a uniform cross-platform recursive `openat2` API. The runtime uses `lstat` on every existing relative component immediately before reads and mutations, `O_NOFOLLOW` for file reads where available, same-filesystem temporary files, `fsync`, and atomic `os.replace`. A hostile process with concurrent write access could still race directory-component replacement between checks on platforms without stronger kernel primitives; do not run updates in an untrusted concurrently writable target.

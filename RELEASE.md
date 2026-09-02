# Release finalization and publication

The tracked source tree remains truthfully `0.2.0-dev.0` and unreleased. Stable identity is created only after publication in an out-of-tree distribution. The finalizer requires an exact `v<version>` tag, verifies that `refs/tags/<tag>`, the supplied full commit, and checkout `HEAD` resolve to the same commit, and stages bytes from that verified commit object. It never writes to the source checkout or Git, avoiding the impossible requirement that a commit contain its own hash.

## Deterministic preflight

```bash
python3 sync_release_material.py sync
python3 sync_release_material.py sync --check
python3 sync_release_material.py build --output "dist/0.2.0-dev.0" --force
python3 sync_release_material.py inspect --distribution "dist/0.2.0-dev.0"
```

`build` emits both public `.skill` archives, deterministic path-and-hash inventories, `SHA256SUMS`, a guarded unreleased bootstrap template, a release-candidate descriptor, and a staged working-tree source snapshot. It uses Git's tracked-plus-untracked, non-ignored inventory and additionally excludes tests, evals, caches, bytecode, workspaces, distribution output, and OS debris. It always remains `unreleased` and cannot emit a final runnable bootstrap.

## Human publication steps that remain

1. Finish source review and run all verification below.
2. A human release owner creates and reviews the release commit and stable tag, then pushes them. This tooling does not commit, tag, push, fetch, change Git configuration, or upload assets.
3. In a separate, completely clean checkout of the published commit, independently resolve the exact `refs/tags/v<version>` tag and full commit. The commit must match `^[0-9a-f]{40}$`; abbreviated or uppercase values are invalid. Dirty tracked files, untracked files, and ignored contamination are rejected.
4. Run the finalizer from that checkout. Git verification is mandatory and uses only local read-only `status`, `rev-parse`, `ls-tree`, and `cat-file --batch` operations; it never fetches. There is no production CLI mode for synthetic stable identities.

```bash
python3 sync_release_material.py finalize \
  --source "/absolute/published-checkout" \
  --output "/absolute/release-output/0.2.0" \
  --version "0.2.0" \
  --tag "v0.2.0" \
  --commit "<40-lowercase-hex-commit>"

python3 sync_release_material.py inspect \
  --distribution "/absolute/release-output/0.2.0"
```

5. Validate both extracted `.skill` archives and smoke-test the stable package. Rebuild to a second output and compare every file byte-for-byte.
6. Upload `project-setup.skill`, `task-tracking-setup.skill`, `SHA256SUMS`, `release-descriptor.json`, and `github-bootstrap.txt` to the matching published release. Re-download and verify checksums before announcement.

## Required validation

```bash
python3 -m unittest discover -s tests -v
python3 task-tracking-setup/scripts/self_test.py
python3 sync_release_material.py sync --check
python3 /Users/x/.config/opencode/skills/skill-creator/scripts/quick_validate.py project-setup
python3 /Users/x/.config/opencode/skills/skill-creator/scripts/quick_validate.py task-tracking-setup
git diff --check
```

Also validate both extracted archives, compile Python with Python 3.10 grammar, run JavaScript syntax checks, parse every JSON/YAML file, check documentation links and managed markers, run clean-checkout simulation, and confirm no `__pycache__`, `.pyc`, or lingering process remains.

## Final artifact contract

A stable output contains:

- a generated stable `source/` staging tree with coherent root and isolated indexes, manifests, release-info, both capability catalogs, tracker integration/provenance, and deterministic vendored tracker archive;
- deterministic `project-setup.skill` and `task-tracking-setup.skill` archives;
- `SHA256SUMS` covering both archives and the final bootstrap;
- `release-descriptor.json` with identity, inventory, and metadata checksums; and
- `github-bootstrap.txt` containing the final tag and full commit with no release placeholders or development version.

Coordinator bundle updates install or update only manifest-owned coordinator/project-setup files. Tracker installation and instruction-block application remain separately previewed, separately token-approved components; the bundle includes their verified installers and catalogs but does not silently apply either one.

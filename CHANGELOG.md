# Changelog

All notable bundle changes are recorded here. Versions describe source-tree state; publication is declared separately in `releases/index.json`.

## 0.2.0-dev.0 — Unreleased

### Added

- Four-mode, preview-first setup protocol with an explicit absolute-target gate.
- Machine-readable capability and release metadata.
- Offline coordinator with status, update planning, transactional update, one-release rollback, and diagnostics.
- Hardened plan-then-apply tracker installation and migration foundation.
- Managed task-board shell, classic CSS/JavaScript application, and versioned deterministic board-data payload.
- Six destination-aware, versioned instruction bundles with deterministic manual and evidence-based gap-analysis planning.

### Changed

- Git and GitHub are optional user-controlled follow-up work, never setup requirements.
- Supporting capabilities require explicit selection and verified stable pins.
- Task rendering now validates complete tracker storage and atomically replaces only `task_board.data.js`; managed assets and tracker configuration remain untouched.
- Legacy board templates retire only with a recorded or accepted exact checksum; custom and unknown files remain in place as conflicts or preserved data.
- Instruction integration now uses explicit destinations and bundle IDs, preserves unmanaged bytes, records managed-block receipts/state, and exposes drift through coordinator status/doctor.
- Capability inventory rendering accepts only explicit catalog IDs and never installs or updates capabilities.
- Coordinator, tracker, and instruction applies now require canonical approved preview tokens bound to the target, manifest/catalog sources, operations, and candidate hashes.
- Coordinator and tracker locks now use process-scoped OS advisory locking so hard termination cannot block journal recovery.
- Instruction integration and multi-file task archival now use fsynced journals and backups with hard-crash recovery at every write boundary.
- Legacy v2/v3 metadata is retained under documented v4 migration namespaces.
- Coordinator state reports the independently installed tracker as absent, obsolete, modified, or current instead of claiming tracker artifacts it did not install.
- `project-setup/` now vendors release metadata, capability/instruction catalogs, and a checksum-verified tracker archive for isolated installation.
- Tracker manifests now strictly bind and validate source mappings, policies, checksums, block metadata, and dynamic artifact rules.
- Board view tabs use roving tabindex with Arrow, Home, and End focus/selection synchronization during direct `file://` use.
- Release material now comes from one deterministic semantic builder for unreleased source candidates and post-publication stable distributions.
- Stable finalization overlays a separately verified tag/full commit/source-checkout identity out of tree, builds both public skill archives and checksums, and emits a placeholder-free bootstrap without mutating Git or source.
- Coordinator updates can derive version and manifest checksum from a verified local release index while preserving explicit argument compatibility and mandatory plan tokens.
- Runtime coordinator/tracker provenance now comes from verified package metadata; stable identity requires a lowercase 40-character commit and coherent tag/source identity.
- The tracker skill is exposed as `task-tracking-setup` and is limited to installation and maintenance; installed AGENTS/CLAUDE blocks now govern routine task work directly.

No tag or immutable commit is claimed for this unreleased version.

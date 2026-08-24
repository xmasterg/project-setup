# Local setup checklist

## Preserve and inspect

- Read every applicable `AGENTS.md` before editing.
- Read every applicable `CLAUDE.md` and treat it as existing project guidance; do not replace it.
- Inspect project manifests, important top-level folders, Git status, remotes, and ignore rules.
- Determine the repository root, application roots, workspace/package boundaries, and whether the target is new or existing before choosing destinations.
- Preserve existing instructions, task data, configuration, unrelated changes, and running services.
- Do not start a development server for setup.

## Choose the layout branch

### New project

- Create root `.agents/project_management/` and `app/` boundaries.
- Put development files under `app/`. Keep root `AGENTS.md`, project-management documents, tracker data/scripts, and AI plans outside `app/`.
- Keep repository metadata and appropriate human-facing root files at the repository root.
- If `app/` remains empty, copy `assets/app/.gitkeep`; remove the marker after development files exist. Do not create an AI or project document inside `app/` to make the folder visible to Git.

### Existing project

- Keep the detected application topology by default. Never force an existing `src/`, `web/`, monorepo, or root application into `app/` without explicit full-adjustment consent.
- If `AGENTS.md` or `CLAUDE.md` exists, use the three-option prompt from `SKILL.md` and wait before writing files.
- For full adjustment, inventory exact sources and destinations, detect collisions, preserve application file contents, rewrite root `AGENTS.md` using all still-applicable existing instructions, and update instruction/documentation path references. Do not silently repair application code or external automation affected by moved paths.
- For preserve-layout full integration, leave application paths unchanged and perform every project-guidance, documentation, tracking, capability, inventory, and validation item below.
- For task-management only, run only the task-tracking and applicable validation sections. Do not add project description, lessons, inventory, project maps, supporting capabilities, or rewrite existing instruction files beyond the bounded task-tracker block. If only `CLAUDE.md` exists, create minimal root `AGENTS.md` with that block and a pointer to `CLAUDE.md`.
- Without either instruction file, preserve the existing application topology and use preserve-layout full integration. Ask only if repository-root placement remains ambiguous.

## Project guidance and documentation

1. If root `AGENTS.md` is missing, copy `assets/agents.template.md` from this skill. Replace its empty inventory and map sections with current project facts and selected layout. When `CLAUDE.md` exists, include it in the map and direct Codex to read it without copying contradictory instructions.
2. If `AGENTS.md` exists, merge only missing project-management sections from the template unless confirmed full adjustment requires the documented rewrite. Do not replace or weaken existing instructions. Keep each managed marker pair unique.
3. Create `.agents/project_management/project_description.md` from `assets/project/project_description.md` when missing, then describe the actual project purpose, stack, architecture, commands, and current status supported by repository evidence.
4. Create `.agents/project_management/lessons-learned.md` from `assets/project/lessons-learned.md` when missing. Do not invent lessons.
5. Update `Project File Map` with meaningful project paths and one-line ownership descriptions. For a new project, identify `app/` as the development root. For an existing project, record actual application roots rather than inventing `app/`. Exclude `.git`, dependency folders, caches, and generated output.
6. Update `Project-Management File Map` with exact `.agents/project_management/` paths that exist.

## Task tracking

- Locate the installed `track-project-tasks/SKILL.md`, read it fully, and run its installer from the target root.
- Confirm active JSON files, tracker scripts, ideation templates, and `open_task_board.html` exist.
- Run the tracker self-test from the installed skill. Do not modify active project tasks merely to test setup.

## Supporting capabilities

Check before installing anything. Avoid duplicates.

- If `$caveman` is unavailable, use the host's plugin-management flow to install `JuliusBrussee/caveman`. If the host cannot install GitHub plugins, report that limitation without blocking other setup work.
- If Brooks review/audit/debt/test/health/sweep skills are unavailable, use the built-in skill installer to install them from `hyhmrright/brooks-lint`. Install only skill directories discovered in that repository; do not guess paths.
- Do not reinstall or replace an existing skill/plugin unless the user explicitly requests an upgrade.

## Available MCP and skill inventory

Update the bounded `agent-accessible-mcp-and-skills` section in `AGENTS.md`:

- list exact MCP server/tool-family names available in the current Codex session;
- list exact skill names available to the current Codex session;
- identify repo-scoped entries when relevant;
- never include credentials, tokens, private endpoints, filesystem cache paths, or generated tool schemas.

Keep entries concise and alphabetized. This is a current setup snapshot, not a promise that every collaborator has identical user-scoped tools.

## Validate local setup

- Validate both installed skills with the bundled skill validator when available.
- Run `git diff --check`.
- Review generated files and ensure links and paths in `AGENTS.md` resolve. For new projects, confirm `app/` exists and contains no AI or project-management docs. For existing projects, compare changed paths with the selected mode and confirm no unapproved application move occurred.
- Continue to GitHub setup only after local validation passes.

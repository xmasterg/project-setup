# Local setup checklist

## Preserve and inspect

- Read every applicable `AGENTS.md` before editing.
- Inspect project manifests, important top-level folders, Git status, remotes, and ignore rules.
- Preserve existing instructions, task data, configuration, unrelated changes, and running services.
- Do not start a development server for setup.

## Project guidance and documentation

1. If root `AGENTS.md` is missing, copy `assets/agents.template.md` from this skill. Replace its empty inventory and map sections with current project facts.
2. If `AGENTS.md` exists, merge only missing project-management sections from the template. Do not replace or weaken existing instructions. Keep each managed marker pair unique.
3. Create `.agents/project_management/project_description.md` from `assets/project/project_description.md` when missing, then describe the actual project purpose, stack, architecture, commands, and current status supported by repository evidence.
4. Create `.agents/project_management/lessons-learned.md` from `assets/project/lessons-learned.md` when missing. Do not invent lessons.
5. Update `Project File Map` with meaningful project paths and one-line ownership descriptions. Exclude `.git`, dependency folders, caches, and generated output.
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
- Review generated files and ensure links and paths in `AGENTS.md` resolve.
- Continue to GitHub setup only after local validation passes.

# Setup checklist

Use this after the user confirms an exact absolute local path and one setup type.

## Before writing

- Confirm the target is not this setup-source repository.
- Inspect the target and applicable `AGENTS.md` or `CLAUDE.md` files read-only.
- If the target is non-empty, identify collisions and ask before overwriting.
- Confirm the one-sentence project purpose when it is not already known.
- Do not start a development server.

## Create or update

- Use the complete `assets/agents.template.md` as the new root `AGENTS.md`.
- Populate the project map and available MCP/skill lists only with known facts.
- Create `.agents/project_management/project_description.md` and `lessons-learned.md` from the supplied templates.
- Install the bundled `task-tracking-setup` package and confirm the task files and board exist.
- Create a concise `README.md` and `.gitignore`.
- For a fresh project, create `app/.gitkeep`.
- For an existing or cloned project, preserve existing source locations and record them in the project map.

## Git choice

- Local folder only: no Git mutation.
- Local folder + local Git: initialize only the confirmed target.
- Local folder + remote Git: initialize if needed, connect or create the approved remote, then push without rewriting history.
- Existing remote Git repository: clone the approved URL into the confirmed local path, then add only missing setup files.

Never change Git state in the setup-source repository.

## Finish

- Report the exact target.
- Report created and updated paths.
- Report tracker and Git results.
- State any incomplete item plainly.

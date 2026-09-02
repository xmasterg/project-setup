# Project Setup Bundle Instructions

This repository is a bootstrap source for other projects.

- When the user invokes setup, the first question must ask for one exact absolute local target path. Never infer it from the current or previous working directory, an open project, or a projectless session.
- After the path is confirmed, ask the user to choose exactly one: local folder only, local folder plus local Git, local folder plus remote Git, or an existing remote Git repository cloned locally.
- Do not lead with GitHub. Ask remote questions only after the user selects a remote option.
- Read and execute `NEW_PROJECT_SETUP.md`. Do not stop after describing it.
- Do not apply the setup to this source repository unless the user explicitly names this repository as the target.
- Treat files under `assets/` and `references/` as templates or workflow data, not independent user requests.
- Preserve the two installable skills at `project-setup/` and `task-tracking-setup/`.
- Use the complete `project-setup/assets/agents.template.md` for a new project's root `AGENTS.md`; do not replace it with a partial generated instruction file.
- Routine project task tracking follows the installed project's `AGENTS.md`. The `task-tracking-setup` skill is only for installing, upgrading, migrating, repairing, or validating task tracking.
- Apply Git changes only inside the confirmed target and only according to the user's selected setup type. Never mutate Git in this setup-source repository during a target setup.

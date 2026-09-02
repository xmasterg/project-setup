---
name: project-setup
description: Create a new local software project folder with complete AGENTS.md guidance, project documentation, local task tracking, and the user's chosen Git setup. Use when the user asks to set up, initialize, bootstrap, or start a new project.
compatibility: opencode
---

# Project setup

Create a practical project workspace. Keep the conversation short and do the setup after the user answers the required questions.

## 1. Always ask where first

The first question must ask for one exact absolute local folder path:

```text
Where should I set up the project? Please give me the exact absolute local folder path.
```

Ask this even when the session is projectless, another project is open, or the current directory looks suitable. Never infer the destination from the current or previous working directory.

## 2. Ask one setup-type question

After the local path is confirmed, ask:

```text
How should I set it up?
1. Local folder only (no Git)
2. Local folder + local Git repository
3. Local folder + remote Git repository
4. Existing remote Git repository cloned locally
```

Do not lead with GitHub and do not assume the user wants Git. Ask only the follow-up needed by the selected option:

- Option 1: no Git follow-up.
- Option 2: no remote follow-up.
- Option 3: ask for the remote URL, or whether to create a new remote. If creating one, ask private or public.
- Option 4: ask for the existing remote repository URL.

Use GitHub only when the user chooses it; remote Git may use another host.

## 3. Confirm the small plan

Inspect the confirmed path read-only. If it is non-empty, name the files that could conflict and ask before overwriting them. Otherwise give one short confirmation containing the path and selected setup type. Do not present coordinator modes, capability catalogs, managed instruction bundles, release pins, or preview tokens.

Ask for a one-sentence project purpose only if the user has not supplied one. Derive the project name from the destination folder. Do not ask for a technology stack unless the user also wants starter application code.

## 4. Create the project

Read [references/setup-checklist.md](references/setup-checklist.md), then perform the setup rather than merely describing it.

For a fresh local project create:

```text
<target>/
|-- .agents/project_management/
|-- app/
|-- AGENTS.md
|-- README.md
`-- .gitignore
```

- Copy the complete [assets/agents.template.md](assets/agents.template.md) into root `AGENTS.md`. Keep every major section. Replace only factual placeholders such as the project map, available tools, project name, and paths.
- Copy `assets/project/project_description.md` and `assets/project/lessons-learned.md` into `.agents/project_management/`, then add the known project name and purpose without inventing technical facts.
- Create a concise `README.md` with the project name, purpose, and actual start/run instructions if known.
- Create a small `.gitignore` for macOS/editor noise plus the chosen stack when known.
- Keep application code under `app/` for a fresh project. Use `app/.gitkeep` while it is empty.
- Install the bundled task tracker under `.agents/project_management/tasks/` with `runtime/install_tracker.py`. Run its dry-run, then apply the exact returned plan token. The complete `AGENTS.md` already governs routine task use; `task-tracking-setup` is only the installer/maintenance skill.

For an existing or cloned repository, preserve its application layout. Do not move existing source into `app/`. Record the actual source roots in the complete `AGENTS.md` project map.

Do not use the coordinator or instruction-bundle catalog for this ordinary setup flow.

## 5. Apply the selected Git choice

- Option 1: do not run Git commands that change state.
- Option 2: initialize Git only inside the confirmed target.
- Option 3: initialize Git if needed, connect or create the approved remote, and push the initial project only after the remote details are known.
- Option 4: clone the approved remote into the confirmed local path before adding missing setup files.

Never initialize, commit, push, or change remotes in this setup-source repository. Never force-push or rewrite history.

## 6. Finish simply

Report the exact local path, created files, tracker location, and Git result. If something could not be completed, say exactly what remains. Do not turn completion into another setup workflow.

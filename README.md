# Project Setup for Codex

No skill or plugin needs to be installed first.

Open a clean Codex session in the project folder and paste this one line:

```text
Run new project setup from https://github.com/xmasterg/project-setup.git
```

Codex accesses this repository, reads [NEW_PROJECT_SETUP.md](NEW_PROJECT_SETUP.md), installs the bundled skills into the user-level Codex skill location selected by the built-in installer, and executes setup against the original project folder in the same turn.

For a new project, setup creates this boundary:

```text
root/
|-- .agents/
|   |-- project_management/
|   `-- ...
|-- app/                 # web, mobile, API, packages, and development files
|-- AGENTS.md
`-- ...                  # repository metadata and appropriate root files
```

Root AI entrypoints such as `AGENTS.md` stay at the repository root. Project-management docs, task data, plans, and tracker scripts live under `.agents/project_management/`; none belong under `app/`.

It asks for the GitHub destination:

```text
Do you already have a GitHub repository for this project? Paste its URL, or reply create to make a new private repository named <project-folder>.
```

For an existing project, Codex first inspects the current layout. It does not force current application code into `app/`. When `AGENTS.md` or `CLAUDE.md` already exists, Codex also asks which setup mode to use:

1. **Full adjustment — Danger zone:** reorganize root development paths under `app/` without rewriting application source contents, and fully rewrite root `AGENTS.md` while retaining applicable existing instructions and rebasing paths.
2. **Preserve layout + full management integration (recommended):** leave application paths untouched, add the full `.agents/project_management/` workspace, and merge missing `AGENTS.md` sections.
3. **Task management only:** add the tracker workspace/scripts and bounded `AGENTS.md` task instructions; leave other project docs and structure unchanged.

After required answers, Codex completes the selected local setup, validation, Git initialization or connection, commit, push, and local/remote verification.

Because this repository is private, the clean Codex environment must already have access through Git credentials, a connected GitHub account, or an authenticated browser session. It must never ask for credentials to be pasted into chat.

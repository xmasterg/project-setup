# Project Setup for Codex

Bootstrap a project, install its local task workspace, create or connect a GitHub repository, and sync the first verified commit.

## One-prompt install and setup

Paste this into a Codex session opened at the project root:

```text
Install the project-setup skill from xmasterg/project-setup. After installation, read the installed project-setup/SKILL.md and use it to set up this project in this same turn.
```

The repository is private, so Codex needs existing GitHub access through Git credentials, a connected GitHub account, or an authenticated browser session.

## Later projects

After the skill is installed, open a Codex session in any project and paste:

```text
$project-setup set up this project
```

Codex asks whether to use an existing GitHub repository or create a new private repository, then completes local setup, commits, pushes, and verifies the sync.

## Included skills

- `project-setup`: end-to-end project bootstrap and GitHub workflow.
- `track-project-tasks`: task storage, planning documents, status lifecycle, weekly archives, and generated task board.

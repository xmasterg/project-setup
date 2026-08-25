# Project Setup for Codex

> **TL;DR:** Set up a new or existing project with durable AI instructions, project docs, task tracking logic and local light-weight kanban/list view-only task page.

## What it does

- Creates/Updates main `AGENTS.md` .
- Adds project docs and task tracking under `.agents/project_management/`.
- Light-weight and simple view-only task kanban/list  html page for us --> the humans.
- Keeps existing project layouts unchanged by default.
- Creates or connects a GitHub repository to your project, then verifies the push.
- Uses `app/` directory (folder) for development files in brand-new projects.

## Included skills

| Skill | Purpose |
| --- | --- |
| `project-setup` | Full setup for new or existing projects |
| `track-project-tasks` | Task files, planning docs, archives, and generated board |
| `$brooks-lint` | Audits your codebase based on top 12 books on code quality, codebase debt, etc. |
| `$caveman`| Saves 70% of token usage by minimising words used for AI output (cuts out redundancy)|


# SETUP

### Full New Project Setup

- Copy/Paste in Codex session

**Full setup for new project**

```text
Run new project setup from https://github.com/xmasterg/project-setup.git
```


### Setup task tracking only

Your current project structure and docs unchanged:

```text
Add task tracking only to this project using https://github.com/xmasterg/project-setup.git. 
Install the track-project-tasks skill if missing. 
Preserve the current application and documentation layout.
```

If `track-project-tasks` is already installed:

```text
Use $track-project-tasks from https://github.com/xmasterg/project-setup.git to install or upgrade task tracking in this project.
```

### Task Tracking Page

- `.agents/project_management/tasks/task_tracking/open_task_board.html`.

**Kanban view**

![alt text](image.png)

**List View**
![alt text](image-1.png)
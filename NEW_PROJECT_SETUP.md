# New project setup

Execute this flow when the user asks to set up a project. Keep it short.

## Required questions

First ask:

```text
Where should I set up the project? Please give me the exact absolute local folder path.
```

Never infer the folder from the current session, current repository, previous directory, or project name.

After the path is confirmed, ask:

```text
How should I set it up?
1. Local folder only (no Git)
2. Local folder + local Git repository
3. Local folder + remote Git repository
4. Existing remote Git repository cloned locally
```

For option 3, ask for a remote URL or permission to create a new remote; when creating one, ask private or public. For option 4, ask for the repository URL. Do not ask about GitHub before the user chooses a remote option.

Ask for a one-sentence project purpose only when it is still unknown. Do not ask about a stack unless starter code was requested.

## Setup

Read and follow `project-setup/SKILL.md` and `project-setup/references/setup-checklist.md` from this source checkout.

For a fresh project:

- create the confirmed folder;
- create `app/` for future application code;
- create the complete root `AGENTS.md` from `project-setup/assets/agents.template.md`;
- create `README.md` and `.gitignore`;
- create `.agents/project_management/project_description.md` and `lessons-learned.md` from the supplied templates; and
- install the bundled task tracker.

For an existing or cloned repository, preserve its application layout and existing work. If a destination file already exists, inspect it and obtain approval before overwriting it.

Use the selected Git behavior only inside the confirmed target. Never mutate Git in this setup-source repository.

Finish with the target path, created or updated files, tracker location, and Git result.

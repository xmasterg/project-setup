# New Project Setup for Codex

No skill or plugin needs to be installed first.

Open a clean Codex session in the project folder and paste this one line:

```text
Run new project setup from https://github.com/xmasterg/project-setup.git
```

Codex accesses this repository, reads [NEW_PROJECT_SETUP.md](NEW_PROJECT_SETUP.md), installs the bundled skills into the user-level Codex skill location selected by the built-in installer, and executes setup against the original project folder in the same turn.

It then asks one required question:

```text
Do you already have a GitHub repository for this project? Paste its URL, or reply create to make a new private repository named <project-folder>.
```

After the answer, Codex completes project documentation, task tracking, supporting capability setup, Git initialization, commit, push, and local/remote verification.

Because this repository is private, the clean Codex environment must already have access through Git credentials, a connected GitHub account, or an authenticated browser session. It must never ask for credentials to be pasted into chat.

# Git choices during project setup

Git is optional. The user chooses one setup type after confirming the local path.

1. **Local folder only:** create files without initializing Git.
2. **Local folder + local Git repository:** run `git init` inside the confirmed target only.
3. **Local folder + remote Git repository:** initialize locally if needed, then connect an approved remote URL or create a remote with the user's chosen visibility. Push only to that approved remote.
4. **Existing remote Git repository cloned locally:** clone the approved repository URL into the confirmed local path, then install missing project-setup files without moving existing application code.

Do not assume GitHub; any Git host may be used. Never expose credentials, force-push, rewrite history, or change Git state in the setup-source repository.

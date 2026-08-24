# GitHub setup and sync

Read this file only after the user chooses an existing repository or asks Codex to create one.

## Safety boundaries

- Preserve existing Git history, remotes, branches, worktrees, and unrelated user changes.
- Never force-push, discard changes, rewrite published history, print credentials, or request a token in chat.
- Inspect remote refs before the first push. If local and remote histories are unrelated or integration conflicts, explain the evidence and ask before merging histories.
- Check filenames and tracked content for likely secrets. Report matching filenames or variable names only; never print secret values.

## Existing repository

1. Validate the supplied URL with a read-only remote query.
2. Initialize Git with `main` only if the project is not already a repository.
3. Add `origin` when absent. If `origin` points elsewhere, show both URLs and get confirmation before changing it unless the user's answer explicitly authorized replacement.
4. Fetch the remote. If it has commits, integrate related history without force. Stop for user direction when histories are unrelated or conflicts require product decisions.

## Create repository

Create a private repository by default. Respect an explicit public/private choice.

Use the first authenticated capability available:

1. connected GitHub tool or connector capable of repository creation;
2. authenticated `gh` CLI;
3. controlled, already signed-in GitHub browser session.

Do not install a CLI solely to avoid using another available authenticated route. If no route is authenticated, state exactly what needs authentication and resume after the user provides it. Do not ask the user to paste credentials.

Before creation, check for a repository name collision. Create the repository without replacing any existing local remote.

## Commit and push

1. Add or update `.gitignore` for the detected stack. At minimum exclude OS metadata, local environment files, credentials, dependency directories, caches, and generated build output when applicable.
2. For a repository with no commits, stage all safe project files. For an established repository, stage setup-generated changes only unless the user explicitly asked to sync other work.
3. Review staged paths and diff, run `git diff --cached --check`, and commit with a normal descriptive message.
4. Push the intended branch and set upstream. Never use `--force`.
5. Fetch the remote, compare `HEAD` with its upstream hash, and check working-tree status.

If nothing changed, do not create an empty commit. Still verify the existing upstream relationship.

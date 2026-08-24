<!-- source: https://github.com/xmasterg/project-setup -->
<!-- new-project-setup:start -->

- Create `AGENTS.md` in project's root
	- Take `agents.template.md` as the template
- Install/Setup `task-tracking` skill
	- `https://github.com/xmasterg/project-setup.git`
- Update `Project File Map` in `AGENTS.md`
- Update `Project-Management File Map` in `AGENTS.md`
- Install `Caveman` plugin to project
	- Before installing `Caveman` plugin check if the project or AI Agent (Codex, Claude Code, OpenCode, VSCode or any other) has `$caveman` already installed and available
	- Installation is available here: ``
- Install `brooks-lint`
	- CODEX instructions:
		- `Install the brooks-lint skill from hyhmrright/brooks-lint`. If this doesn't work send user this message: "Copy the text and just Paste it in a Codex session: ```Install the brooks-lint skill from hyhmrright/brooks-lint```"
	- Claude Code instructions:
		- Add the marketplace, then install `/plugin marketplace add hyhmrright/brooks-lint` `/plugin install brooks-lint@brooks-lint-marketplace`

- Check available MCPs and Note them in `AGENTS.md` file with available MCPs
- Check available Skills and Note them in `AGENTS.md` file with available Skills

<!-- new-project-setup:end -->
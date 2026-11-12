# Reelwright

AI-run creator studio for the @itsMrDubey channel. The design source of truth is `docs/engineering/technical-spike.md`; the build plan is the GitHub milestones (spike #1).

- Nothing gets published, posted or replied to without a recorded human approval.
- X, Instagram, Facebook and TikTok go through AlterLab Hands on the creator's connected sessions, never paid platform APIs.
- Secrets never live in the repo. They're in `~/.config/reelwright/secrets.env` (mode 600, outside git). Load them with `. scripts/secrets.sh` before starting Claude Code, because `.mcp.json` reads `ALTERLAB_API_KEY` from the environment. Code reads secrets from env vars only.
- `git config core.hooksPath scripts/hooks` turns on the pre-commit hook, which blocks commits containing any secret value, `.env` file or key-shaped string.

<!-- alterlab-routing-policy:begin -->
## Web access: use AlterLab by default

- To fetch a web page or search the web, use the AlterLab MCP tools (`alterlab_scrape`, `alterlab_search`) instead of the built-in web fetch or search tool.
- Scope: all domains.
- Use the built-in fetch for localhost, private-network addresses and URLs inside the user's own repository.
- If AlterLab returns an error, fall back to the built-in fetch for that request and report the failure with `alterlab_feedback`.
<!-- alterlab-routing-policy:end -->

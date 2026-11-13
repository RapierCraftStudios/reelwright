# Reelwright

**An AI-run creator studio.** You record; Reelwright does the rest — research, script, a voice-following teleprompter, best-take rough cuts, motion graphics in your own house style, music, captions, Shorts, multi-platform publishing (always behind your approval), and a retention-to-script learning loop that makes each video better than the last.

Built first for [@itsMrDubey](https://youtube.com/@itsMrDubey) — every video on the channel is made with it. Public so other creators can self-host it too.

> Status: bootstrapping. Design: [`docs/engineering/technical-spike.md`](docs/engineering/technical-spike.md). Build plan: milestones and issues.

## Development

Needs [uv](https://docs.astral.sh/uv/), Node 22 and pnpm.

```sh
uv sync                                  # Python deps (SpectraCut is pinned to a commit)
pnpm install                             # web + Playwright
pnpm --filter @reelwright/web build      # dashboard bundle, served by the API
uv run rw serve                          # API + dashboard on http://127.0.0.1:8000 (GET /healthz)
uv run rw projects list                  # projects in the local DB
```

The database is SQLite at `./data/reelwright.sqlite`, set by `REELWRIGHT_DB_URL`. Migrations apply on start; generate
a new one with `uv run alembic revision --autogenerate -m "<message>"`. Every external service resolves to a mock
when its credential is unset, so development and CI need no secrets.

Checks: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`, then
`pnpm -r typecheck && pnpm -r test`, then the smoke journey `pnpm exec playwright test`.

Live prod (`main`) and staging (`staging`) run on the dev server over the tailnet: see
[`docs/setup/deploy.md`](docs/setup/deploy.md).

## License
AGPL-3.0-or-later. A [RapierCraft Studios](https://github.com/RapierCraftStudios) project.

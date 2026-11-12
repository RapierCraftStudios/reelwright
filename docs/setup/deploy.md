# Deploy: prod and staging on the dev server

Reelwright runs live on the Arch dev server as systemd **user** services, reachable only over the tailnet
(spike § Runtime topology, § Security and privacy).

| Environment | Tracks    | Checkout                 | API port | Tailnet HTTPS front | Data (SQLite)                           |
|-------------|-----------|--------------------------|----------|---------------------|-----------------------------------------|
| prod        | `main`    | `~/reelwright/prod`      | 8480     | 8490                | `~/.local/share/reelwright/prod/`       |
| staging     | `staging` | `~/reelwright/staging`   | 8481     | 8491                | `~/.local/share/reelwright/staging/`    |

Each environment runs two units: `reelwright-api@<env>` (`rw serve`, bound to the machine's Tailscale IPv4) and
`reelwright-scheduler@<env>` (`rw scheduler`). Port and branch come from `deploy/env/<env>.env`; secrets, if any,
from `~/.config/reelwright/secrets.env` (never the repo).

## How a deploy works

There is no self-hosted runner (the repo is public). `reelwright-deploy.timer` fires `reelwright-deploy.service`
every 2 minutes, which runs `deploy/deploy.sh staging` and then `deploy/deploy.sh prod` from the prod checkout. A push
is therefore live within about 2 minutes plus build time, well under 5 minutes.

For each environment `deploy.sh`:

1. **Checks free disk** on the checkout's filesystem and aborts, before changing anything, below 10 GB
   (`REELWRIGHT_DEPLOY_MIN_FREE_GB`). The dev server is ~95% full.
2. Fetches `origin/<branch>`; stops if already up to date.
3. Fast-forwards (`git merge --ff-only`), then `uv sync --locked` and builds the web bundle.
4. Copies the SQLite DB to `reelwright.sqlite.pre-deploy`, then runs `rw migrate`.
5. Restarts both units and polls `http://<tailscale ip>:<port>/healthz` (`REELWRIGHT_HEALTH_TIMEOUT`, default 60 s).
6. **On any failure after the fetch, rolls back**: `git reset --hard` to the previous commit, restores the DB backup,
   rebuilds, restarts, and exits non-zero (visible in `systemctl --user status reelwright-deploy`).

A lock (`~/.local/share/reelwright/<env>/deploy.lock`) prevents overlapping runs.

Knobs: `REELWRIGHT_DEPLOY_ROOT` (default `~/reelwright`), `REELWRIGHT_DATA_ROOT` (default
`~/.local/share/reelwright`), `REELWRIGHT_DEPLOY_MIN_FREE_GB` (10), `REELWRIGHT_HEALTH_URL`,
`REELWRIGHT_HEALTH_TIMEOUT` (60), `REELWRIGHT_HEALTH_INTERVAL` (2).

## One-time host setup

Needs `git`, `uv`, Node 22 + `pnpm`, `tailscale` (logged in) and `flock` on the dev server.

```sh
# 1. One checkout per environment
git clone -b main    https://github.com/RapierCraftStudios/reelwright ~/reelwright/prod
git clone -b staging https://github.com/RapierCraftStudios/reelwright ~/reelwright/staging
for env in prod staging; do (cd ~/reelwright/$env && uv sync --locked && pnpm install --frozen-lockfile \
  && pnpm --filter @reelwright/web build); mkdir -p ~/.local/share/reelwright/$env; done

# 2. Keep user services running without a login session
sudo loginctl enable-linger "$USER"

# 3. Install and enable the units (symlinked from the prod checkout, so deploys update them)
mkdir -p ~/.config/systemd/user
ln -sf ~/reelwright/prod/deploy/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reelwright-api@prod reelwright-scheduler@prod \
  reelwright-api@staging reelwright-scheduler@staging reelwright-deploy.timer

# 4. Tailnet-only HTTPS fronts (for the phone prompter's microphone, which needs HTTPS)
~/reelwright/prod/deploy/deploy.sh serve
```

`deploy/tailscale-serve.json` describes the fronts in the shape of `tailscale serve status --json`;
`deploy.sh serve` substitutes the Tailscale IP for `__TAILSCALE_IP__` and runs `tailscale serve --bg --https=<port>`
for each entry. It refuses a config that enables Funnel. Never use `tailscale funnel` for Reelwright.

The `staging` branch must contain the app: promote the milestone branch to it (`staging`) and later to `main`.
Until then a deploy of that branch fails its health check and rolls back, which is harmless.

## Operations

- Logs: `journalctl --user -u reelwright-api@prod -f`, `journalctl --user -u reelwright-deploy`.
- Deploy now: `systemctl --user start reelwright-deploy` (or `~/reelwright/prod/deploy/deploy.sh staging`).
- Next run: `systemctl --user list-timers reelwright-deploy.timer`.
- Manual rollback: `git -C ~/reelwright/<env> reset --hard <sha>`, copy back `reelwright.sqlite.pre-deploy` if the
  DB was migrated, then `systemctl --user restart reelwright-api@<env> reelwright-scheduler@<env>`.

## Verify

After setup, check the two manual acceptance criteria:

1. **Live within 5 minutes.** Push a trivial commit to `staging`; within 5 minutes
   `curl http://$(tailscale ip -4):8481/healthz` returns `{"status":"ok"}` and
   `git -C ~/reelwright/staging rev-parse HEAD` equals the pushed commit. Repeat for `main` on port 8480.
2. **Tailnet only.** From the dev server: `ss -ltnp | grep -E ':848[01]'` shows only the `100.x` Tailscale address
   (no `0.0.0.0`, `*` or LAN address). From a device that is not on the tailnet (e.g. phone on mobile data with
   Tailscale off), connecting to the dev server's LAN/public address on 8480, 8481, 8490 and 8491 fails, and
   `tailscale serve status` shows no Funnel.

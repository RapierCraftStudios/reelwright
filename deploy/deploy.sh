#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Pull-based deploy for one Reelwright environment on the dev server (see docs/setup/deploy.md).
#
#   deploy.sh prod|staging   free-disk check, fast-forward, build, migrate, restart, health-check;
#                            on any failure after the fetch, roll back to the previous commit and DB.
#   deploy.sh serve          apply deploy/tailscale-serve.json (tailnet-only HTTPS fronts).
#
# Knobs (env): REELWRIGHT_DEPLOY_ROOT (checkouts, default ~/reelwright/<env>),
# REELWRIGHT_DATA_ROOT (DB + lock, default ~/.local/share/reelwright/<env>),
# REELWRIGHT_DEPLOY_MIN_FREE_GB (default 10), REELWRIGHT_HEALTH_URL (default the Tailscale IP + port),
# REELWRIGHT_HEALTH_TIMEOUT seconds (default 60), REELWRIGHT_HEALTH_INTERVAL seconds (default 2).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="${REELWRIGHT_DEPLOY_ROOT:-$HOME/reelwright}"
data_root="${REELWRIGHT_DATA_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/reelwright}"
min_free_gb="${REELWRIGHT_DEPLOY_MIN_FREE_GB:-10}"
health_timeout="${REELWRIGHT_HEALTH_TIMEOUT:-60}"
health_interval="${REELWRIGHT_HEALTH_INTERVAL:-2}"
env_name=""

log() { printf 'deploy[%s]: %s\n' "${env_name:-serve}" "$*" >&2; }
die() {
  log "$*"
  exit 1
}

tailscale_ip() {
  local ip
  ip="$(tailscale ip -4 | head -n1)"
  [[ -n "$ip" ]] || die "no Tailscale IPv4 address (is tailscaled up?)"
  printf '%s' "$ip"
}

# Abort before touching anything when the disk holding the checkout has less than min_free_gb free.
check_disk() {
  local path="$1" avail_kb need_kb
  [[ "$min_free_gb" =~ ^[0-9]+$ ]] || die "REELWRIGHT_DEPLOY_MIN_FREE_GB must be a whole number"
  avail_kb="$(df -Pk "$path" | awk 'NR == 2 { print $4 }')"
  [[ "$avail_kb" =~ ^[0-9]+$ ]] || die "cannot read free disk space for $path"
  need_kb=$((min_free_gb * 1024 * 1024))
  if ((avail_kb < need_kb)); then
    die "only $((avail_kb / 1024 / 1024)) GB free on $path, need ${min_free_gb} GB: aborting deploy"
  fi
}

# Each step is chained with && because set -e is suspended inside an if/&& condition.
build() {
  (cd "$dir" &&
    uv sync --locked --quiet &&
    pnpm install --frozen-lockfile --silent &&
    pnpm --filter @reelwright/web build)
}

migrate() { (cd "$dir" && uv run --locked rw migrate); }

restart() {
  systemctl --user restart "reelwright-api@${env_name}.service" \
    "reelwright-scheduler@${env_name}.service"
}

healthy() {
  local url="${REELWRIGHT_HEALTH_URL:-}" deadline
  [[ -n "$url" ]] || url="http://$(tailscale_ip):${port}/healthz"
  deadline=$((SECONDS + health_timeout))
  while true; do
    if curl -fsS --max-time 5 -o /dev/null "$url"; then
      return 0
    fi
    ((SECONDS < deadline)) || break
    sleep "$health_interval"
  done
  log "health check failed: $url"
  return 1
}

rollback() {
  log "deploy of ${target:0:12} failed: rolling back to ${prev:0:12}"
  git -C "$dir" reset --hard --quiet "$prev"
  if [[ -f "$backup" ]]; then
    cp -p "$backup" "$db"
  elif [[ -f "$db" ]]; then
    # Created by this deploy's migration: the previous commit ran without a DB file.
    mv -f "$db" "$db.failed-deploy"
  fi
  build || log "rebuild of ${prev:0:12} failed"
  restart || log "restart after rollback failed"
  if healthy; then
    log "rolled back to ${prev:0:12}"
  else
    log "still unhealthy after rolling back to ${prev:0:12}"
  fi
  exit 1
}

deploy_env() {
  env_name="$1"
  local envfile="$here/env/${env_name}.env"
  [[ -f "$envfile" ]] || die "no env file $envfile"
  # shellcheck source=/dev/null
  . "$envfile"
  branch="${REELWRIGHT_BRANCH:?REELWRIGHT_BRANCH unset in $envfile}"
  port="${REELWRIGHT_PORT:?REELWRIGHT_PORT unset in $envfile}"
  dir="$root/$env_name"
  [[ -d "$dir/.git" ]] || die "no checkout at $dir (see docs/setup/deploy.md)"

  local data="$data_root/$env_name"
  mkdir -p "$data"
  exec 9>"$data/deploy.lock"
  if ! flock -n 9; then
    log "another deploy is running"
    exit 0
  fi

  check_disk "$dir"

  git -C "$dir" fetch --quiet origin "$branch"
  prev="$(git -C "$dir" rev-parse HEAD)"
  target="$(git -C "$dir" rev-parse "origin/$branch")"
  if [[ "$prev" == "$target" ]]; then
    log "up to date at ${prev:0:12}"
    return 0
  fi
  log "deploying $branch ${prev:0:12} -> ${target:0:12}"

  db="$data/reelwright.sqlite"
  backup="$db.pre-deploy"
  export REELWRIGHT_DB_URL="sqlite:///$db"
  if [[ -f "$db" ]]; then
    cp -p "$db" "$backup"
  elif [[ -f "$backup" ]]; then
    mv -f "$backup" "$backup.stale"
  fi

  if git -C "$dir" merge --ff-only --quiet "$target" && build && migrate && restart && healthy; then
    log "live at ${target:0:12}"
    return 0
  fi
  rollback
}

# Apply tailscale-serve.json: one tailnet-only HTTPS front per "host:port" Web entry.
apply_serve() {
  local ip https_port proxy
  ip="$(tailscale_ip)"
  while read -r https_port proxy; do
    [[ -n "$https_port" ]] || continue
    proxy="${proxy//__TAILSCALE_IP__/$ip}"
    tailscale serve --bg --https="$https_port" "$proxy"
    log "https :$https_port -> $proxy"
  done < <(
    python3 - "$here/tailscale-serve.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
if any(cfg.get("AllowFunnel", {}).values()):
    sys.exit("tailscale-serve.json must not enable Funnel")
for hostport, web in cfg["Web"].items():
    print(hostport.rsplit(":", 1)[1], web["Handlers"]["/"]["Proxy"])
PY
  )
}

case "${1:-}" in
  prod | staging) deploy_env "$1" ;;
  serve) apply_serve ;;
  *)
    echo "usage: $0 prod|staging|serve" >&2
    exit 2
    ;;
esac

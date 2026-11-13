# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy/deploy.sh and the systemd/Tailscale config it ships with.

The script runs for real against a throwaway origin and checkout; systemctl, curl, df, uv, pnpm and
tailscale are PATH shims that log their calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
SCRIPT = DEPLOY / "deploy.sh"
GB_KB = 1024 * 1024

SHIMS = {
    "systemctl": 'echo "systemctl $*" >> "$SHIM_LOG"\n',
    "curl": 'echo "curl $*" >> "$SHIM_LOG"\n[ "$(cat "$SHIM_DIR/healthy")" = 1 ]\n',
    "df": (
        'echo "df $*" >> "$SHIM_LOG"\n'
        'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
        'echo "/dev/x 999999999 1 $(cat "$SHIM_DIR/avail_kb") 99% /"\n'
    ),
    # `uv run ... rw migrate` writes a marker into the DB so rollback can be observed.
    "uv": (
        'echo "uv $*" >> "$SHIM_LOG"\n'
        'case "$*" in *"rw migrate"*) echo migrated > "${REELWRIGHT_DB_URL#sqlite:///}";; esac\n'
    ),
    "pnpm": 'echo "pnpm $*" >> "$SHIM_LOG"\n',
    "tailscale": (
        'echo "tailscale $*" >> "$SHIM_LOG"\nif [ "$1" = ip ]; then echo 100.64.0.7; fi\n'
    ),
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@dataclass
class Host:
    tmp: Path
    shims: Path
    checkout: Path
    seed: Path
    data: Path
    env: dict[str, str]

    def push_commit(self, name: str) -> str:
        (self.seed / name).write_text(name)
        _git(self.seed, "add", name)
        _git(self.seed, "commit", "-qm", name)
        _git(self.seed, "push", "-q", "origin", "HEAD:staging")
        return _git(self.seed, "rev-parse", "HEAD")

    def set(self, *, healthy: bool = True, free_gb: float = 50) -> None:
        (self.shims / "healthy").write_text("1" if healthy else "0")
        (self.shims / "avail_kb").write_text(str(int(free_gb * GB_KB)))

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        log = self.tmp / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        shim = shims / name
        shim.write_text("#!/usr/bin/env bash\n" + body)
        shim.chmod(0o755)
    git_env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key, value in git_env.items():
        monkeypatch.setenv(key, value)  # _git() inherits these
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    _git(seed, "checkout", "-qb", "staging")
    root = tmp_path / "deploys"
    data = tmp_path / "data"
    h = Host(
        tmp=tmp_path,
        shims=shims,
        checkout=root / "staging",
        seed=seed,
        data=data / "staging",
        env={
            **os.environ,
            "PATH": f"{shims}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "SHIM_DIR": str(shims),
            "SHIM_LOG": str(tmp_path / "calls.log"),
            "REELWRIGHT_DEPLOY_ROOT": str(root),
            "REELWRIGHT_DATA_ROOT": str(data),
            "REELWRIGHT_HEALTH_TIMEOUT": "1",
            "REELWRIGHT_HEALTH_INTERVAL": "0.1",
        },
    )
    h.env.pop("REELWRIGHT_DEPLOY_MIN_FREE_GB", None)
    h.push_commit("first")
    subprocess.run(
        ["git", "clone", "-q", "-b", "staging", str(origin), str(h.checkout)], check=True
    )
    h.set()
    return h


def test_healthy_deploy_fast_forwards_migrates_and_restarts(host: Host) -> None:
    new = host.push_commit("second")
    result = host.run("staging")
    assert result.returncode == 0, result.stderr
    assert host.head() == new
    calls = host.calls()
    assert any("rw migrate" in c for c in calls)
    assert (
        "systemctl --user restart reelwright-api@staging.service "
        "reelwright-scheduler@staging.service" in calls
    )
    assert any(c.startswith("curl") and "http://100.64.0.7:8481/healthz" in c for c in calls)
    assert (host.data / "reelwright.sqlite").read_text().strip() == "migrated"


def test_up_to_date_is_a_no_op(host: Host) -> None:
    result = host.run("staging")
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("systemctl") for c in host.calls())


def test_failed_healthz_rolls_back_to_previous_commit_and_db(host: Host) -> None:
    previous = host.head()
    host.data.mkdir(parents=True)
    (host.data / "reelwright.sqlite").write_text("before")
    host.push_commit("broken")
    host.set(healthy=False)
    result = host.run("staging")
    assert result.returncode != 0
    assert "rolling back" in result.stderr
    assert host.head() == previous
    assert (host.data / "reelwright.sqlite").read_text() == "before"
    restarts = [c for c in host.calls() if c.startswith("systemctl --user restart")]
    assert len(restarts) == 2  # the failed deploy, then the rollback


def test_disk_below_10_gb_aborts_before_any_change(host: Host) -> None:
    previous = host.head()
    host.push_commit("second")
    host.set(free_gb=9)
    result = host.run("staging")
    assert result.returncode != 0
    assert "need 10 GB" in result.stderr
    assert host.head() == previous
    assert _git(host.checkout, "rev-parse", "origin/staging") == previous  # never fetched
    assert [c.split()[0] for c in host.calls()] == ["df"]


@pytest.mark.parametrize(("free_gb", "ok"), [(10 - 1 / GB_KB, False), (10, True), (11, True)])
def test_default_disk_threshold_is_10_gb(host: Host, free_gb: float, ok: bool) -> None:
    host.push_commit("second")
    host.set(free_gb=free_gb)
    assert (host.run("staging").returncode == 0) is ok


def test_serve_applies_tailnet_only_https_fronts(host: Host) -> None:
    result = host.run("serve")
    assert result.returncode == 0, result.stderr
    calls = host.calls()
    assert "tailscale serve --bg --https=8490 http://100.64.0.7:8480" in calls
    assert "tailscale serve --bg --https=8491 http://100.64.0.7:8481" in calls
    assert not any("funnel" in c for c in calls)


def _env_file(name: str) -> dict[str, str]:
    lines = (DEPLOY / "env" / f"{name}.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_environments_map_branches_to_ports() -> None:
    assert _env_file("prod") == {
        "REELWRIGHT_ENV": "prod",
        "REELWRIGHT_BRANCH": "main",
        "REELWRIGHT_PORT": "8480",
    }
    assert _env_file("staging") == {
        "REELWRIGHT_ENV": "staging",
        "REELWRIGHT_BRANCH": "staging",
        "REELWRIGHT_PORT": "8481",
    }


def _seconds(span: str) -> int:
    m = re.fullmatch(r"(\d+)(s|min)", span)
    assert m, span
    return int(m[1]) * (60 if m[2] == "min" else 1)


def test_timer_polls_often_enough_to_be_live_within_5_minutes() -> None:
    timer = (DEPLOY / "systemd" / "reelwright-deploy.timer").read_text()
    interval = re.search(r"^OnUnitActiveSec=(\S+)$", timer, re.M)
    assert interval
    assert _seconds(interval[1]) <= 180  # leaves >= 2 min for build, migrate and health check
    service = (DEPLOY / "systemd" / "reelwright-deploy.service").read_text()
    assert "deploy.sh staging" in service and "deploy.sh prod" in service


def test_api_binds_only_the_tailscale_ip() -> None:
    unit = (DEPLOY / "systemd" / "reelwright-api@.service").read_text()
    (exec_start,) = re.findall(r"^ExecStart=(.*)$", unit, re.M)
    assert "tailscale ip -4" in exec_start
    assert '--host "$$ip"' in exec_start
    assert '--port "$$REELWRIGHT_PORT"' in exec_start
    for wildcard in ("0.0.0.0", "::", "127.0.0.1"):
        assert wildcard not in exec_start


def test_serve_config_never_enables_funnel() -> None:
    cfg = json.loads((DEPLOY / "tailscale-serve.json").read_text())
    assert not any(cfg.get("AllowFunnel", {}).values())
    proxies = {web["Handlers"]["/"]["Proxy"] for web in cfg["Web"].values()}
    assert proxies == {"http://__TAILSCALE_IP__:8480", "http://__TAILSCALE_IP__:8481"}


# Post-deploy checks registered in .forge/post-deploy-checks.json (R1 live half, R3 live half).


def _shim(host: Host, name: str, body: str) -> None:
    shim = host.shims / name
    shim.write_text("#!/usr/bin/env bash\n" + body)
    shim.chmod(0o755)


def test_post_deploy_checks_are_registered() -> None:
    checks = json.loads((REPO / ".forge" / "post-deploy-checks.json").read_text())
    assert "check-live staging" in checks["deploy-push-live-within-5min"]["run"]
    assert "check-live prod" in checks["deploy-push-live-within-5min"]["run"]
    assert checks["deploy-tailnet-only-refusal"]["run"] == "bash deploy/deploy.sh check-tailnet"


def test_check_live_passes_when_deployed_and_healthy(host: Host) -> None:
    result = host.run("check-live", "staging")
    assert result.returncode == 0, result.stderr
    assert any("http://100.64.0.7:8481/healthz" in c for c in host.calls())


def test_check_live_allows_a_fresh_push_inside_the_window(host: Host) -> None:
    host.push_commit("just-pushed")
    result = host.run("check-live", "staging")
    assert result.returncode == 0, result.stderr
    assert "inside the 5-minute window" in result.stderr


def test_check_live_fails_when_a_push_is_older_than_5_minutes(host: Host) -> None:
    old = "2000-01-01T00:00:00Z"
    (host.seed / "stale").write_text("stale")
    _git(host.seed, "add", "stale")
    subprocess.run(
        ["git", "commit", "-qm", "stale"],
        cwd=host.seed,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old},
    )
    _git(host.seed, "push", "-q", "origin", "HEAD:staging")
    result = host.run("check-live", "staging")
    assert result.returncode != 0
    assert "> 5 min" in result.stderr


def test_check_live_fails_when_healthz_is_down(host: Host) -> None:
    host.set(healthy=False)
    assert host.run("check-live", "staging").returncode != 0


def _tailnet_host(
    host: Host, listeners: str, *, funnel: bool = False, loopback: bool = False
) -> None:
    (host.shims / "listeners").write_text(listeners)
    _shim(
        host,
        "ss",
        'port="${!#}"; port="${port##*:}"\ngrep ":$port " "$SHIM_DIR/listeners" || true\n',
    )
    _shim(
        host,
        "curl",
        'echo "curl $*" >> "$SHIM_LOG"\n'
        f'case "$*" in *127.0.0.1*) exit {0 if loopback else 7};; esac\n',
    )
    status = '{"AllowFunnel": {"x:443": true}}' if funnel else "{}"
    _shim(
        host,
        "tailscale",
        f"case \"$1\" in ip) echo 100.64.0.7;; serve) echo '{status}';; esac\n",
    )


TAILNET_ONLY = (
    "LISTEN 0 2048 100.64.0.7:8480 0.0.0.0:* \nLISTEN 0 2048 100.64.0.7:8481 0.0.0.0:* \n"
)


def test_check_tailnet_passes_when_bound_only_to_the_tailscale_ip(host: Host) -> None:
    _tailnet_host(host, TAILNET_ONLY)
    result = host.run("check-tailnet")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("listeners", "funnel", "loopback", "why"),
    [
        (TAILNET_ONLY.replace("100.64.0.7:8481", "0.0.0.0:8481"), False, False, "0.0.0.0:8481"),
        (TAILNET_ONLY, True, False, "Funnel"),
        (TAILNET_ONLY, False, True, "loopback"),
        (TAILNET_ONLY.splitlines()[0] + "\n", False, False, "nothing listens on port 8481"),
    ],
)
def test_check_tailnet_fails_when_reachable_outside_the_tailnet(
    host: Host, listeners: str, funnel: bool, loopback: bool, why: str
) -> None:
    _tailnet_host(host, listeners, funnel=funnel, loopback=loopback)
    result = host.run("check-tailnet")
    assert result.returncode != 0
    assert why in result.stderr

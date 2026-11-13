# SPDX-License-Identifier: AGPL-3.0-or-later
"""GPU worker agent entry point (Windows native or WSL on MrDubey and Legion).

Thin wrapper around `rw worker run`: heartbeat GPU stats, pull jobs over HTTPS on the tailnet,
run them at low priority (and a RAM cap with --mem-cap-mb). Installed by install.ps1; the server
URL is a parameter and the token comes from REELWRIGHT_WORKER_TOKEN, never from a file here.

    python workers/gpu/agent.py --server https://<dev-server>.<tailnet>.ts.net [--cpu]
"""

from __future__ import annotations

import sys

from reelwright.cli.main import app


def main() -> None:
    app(["worker", "run", *sys.argv[1:]], prog_name="gpu-agent")


if __name__ == "__main__":
    main()

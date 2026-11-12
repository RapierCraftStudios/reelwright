# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print this machine's worker heartbeat (GPU stats, capabilities, yield flag) as JSON.

Install diagnostics: `python workers/gpu/probe.py` shows what the scheduler will see.
"""

from __future__ import annotations

import json

from reelwright.render.probe import probe


def main() -> None:
    print(json.dumps(probe(), indent=2))


if __name__ == "__main__":
    main()

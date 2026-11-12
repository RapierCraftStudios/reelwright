# SPDX-License-Identifier: AGPL-3.0-or-later
"""Footage watcher entry point on the capture PC (Windows native, MrDubey).

Thin wrapper around reelwright.ingest.watcher: polls the DJI and Recordly folders, waits until a
file stops growing, then posts its checksum, size, kind and duration to the API. Footage never
leaves this PC. Installed by install.ps1; the token comes from REELWRIGHT_WORKER_TOKEN.

    python workers/watcher/watch.py --server https://<dev-server>.<tailnet>.ts.net \\
        --dji D:\\DJI --recordly D:\\Recordly
"""

from __future__ import annotations

import sys

from reelwright.ingest.watcher import main

if __name__ == "__main__":
    main(sys.argv[1:])

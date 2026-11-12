# SPDX-License-Identifier: AGPL-3.0-or-later
"""QA job: pre-publish media QA of a final render, written as a JSON report.

Run by the worker agent (reelwright.render.worker, qa executors) under its resource policy. The
agent then uploads the report to ``/api/projects/{id}/renders/{rid}/qa``.

    python workers/gpu/jobs/qa.py --input D:/out/final.mp4 --output rw-render-qa-7.json

Exit 0 when the analysis ran, whether QA passed or not (the verdict is in the report); exit 1
when ffmpeg could not analyse the file.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reelwright.audio.loudness import Capture, capture_stderr
from reelwright.render.qa import QAFailed, run_qa


def main(argv: Sequence[str] | None = None, capture: Capture = capture_stderr) -> int:
    parser = argparse.ArgumentParser(prog="qa", description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_qa(args.input, capture=capture)
        Path(args.output).write_text(report.model_dump_json(), encoding="utf-8")
    except (QAFailed, OSError) as e:
        print(f"qa failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"qa {'passed' if report.passed else 'failed'}: {len(report.issues)} issue(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

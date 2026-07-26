"""Regenerate the published self-index snapshot the setup site renders.

Run this whenever the freshness gate fails, then commit the result. It also
resyncs the numbers the quality page and handoff publish, so closing the gate
is one command rather than a hunt for every copy of the same figure.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from repobrain.testing.snapshot import refresh_snapshot
from verify_setup_metrics import write_published_metrics

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "setup" / "graph-data.js")
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="regenerate the snapshot only, leaving published numbers untouched",
    )
    args = parser.parse_args()

    counts = refresh_snapshot(args.root, args.output)
    print(
        f"Refreshed {args.output}: {counts['files']} files, "
        f"{counts['nodes']} nodes, {counts['structural_edges']} structural edges"
    )
    # Only the default paths feed the published metrics; a snapshot exported
    # from somewhere else says nothing about this repository's numbers.
    if args.no_metrics or args.output != ROOT / "setup" / "graph-data.js":
        return 0
    measured = write_published_metrics()
    print(
        f"Published metrics updated: {measured['tests']} tests, "
        f"{measured['files']} files, {measured['graph']} graph facts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

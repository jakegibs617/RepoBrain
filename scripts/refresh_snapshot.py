"""Regenerate the published self-index snapshot the setup site renders.

Run this whenever the freshness gate fails, then commit ``setup/graph-data.js``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from repobrain.testing.snapshot import refresh_snapshot

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "setup" / "graph-data.js")
    args = parser.parse_args()

    counts = refresh_snapshot(args.root, args.output)
    print(
        f"Refreshed {args.output}: {counts['files']} files, "
        f"{counts['nodes']} nodes, {counts['structural_edges']} structural edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

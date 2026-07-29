"""Export an existing RepoBrain SQLite graph as a browser-safe static dataset.

To regenerate the site's own published snapshot, use ``refresh_snapshot.py``:
it indexes the working tree from scratch rather than trusting local state.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from repobrain.testing.snapshot import graph_payload, write_snapshot


def export_graph(database: Path, output: Path) -> None:
    write_snapshot(graph_payload(database), output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=Path(".repobrain/repobrain.sqlite")
    )
    parser.add_argument("--output", type=Path, default=Path("setup/graph-data.js"))
    args = parser.parse_args()
    export_graph(args.database, args.output)
    print(f"Exported {args.database} to {args.output}")


if __name__ == "__main__":
    main()

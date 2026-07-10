"""Export RepoBrain's local SQLite graph as a browser-safe static dataset."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def export_graph(database: Path, output: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        nodes = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, type, name, qualified_name, path, start_line, end_line,
                       language, confidence
                FROM nodes ORDER BY type, path, start_line, name
                """
            )
        ]
        node_ids = {node["id"] for node in nodes}
        edges = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source_node_id AS source, target_node_id AS target, type,
                       path, start_line, confidence, is_inferred
                FROM edges ORDER BY type, path, start_line
                """
            )
            if row["source"] in node_ids and row["target"] in node_ids
        ]

    payload = {
        "generated_from": str(database),
        "nodes": nodes,
        "edges": edges,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "window.REPOBRAIN_GRAPH = "
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )


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

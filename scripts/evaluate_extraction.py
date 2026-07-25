"""Index a repository and score it against a labeled graph-fact specification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.testing.accuracy import evaluate_facts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    parser.add_argument(
        "spec",
        type=Path,
        help="JSON object with expected and forbidden fact-key arrays",
    )
    args = parser.parse_args()

    try:
        specification = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read accuracy specification: {exc}")
    if not isinstance(specification, dict):
        parser.error("accuracy specification must be a JSON object")
    expected = specification.get("expected", [])
    forbidden = specification.get("forbidden", [])
    if not isinstance(expected, list) or not all(isinstance(x, str) for x in expected):
        parser.error("accuracy specification 'expected' must be an array of strings")
    if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
        parser.error("accuracy specification 'forbidden' must be an array of strings")

    with tempfile.TemporaryDirectory(prefix="repobrain-accuracy-") as temp_dir:
        with GraphStore(Path(temp_dir) / "repobrain.sqlite") as store:
            stats = Indexer(store).index(args.repository.resolve(), incremental=False)
            result = evaluate_facts(store, expected=expected, forbidden=forbidden)
    payload = result.to_dict()
    payload["warnings"] = stats.warnings
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.passed and not stats.warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())

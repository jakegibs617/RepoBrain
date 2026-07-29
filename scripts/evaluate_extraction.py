"""Score extraction against labeled ground truth.

Two forms:

    evaluate_extraction.py <repository> <spec.json>
    evaluate_extraction.py --corpus docs/evaluation/corpus.json

Both exit non-zero on a missing expected fact, a present forbidden fact, or an
extraction warning. Scores describe the labeled corpus, not repositories in
general (D38).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from repobrain.testing.accuracy import (
    evaluate_corpus,
    evaluate_repository,
    load_specification,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path, nargs="?")
    parser.add_argument(
        "spec",
        type=Path,
        nargs="?",
        help="JSON object with expected and forbidden fact-key arrays",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        help="JSON manifest scoring several labeled repositories in one run",
    )
    args = parser.parse_args()

    if args.corpus:
        if args.repository or args.spec:
            parser.error("--corpus scores a whole manifest; pass no positional paths")
        try:
            report = evaluate_corpus(args.corpus)
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            parser.error(f"cannot evaluate corpus: {exc}")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1

    if not args.repository or not args.spec:
        parser.error("pass a repository and a spec, or --corpus")
    try:
        expected, forbidden = load_specification(args.spec)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read accuracy specification: {exc}")

    result, warnings = evaluate_repository(
        args.repository, expected=expected, forbidden=forbidden
    )
    payload = result.to_dict()
    payload["warnings"] = warnings
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.passed and not warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())

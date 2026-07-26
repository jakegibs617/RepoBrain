"""Verify setup-site metrics against the artifacts and tests they describe."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from repobrain.testing.snapshot import read_snapshot, snapshot_drift, structural_counts


ROOT = Path(__file__).resolve().parents[1]
GRAPH_DATA = ROOT / "setup" / "graph-data.js"
QUALITY_PAGE = ROOT / "setup" / "evaluation.html"
HANDOFF = ROOT / "AGENT_HANDOFF.md"


def _published_metric(page: str, name: str) -> int:
    match = re.search(
        rf'data-quality-metric="{re.escape(name)}"\s+data-value="(\d+)"',
        page,
    )
    if not match:
        raise ValueError(f"missing published metric: {name}")
    return int(match.group(1))


def _collected_tests() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if not match:
        raise ValueError("pytest output did not report a collected-test count")
    return int(match.group(1))


def _verify_snapshot_freshness(snapshot: dict) -> bool:
    """Fail when the committed self-index no longer matches the working tree."""
    drift = snapshot_drift(ROOT, snapshot)
    if not drift:
        return True
    for metric, (shown, measured) in drift.items():
        print(
            f"{metric}: setup/graph-data.js publishes {shown}, "
            f"indexing this tree measures {measured}",
            file=sys.stderr,
        )
    print(
        "setup/graph-data.js is stale: run scripts/refresh_snapshot.py and "
        "commit the regenerated snapshot.",
        file=sys.stderr,
    )
    return False


def main() -> int:
    page = QUALITY_PAGE.read_text(encoding="utf-8")
    handoff = HANDOFF.read_text(encoding="utf-8")
    graph = read_snapshot(GRAPH_DATA)
    nodes, edges = graph["nodes"], graph["edges"]

    actual = {
        "tests": _collected_tests(),
        "files": structural_counts(graph)["files"],
        "graph": len(nodes) + len(edges),
    }
    published = {name: _published_metric(page, name) for name in actual}
    handoff_match = re.search(r"^- (\d+) tests are collected;", handoff, re.MULTILINE)
    if not handoff_match:
        raise ValueError("AGENT_HANDOFF.md is missing its current collected-test metric")
    published["handoff_tests"] = int(handoff_match.group(1))
    actual["handoff_tests"] = actual["tests"]
    mismatches = {
        name: (published[name], value)
        for name, value in actual.items()
        if published[name] != value
    }
    for name, (shown, measured) in mismatches.items():
        print(
            f"{name}: setup/evaluation.html publishes {shown}, measured {measured}",
            file=sys.stderr,
        )
    # Both checks always run: a stale snapshot also skews the published metrics,
    # and reporting only the first failure would hide half the work to do.
    if not _verify_snapshot_freshness(graph) or mismatches:
        return 1

    print(
        "quality metrics verified: "
        f"{actual['tests']} tests, {actual['files']} files, "
        f"{len(nodes)} nodes + {len(edges)} edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

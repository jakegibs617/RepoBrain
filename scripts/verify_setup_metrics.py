"""Verify setup-site metrics against the artifacts and tests they describe.

``--write`` rewrites the published numbers instead of only reporting them, so
a failing gate costs one command rather than hunting every hand-maintained
copy of the same figure.
"""

from __future__ import annotations

import argparse
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


def _rewrite(
    pattern: str, replacement: str, text: str, what: str, flags: int = 0
) -> str:
    """Substitute exactly what the checker reads back, or refuse the edit."""
    updated, count = re.subn(pattern, replacement, text, flags=flags)
    if not count:
        raise ValueError(f"cannot update {what}: the published copy is absent")
    return updated


def sync_metrics(
    page: str, handoff: str, metrics: dict[str, int], *, nodes: int, edges: int
) -> tuple[str, str]:
    """Rewrite every published copy of the measured numbers.

    Each metric appears twice on the quality page — the machine-readable
    ``data-value`` the checker parses and the human-readable ``<strong>`` a
    reader sees — plus a node/edge breakdown and one line in the handoff.
    Updating them by hand is where drift got in, so the checker owns the edit.
    """
    for name, value in metrics.items():
        article = rf'(data-quality-metric="{re.escape(name)}"\s+data-value=")\d+("[^>]*>)'
        page = _rewrite(article, rf"\g<1>{value}\g<2>", page, f"{name} data-value")
        shown = rf'(data-quality-metric="{re.escape(name)}"[^>]*>[^\n]*?<strong>)[\d,]+(</strong>)'
        page = _rewrite(shown, rf"\g<1>{value:,}\g<2>", page, f"{name} display value")

    breakdown = (
        r'(data-quality-metric="graph"[^>]*>[^\n]*?<small>)'
        r"[\d,]+ nodes \+ [\d,]+ edges(</small>)"
    )
    if "graph" in metrics:
        page = _rewrite(
            breakdown, rf"\g<1>{nodes:,} nodes + {edges:,} edges\g<2>", page,
            "graph node/edge breakdown",
        )
    if "tests" in metrics:
        handoff = _rewrite(
            r"^- \d+ tests are collected;", f"- {metrics['tests']} tests are collected;",
            handoff, "handoff collected-test count", flags=re.MULTILINE,
        )
    return page, handoff


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


def write_published_metrics() -> dict[str, int]:
    """Bring every published number in line with what is measured now."""
    graph = read_snapshot(GRAPH_DATA)
    nodes, edges = graph["nodes"], graph["edges"]
    measured = {
        "tests": _collected_tests(),
        "files": structural_counts(graph)["files"],
        "graph": len(nodes) + len(edges),
    }
    page, handoff = sync_metrics(
        QUALITY_PAGE.read_text(encoding="utf-8"),
        HANDOFF.read_text(encoding="utf-8"),
        measured,
        nodes=len(nodes),
        edges=len(edges),
    )
    QUALITY_PAGE.write_text(page, encoding="utf-8")
    HANDOFF.write_text(handoff, encoding="utf-8")
    return measured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the published numbers instead of only checking them",
    )
    args = parser.parse_args()
    if args.write:
        measured = write_published_metrics()
        print(
            "published metrics updated: "
            f"{measured['tests']} tests, {measured['files']} files, "
            f"{measured['graph']} graph facts"
        )

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

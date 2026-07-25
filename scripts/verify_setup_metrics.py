"""Verify setup-site metrics against the artifacts and tests they describe."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys


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


def _graph() -> dict[str, object]:
    source = GRAPH_DATA.read_text(encoding="utf-8")
    prefix = "window.REPOBRAIN_GRAPH = "
    if not source.startswith(prefix) or not source.rstrip().endswith(";"):
        raise ValueError(f"unexpected graph-data.js format: {GRAPH_DATA}")
    return json.loads(source[len(prefix) :].strip().removesuffix(";"))


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


def main() -> int:
    page = QUALITY_PAGE.read_text(encoding="utf-8")
    handoff = HANDOFF.read_text(encoding="utf-8")
    graph = _graph()
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("graph-data.js must contain node and edge lists")

    actual = {
        "tests": _collected_tests(),
        "files": sum(node.get("type") == "File" for node in nodes),
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
    if mismatches:
        for name, (shown, measured) in mismatches.items():
            print(
                f"{name}: setup/evaluation.html publishes {shown}, measured {measured}",
                file=sys.stderr,
            )
        return 1

    print(
        "quality metrics verified: "
        f"{actual['tests']} tests, {actual['files']} files, "
        f"{len(nodes)} nodes + {len(edges)} edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

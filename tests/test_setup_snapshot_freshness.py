import json
from pathlib import Path

import pytest

from repobrain.testing.snapshot import (
    graph_payload,
    head_commit,
    index_repository_payload,
    read_snapshot,
    refresh_snapshot,
    snapshot_drift,
    structural_counts,
    write_snapshot,
)


def test_freshly_indexed_snapshot_reports_no_drift(small_app, tmp_path):
    snapshot = index_repository_payload(small_app)

    assert snapshot_drift(small_app, snapshot) == {}


def test_dropping_a_node_from_the_snapshot_is_detected(small_app, tmp_path):
    snapshot = index_repository_payload(small_app)
    dropped = next(node for node in snapshot["nodes"] if node["type"] == "Function")
    snapshot["nodes"] = [n for n in snapshot["nodes"] if n["id"] != dropped["id"]]

    drift = snapshot_drift(small_app, snapshot)

    published, measured = drift["nodes"]
    assert published == measured - 1


def test_dropping_a_file_from_the_snapshot_is_detected(small_app, tmp_path):
    snapshot = index_repository_payload(small_app)
    dropped = next(node for node in snapshot["nodes"] if node["type"] == "File")
    snapshot["nodes"] = [n for n in snapshot["nodes"] if n["id"] != dropped["id"]]

    drift = snapshot_drift(small_app, snapshot)

    assert "files" in drift
    assert drift["files"][0] == drift["files"][1] - 1


def test_history_edges_are_excluded_from_the_comparison(small_app, tmp_path):
    # CO_CHANGED_WITH is derived from git history, so it churns on every commit.
    # A snapshot must not be considered stale because history moved on.
    snapshot = index_repository_payload(small_app)
    node_id = snapshot["nodes"][0]["id"]
    snapshot["edges"].append({
        "source": node_id, "target": node_id, "type": "CO_CHANGED_WITH",
        "path": "", "start_line": None, "confidence": 0.5, "is_inferred": 1,
    })

    assert snapshot_drift(small_app, snapshot) == {}


def test_structural_counts_ignore_history_edges():
    payload = {
        "nodes": [{"id": 1, "type": "File"}, {"id": 2, "type": "Function"}],
        "edges": [
            {"type": "CALLS"},
            {"type": "CO_CHANGED_WITH"},
        ],
    }

    assert structural_counts(payload) == {
        "files": 1, "nodes": 2, "structural_edges": 1,
    }


def test_written_snapshot_is_browser_loadable_and_carries_provenance(
    small_app, tmp_path
):
    snapshot = index_repository_payload(
        small_app, commit="abc1234", generated_at="2026-07-25"
    )
    output = tmp_path / "graph-data.js"

    write_snapshot(snapshot, output)

    source = output.read_text(encoding="utf-8")
    assert source.startswith("window.REPOBRAIN_GRAPH = ")
    payload = json.loads(source[len("window.REPOBRAIN_GRAPH = "):].strip()[:-1])
    assert payload["commit"] == "abc1234"
    assert payload["generated_at"] == "2026-07-25"
    assert payload["nodes"] and payload["edges"]


def test_graph_payload_drops_edges_whose_endpoints_were_not_exported(
    small_app, tmp_path
):
    database = tmp_path / "graph.sqlite"
    index_repository_payload(small_app, database=database)
    payload = graph_payload(database)
    exported = {node["id"] for node in payload["nodes"]}

    assert all(
        edge["source"] in exported and edge["target"] in exported
        for edge in payload["edges"]
    )


def test_refresh_writes_a_snapshot_that_matches_the_tree_it_indexed(
    small_app, tmp_path
):
    output = tmp_path / "graph-data.js"

    counts = refresh_snapshot(small_app, output)

    payload = read_snapshot(output)
    assert structural_counts(payload) == counts
    assert snapshot_drift(small_app, payload) == {}
    assert payload["generated_at"]


def test_head_commit_is_none_outside_a_git_checkout(tmp_path):
    assert head_commit(tmp_path) is None


def test_head_commit_reads_this_repository():
    commit = head_commit(Path(__file__).resolve().parents[1])

    assert commit and len(commit) >= 7


@pytest.mark.parametrize("body", [
    "not an assignment",
    "window.REPOBRAIN_GRAPH = [1,2,3];",
    'window.REPOBRAIN_GRAPH = {"nodes":[]};',
])
def test_unusable_snapshots_are_rejected_rather_than_half_parsed(tmp_path, body):
    path = tmp_path / "graph-data.js"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError):
        read_snapshot(path)


def test_published_repository_snapshot_is_current():
    """The committed self-index must match what RepoBrain sees at HEAD today."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "setup" / "graph-data.js").read_text(encoding="utf-8")
    prefix = "window.REPOBRAIN_GRAPH = "
    snapshot = json.loads(source[len(prefix):].strip()[:-1])

    drift = snapshot_drift(root, snapshot)

    assert drift == {}, (
        "setup/graph-data.js is stale; run scripts/refresh_snapshot.py "
        f"and commit the result. Drift (published, measured): {drift}"
    )

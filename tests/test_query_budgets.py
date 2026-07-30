"""Response size is a correctness property for agent-facing query tools.

An answer that cannot fit the context window of the consumer it was built for
is not a worse answer, it is not an answer. These tests pin the ceiling at the
MCP seam -- the surface an agent actually calls -- on a deterministic synthetic
repository, so the assertions describe a fixed corpus rather than whatever this
project's own graph happens to weigh today.
"""
from __future__ import annotations

import json

import pytest

from repobrain.graph.queries import DEFAULT_QUERY_BUDGET
from repobrain.indexing.indexer import Indexer
from repobrain.graph.store import GraphStore
from repobrain.mcp_server import RepoBrainTools
from repobrain.testing.synthetic_repo import generate_synthetic_repo

# Large enough that an unbounded traversal is genuinely unusable: the audit
# measured 62k-104k tokens for a two-hop trace on a comparable graph.
N_MODULES = 300


@pytest.fixture
def hub_repo(tmp_path):
    root = tmp_path / "repo"
    info = generate_synthetic_repo(root, n_modules=N_MODULES)
    with GraphStore(root / ".repobrain" / "repobrain.sqlite") as store:
        Indexer(store).index(root)
    return info


def _tokens(payload: dict) -> int:
    return -(-len(json.dumps(payload)) // 4)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda t: t.trace_symbol("Service0", auto_index=False),
                     id="trace_symbol"),
        pytest.param(lambda t: t.impact_analysis("pkg000/module_0.py", auto_index=False),
                     id="impact_analysis"),
        pytest.param(lambda t: t.trace_data_flow("helper_0", auto_index=False),
                     id="trace_data_flow"),
    ],
)
def test_default_call_stays_within_the_default_budget(hub_repo, call):
    tools = RepoBrainTools(hub_repo.root)

    result = call(tools)

    assert result["status"] in {"ok", "not_found"}
    assert _tokens(result) <= DEFAULT_QUERY_BUDGET


def test_trimming_is_reported_rather_than_silent(hub_repo):
    # A caller that cannot tell a trimmed result from a complete one will treat
    # it as complete, which is the confidently-wrong answer the freshness gate
    # exists to prevent.
    tools = RepoBrainTools(hub_repo.root)

    result = tools.trace_symbol("Service0", depth=3, budget=400, auto_index=False)

    truncation = result["truncation"]
    assert truncation["applied"] is True
    assert truncation["budget"] == 400
    assert sum(truncation["dropped"].values()) > 0
    assert _tokens(result) <= 400


def test_an_untruncated_result_says_so(hub_repo):
    tools = RepoBrainTools(hub_repo.root)

    result = tools.trace_symbol("Service0", depth=0, auto_index=False)

    assert result["truncation"]["applied"] is False
    assert result["truncation"]["dropped"] == {}

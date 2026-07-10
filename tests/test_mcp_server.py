import json

import pytest

from repobrain.mcp_server import RepoBrainTools, _safe


def test_transport_independent_mcp_tools_return_json_safe_results(small_app):
    tools = RepoBrainTools(small_app)
    indexed = tools.index_repo()
    assert indexed["status"] == "ok"
    assert indexed["files_scanned"] > 0

    results = tools.search_project("UserRepository")
    assert results["status"] == "ok"
    json.dumps(results)

    project = tools.explain_project("architecture")
    assert project["files"] > 0
    json.dumps(project)

    symbols = tools.find_symbol("UserRepository")
    assert symbols["symbols"]
    json.dumps(symbols)

    flow = tools.trace_data_flow("POST /api/users", direction="out")
    assert flow["status"] == "ok"
    assert flow["flow"]["start"]["type"] == "Route"
    impact = tools.impact_analysis("app/services/user_service.py")
    assert impact["status"] == "ok"
    assert impact["impact"]["high_confidence"]


def test_mcp_index_is_confined_to_server_root(small_app, tmp_path):
    tools = RepoBrainTools(small_app)
    with pytest.raises(ValueError, match="scoped to"):
        tools.index_repo(str(tmp_path))


def test_mcp_memory_round_trip_is_json_safe(tmp_path):
    tools = RepoBrainTools(tmp_path)
    written = tools.write_agent_memory("Added MCP.", decisions=["Use stdio."])
    read = tools.read_agent_memory(topic="stdio")
    assert written["status"] == "ok"
    assert read["entries"][0]["decisions"] == ["Use stdio."]
    json.dumps(_safe(read))

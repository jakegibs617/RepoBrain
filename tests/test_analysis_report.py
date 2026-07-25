from repobrain.graph.queries import impact_analysis, trace_data_flow
from repobrain.reporting import generate_report, project_overview


def test_route_to_service_data_flow(indexer, store, node_app):
    indexer.index(node_app)
    result = trace_data_flow(store, "POST /api/users", depth=3, direction="out")
    assert result and result["start"]["type"] == "Route"
    names = {node["name"] for node in result["nodes"]}
    assert "createUser" in names
    assert any(edge["type"] == "HANDLES_ROUTE" for edge in result["edges"])
    assert any(edge["type"] == "CALLS" for edge in result["edges"])


def test_python_route_detection(indexer, store, small_app):
    indexer.index(small_app)
    result = trace_data_flow(store, "POST /api/users", depth=2, direction="out")
    assert result
    assert {node["name"] for node in result["nodes"]} >= {
        "POST /api/users", "create_user_route", "handle_create_user"
    }


def test_impact_buckets_tests_and_docs(indexer, store, node_app):
    indexer.index(node_app)
    result = impact_analysis(store, "src/services/userService.js")
    assert result
    impacted = result["high_confidence"] + result["medium_confidence"] + result["low_confidence"]
    assert any(item["node"]["path"] == "src/routes/users.js" for item in impacted)
    assert any(item["node"]["type"] == "TestCase" for item in result["recommended_tests"])


def test_project_overview_and_report(indexer, store, node_app):
    indexer.index(node_app)
    overview = project_overview(store)
    assert overview["files"] > 0
    assert any(item["name"] == "POST /api/users" for item in overview["entrypoints"])
    assert {item["type"] for item in overview["entrypoints"]} == {"Route"}
    markdown, html = generate_report(store, node_app)
    report = markdown.read_text()
    assert "## Detected Routes" in report
    assert "Detected Config" in report
    assert html.read_text().startswith("<!doctype html>")


def test_report_honestly_shows_when_no_routes_are_detected(
    indexer, store, docs_app
):
    indexer.index(docs_app)

    overview = project_overview(store)
    markdown, _ = generate_report(store, docs_app)
    routes_section = markdown.read_text().split(
        "## Detected Routes\n", 1
    )[1].split("\n## ", 1)[0]

    assert overview["entrypoints"] == []
    assert "- None detected" in routes_section

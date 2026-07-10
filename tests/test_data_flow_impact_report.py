from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.queries import impact_analysis, trace_data_flow
from repobrain.reporting import generate_report, project_overview


def test_python_route_data_flow_reaches_service_and_repository(indexer, store, small_app):
    indexer.index(small_app)
    result = trace_data_flow(store, "POST /api/users", depth=5, direction="out")
    assert result is not None
    assert result["start"]["type"] == "Route"
    names = {node["name"] for node in result["nodes"]}
    assert {"create_user_route", "handle_create_user", "create_user", "insert"} <= names


def test_js_route_data_flow_starts_at_route(indexer, store, node_app):
    indexer.index(node_app)
    result = trace_data_flow(store, "POST /api/users", depth=3, direction="out")
    assert result is not None
    assert any(edge["type"] == "HANDLES_ROUTE" for edge in result["edges"])
    assert any(node["name"] == "createUser" for node in result["nodes"])


def test_impact_analysis_includes_importers_docs_and_tests(indexer, store, small_app):
    indexer.index(small_app)
    result = impact_analysis(store, "app/services/user_service.py")
    assert result is not None
    paths = {item["node"]["path"] for key in ("high_confidence", "medium_confidence", "low_confidence") for item in result[key]}
    assert "app/handlers/user_handler.py" in paths
    assert "README.md" in paths


def test_project_overview_and_report(indexer, store, small_app):
    indexer.index(small_app)
    overview = project_overview(store)
    assert overview["files"] > 0
    assert any(item["name"] == "POST /api/users" for item in overview["entrypoints"])
    md, html = generate_report(store, small_app)
    assert md.exists() and html.exists()
    assert "Graph Stats" in md.read_text()


def test_new_cli_commands(indexer, store, small_app):
    # Build the fixture's own DB, since CLI resolves it from --path.
    runner = CliRunner()
    assert runner.invoke(main, ["index", str(small_app)]).exit_code == 0
    assert runner.invoke(main, ["explain", "project", "--path", str(small_app), "--json"]).exit_code == 0
    assert runner.invoke(main, ["trace", "data-flow", "POST /api/users", "--path", str(small_app), "--json"]).exit_code == 0
    assert runner.invoke(main, ["impact", "app/services/user_service.py", "--path", str(small_app), "--json"]).exit_code == 0
    assert runner.invoke(main, ["report", "--path", str(small_app)]).exit_code == 0

import json

from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.queries import trace_config
from repobrain.parsers.config_parser import EnvFileParser


def _types(store, table="nodes"):
    return store.counts_by_type(table)


def test_yaml_adapters_and_env_definitions(indexer, store, tmp_path):
    from conftest import copy_fixture
    repo = copy_fixture("config_app", tmp_path)
    stats = indexer.index(repo)
    assert not stats.warnings
    types = _types(store)
    assert types["ConfigFile"] == 4
    assert types["ConfigKey"] > 10
    assert types["GitHubWorkflow"] == 1
    assert types["GitHubJob"] == 1
    assert types["GitHubStep"] == 1
    assert types["DockerService"] == 2
    assert types["DockerImage"] == 2
    assert types["KubernetesResource"] == 1
    assert types["SecretRef"] == 1
    edges = _types(store, "edges")
    assert edges["SETS_ENV"] >= 4
    assert edges["DEPENDS_ON"] == 1
    assert edges["USES_IMAGE"] >= 3


def test_trace_config_joins_definitions_and_code_reads(indexer, store, tmp_path):
    from conftest import copy_fixture
    repo = copy_fixture("config_app", tmp_path)
    indexer.index(repo)
    traced = trace_config(store, "DATABASE_URL")
    definition_paths = {item["path"] for item in traced["definitions"]}
    assert {"docker-compose.yml", "k8s/deployment.yml"} <= definition_paths
    assert ".env.example" not in definition_paths
    assert any(item["path"] == "app.py" for item in traced["usages"])


def test_trace_config_cli_json(tmp_path):
    from conftest import copy_fixture
    repo = copy_fixture("config_app", tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["index", str(repo)]).exit_code == 0
    result = runner.invoke(main, ["trace", "config", "DATABASE_URL", "--path", str(repo), "--json"])
    assert result.exit_code == 0
    assert '"definitions"' in result.output
    assert '"usages"' in result.output


def test_dotenv_parser_keeps_key_and_line_but_never_value(store):
    canary = "sk_live_AUDITCANARY_parser_9f3d2b"
    parsed = EnvFileParser().parse(
        ".env.example",
        f"# intentionally dangerous template\nSTRIPE_SECRET_KEY={canary}\n",
    )

    assert parsed.fts_rows == []
    key = next(node for node in parsed.nodes if node.type == "ConfigKey")
    assert key.name == "STRIPE_SECRET_KEY"
    assert key.start_line == 2
    assert key.end_line == 2
    assert key.metadata == {"format": "dotenv"}
    assert all("value" not in edge.metadata for edge in parsed.edges)
    assert canary not in json.dumps(
        {
            "nodes": [node.metadata for node in parsed.nodes],
            "edges": [edge.metadata for edge in parsed.edges],
            "fts": [row.content for row in parsed.fts_rows],
        },
        sort_keys=True,
    )

    with store.conn:
        store.upsert_nodes(parsed.nodes)
        store.upsert_edges(parsed.edges)
        store.add_fts_rows(parsed.fts_rows)
    traced = trace_config(store, "STRIPE_SECRET_KEY")
    assert traced["definitions"][0]["path"] == ".env.example"
    assert traced["definitions"][0]["start_line"] == 2
    assert traced["definitions"][0]["metadata"] == {}
    assert canary not in json.dumps(traced, sort_keys=True)

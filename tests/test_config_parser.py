from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.queries import trace_config


def _types(store, table="nodes"):
    return store.counts_by_type(table)


def test_yaml_adapters_and_env_definitions(indexer, store, tmp_path):
    from conftest import copy_fixture
    repo = copy_fixture("config_app", tmp_path)
    stats = indexer.index(repo)
    assert not stats.warnings
    types = _types(store)
    assert types["ConfigFile"] == 5
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
    assert {".env.example", "docker-compose.yml", "k8s/deployment.yml"} <= definition_paths
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

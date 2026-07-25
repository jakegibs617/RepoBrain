import json
from pathlib import Path

from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.queries import trace_config
from repobrain.parsers.config_parser import EnvFileParser
from repobrain.parsers.yaml_parser import YamlParser


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


def test_yaml_parser_keeps_structure_but_never_scalar_or_env_values():
    canaries = (
        "YAMLPASSWORDCANARY",
        "COMPOSEENVCANARY",
        "COMPOSECOMMANDCANARY",
    )
    parsed = YamlParser().parse(
        "docker-compose.yml",
        f"""password: {canaries[0]}
services:
  api:
    image: python:3.12-slim
    command: ["sh", "-c", "echo {canaries[2]}"]
    environment:
      APP_PASSWORD: {canaries[1]}
      TOKEN: ${{TOKEN:-{canaries[0]}}}
""",
    )

    assert not parsed.fts_rows
    password = next(
        node
        for node in parsed.nodes
        if node.type == "ConfigKey" and node.qualified_name == "password"
    )
    assert password.start_line == 1
    assert password.metadata == {"value_type": "str"}
    serialized = json.dumps(
        {
            "nodes": [
                {"name": node.name, "metadata": node.metadata}
                for node in parsed.nodes
            ],
            "edges": [edge.metadata for edge in parsed.edges],
            "fts": [row.content for row in parsed.fts_rows],
            "warnings": parsed.warnings,
        },
        sort_keys=True,
    )
    assert all(canary not in serialized for canary in canaries)
    interpolation_edges = [
        edge for edge in parsed.edges
        if edge.type == "SETS_ENV" and edge.metadata.get("interpolation")
    ]
    assert interpolation_edges
    assert interpolation_edges[0].metadata == {"interpolation": True}


def test_yaml_canaries_never_reach_database_search_or_trace(indexer, store, tmp_path):
    canaries = {
        "yaml": "YAMLSCALARCANARY",
        "compose_env": "COMPOSEENVCANARY",
        "compose_command": "COMPOSECOMMANDCANARY",
        "k8s_secret": "K8SSECRETCANARY",
        "k8s_env": "K8SENVCANARY",
        "gha_env": "GHAENVCANARY",
        "gha_run": "GHARUNCANARY",
    }
    repo = tmp_path / "yaml-secret-repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "k8s").mkdir()
    (repo / "settings.yml").write_text(
        f"database:\n  password: {canaries['yaml']}\n",
        encoding="utf-8",
    )
    (repo / "docker-compose.yml").write_text(
        f"""services:
  api:
    image: python:3.12-slim
    command: ["sh", "-c", "echo {canaries['compose_command']}"]
    environment:
      APP_PASSWORD: {canaries['compose_env']}
""",
        encoding="utf-8",
    )
    (repo / "k8s" / "resources.yml").write_text(
        f"""apiVersion: v1
kind: Secret
metadata:
  name: app-secret
stringData:
  password: {canaries['k8s_secret']}
---
apiVersion: v1
kind: Pod
metadata:
  name: api
spec:
  containers:
    - name: api
      image: python:3.12-slim
      env:
        - name: DIRECT_TOKEN
          value: {canaries['k8s_env']}
        - name: APP_PASSWORD
          valueFrom:
            secretKeyRef:
              name: app-secret
              key: password
""",
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        f"""name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      CI_TOKEN: {canaries['gha_env']}
    steps:
      - name: test
        run: echo {canaries['gha_run']}
""",
        encoding="utf-8",
    )

    stats = indexer.index(repo)
    assert not stats.warnings
    for canary in canaries.values():
        assert not store.conn.execute(
            "SELECT 1 FROM content_fts WHERE content_fts MATCH ?",
            (canary,),
        ).fetchone()
        assert not store.conn.execute(
            "SELECT 1 FROM nodes WHERE metadata_json LIKE ?",
            (f"%{canary}%",),
        ).fetchone()
        assert not store.conn.execute(
            "SELECT 1 FROM edges WHERE metadata_json LIKE ?",
            (f"%{canary}%",),
        ).fetchone()

    for name in ("password", "APP_PASSWORD", "DIRECT_TOKEN", "CI_TOKEN"):
        traced = trace_config(store, name)
        assert all(
            canary not in json.dumps(traced, sort_keys=True)
            for canary in canaries.values()
        )

    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = Path(store.db_path).read_bytes()
    for canary in canaries.values():
        assert canary.encode() not in database_bytes

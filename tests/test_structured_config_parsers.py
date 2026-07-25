from pathlib import Path

from repobrain.graph.store import GraphStore
from repobrain.graph.schema import EdgeType, NodeType
from repobrain.indexing.indexer import Indexer
from repobrain.parsers.base import default_registry
from repobrain.parsers.dockerfile_parser import DockerfileParser
from repobrain.parsers.structured_config_parser import JsonParser, TomlParser


def _nodes(result, type_):
    return [node for node in result.nodes if node.type == type_]


def test_json_parser_extracts_grounded_keys_without_values():
    content = """{
  "server": {
    "port": 8080,
    "features": ["search", "graph"]
  },
  "enabled": true
}
"""
    result = JsonParser().parse("config/settings.json", content)
    keys = {node.qualified_name: node for node in _nodes(result, NodeType.CONFIG_KEY)}

    assert keys["server"].start_line == 2
    assert keys["server.port"].start_line == 3
    assert keys["server.features"].metadata == {"value_type": "array"}
    assert keys["enabled"].metadata == {"value_type": "boolean"}
    assert all("value" not in node.metadata for node in keys.values())
    assert not result.fts_rows
    assert len(_nodes(result, NodeType.CONFIG_FILE)) == 1
    assert not result.warnings


def test_json_parser_warns_without_inventing_keys_for_invalid_input():
    result = JsonParser().parse("broken.json", '{"valid": 1,}')

    assert len(_nodes(result, NodeType.CONFIG_FILE)) == 1
    assert not _nodes(result, NodeType.CONFIG_KEY)
    assert result.warnings and "invalid JSON" in result.warnings[0]


def test_toml_parser_extracts_tables_and_dotted_keys_with_lines():
    content = """title = "RepoBrain"

[server]
port = 8080
tls.enabled = true
"""
    result = TomlParser().parse("pyproject.toml", content)
    keys = {node.qualified_name: node for node in _nodes(result, NodeType.CONFIG_KEY)}

    assert keys["title"].start_line == 1
    assert keys["server"].start_line == 3
    assert keys["server.port"].start_line == 4
    assert keys["server.tls.enabled"].start_line == 5
    assert keys["server.port"].metadata == {"value_type": "int"}
    assert all("value" not in node.metadata for node in keys.values())
    assert not result.fts_rows


def test_toml_parser_warns_without_inventing_keys_for_invalid_input():
    result = TomlParser().parse("broken.toml", "[server\nport = 80")

    assert len(_nodes(result, NodeType.CONFIG_FILE)) == 1
    assert not _nodes(result, NodeType.CONFIG_KEY)
    assert result.warnings and "invalid TOML" in result.warnings[0]


def test_dockerfile_parser_extracts_instructions_and_concrete_base_images():
    content = """# syntax=docker/dockerfile:1
ARG BASE=python:3.12
FROM python:3.12-slim AS build
WORKDIR /app
RUN python -m compileall \\
    src
FROM $BASE AS runtime
ENV TOKEN=do-not-store
"""
    result = DockerfileParser().parse("containers/Dockerfile", content)
    keys = _nodes(result, NodeType.CONFIG_KEY)
    images = _nodes(result, NodeType.DOCKER_IMAGE)

    assert [node.name for node in keys] == [
        "ARG", "FROM", "WORKDIR", "RUN", "FROM", "ENV"
    ]
    assert keys[1].start_line == 3
    assert keys[3].start_line == 5
    assert keys[1].metadata["stage"] == "build"
    assert "arguments" not in keys[0].metadata
    assert "arguments" not in keys[-1].metadata
    assert all("do-not-store" not in str(node.metadata) for node in keys)
    assert not result.fts_rows
    assert [node.name for node in images] == ["python:3.12-slim"]
    assert any(edge.type == EdgeType.USES_IMAGE for edge in result.edges)
    assert not result.warnings


def test_default_registry_uses_dedicated_structured_parsers_only_for_their_inputs():
    registry = default_registry()

    assert any(isinstance(parser, JsonParser) for parser in registry.parsers_for("a.json", "json"))
    assert any(isinstance(parser, TomlParser) for parser in registry.parsers_for("a.toml", "toml"))
    assert any(
        isinstance(parser, DockerfileParser)
        for parser in registry.parsers_for("docker/Dockerfile.dev", None)
    )
    assert not any(
        isinstance(parser, DockerfileParser)
        for parser in registry.parsers_for("docs/dockerfile.md", "markdown")
    )


def test_structured_config_canaries_never_reach_database_or_search(tmp_path):
    json_canary = "SUPERSECRETJSONCANARY"
    toml_canary = "SUPERSECRETTOMLCANARY"
    docker_canary = "SUPERSECRETDOCKERCANARY"
    (tmp_path / "settings.json").write_text(
        f'{{"api": {{"token": "{json_canary}"}}}}\n',
        encoding="utf-8",
    )
    (tmp_path / "settings.toml").write_text(
        f'[api]\ntoken = "{toml_canary}"\n',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile.dev").write_text(
        f"FROM python:3.12-slim\nARG TOKEN={docker_canary}\n"
        f"ENV TOKEN={docker_canary}\nWORKDIR /{docker_canary}\n"
        f"EXPOSE {docker_canary}\nRUN echo safe\n",
        encoding="utf-8",
    )
    db_path = tmp_path / ".repobrain" / "repobrain.sqlite"

    with GraphStore(db_path) as store:
        Indexer(store).index(tmp_path)
        for canary in (json_canary, toml_canary, docker_canary):
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

    database_bytes = Path(db_path).read_bytes()
    for canary in (json_canary, toml_canary, docker_canary):
        assert canary.encode() not in database_bytes

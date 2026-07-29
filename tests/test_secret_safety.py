import json
from pathlib import Path

from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.schema import Edge, EdgeType, FtsRow, Node, NodeType
from repobrain.graph.store import GraphStore
from repobrain.mcp_server import RepoBrainTools


CANARY = "sk_live_AUDITCANARY_9f3d2b"
CONFIG_KEY = "STRIPE_SECRET_KEY"


def _serialized(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _database_bytes(db_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(db_path.parent.glob(f"{db_path.name}*"))
        if path.is_file()
    )


def _every_mcp_read(tools: RepoBrainTools, search_result, trace_result) -> list[dict]:
    return [
        search_result,
        tools.explain_project(auto_index=False),
        tools.project_brief(auto_index=False),
        tools.change_context(auto_index=False),
        tools.co_change("app.py", auto_index=False),
        tools.churn_hotspots(auto_index=False),
        tools.ownership("app.py", auto_index=False),
        tools.explain_file("app.py", auto_index=False),
        tools.find_symbol("stripe_key", auto_index=False),
        tools.trace_symbol("stripe_key", auto_index=False),
        trace_result,
        tools.trace_data_flow("stripe_key", auto_index=False),
        tools.impact_analysis("app.py", auto_index=False),
        tools.docs_for_code("app.py", auto_index=False),
        tools.code_for_docs("app.py", auto_index=False),
        tools.read_agent_memory(auto_index=False),
        tools.verify_agent_memory(auto_index=False),
    ]


def test_dotenv_canary_never_reaches_database_cli_or_mcp_reads(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repo / ".env.local").write_text(
        f"{CONFIG_KEY}={CANARY}\n"
        "DATABASE_URL=postgres://admin:hunter2@prod-db/app\n",
        encoding="utf-8",
    )
    (repo / ".env.example").write_text(
        f"{CONFIG_KEY}={CANARY}\n",
        encoding="utf-8",
    )
    (repo / "app.py").write_text(
        "import os\n"
        f"stripe_key = os.getenv({CONFIG_KEY!r})\n",
        encoding="utf-8",
    )

    tools = RepoBrainTools(repo)
    indexed = tools.index_repo()
    assert indexed["status"] == "ok"

    # The two read paths called autonomously by an agent are explicit
    # regressions: search cannot find the value, while trace retains the code
    # usage and its line number without any dotenv definition/value.
    search_result = tools.search_project(CANARY, auto_index=False)
    assert search_result["status"] == "ok"
    assert search_result["results"] == []
    trace_result = tools.trace_config(CONFIG_KEY, auto_index=False)
    assert trace_result["status"] == "ok"
    assert trace_result["definitions"] == []
    assert any(usage["path"] == "app.py" for usage in trace_result["usages"])

    runner = CliRunner()
    cli_results = [
        runner.invoke(main, ["search", CANARY, "--path", str(repo), "--no-auto-index"]),
        runner.invoke(
            main,
            [
                "trace", "config", CONFIG_KEY,
                "--path", str(repo), "--json", "--no-auto-index",
            ],
        ),
    ]
    assert all(result.exit_code == 0 for result in cli_results)

    # Exercise every MCP read tool.  Most cannot independently surface dotenv
    # content, but keeping the complete public read surface in this adversarial
    # test prevents a future query/summary path from reintroducing the canary.
    read_results = _every_mcp_read(tools, search_result, trace_result)

    exposed = _serialized(read_results + [result.output for result in cli_results])
    assert CANARY not in exposed
    assert "hunter2" not in exposed

    db_path = repo / ".repobrain" / "repobrain.sqlite"
    db_bytes = _database_bytes(db_path)
    assert CANARY.encode() not in db_bytes
    assert b"hunter2" not in db_bytes

    # The canary source remains present, proving the absence checks are not
    # accidentally testing a fixture that never contained the secret.
    assert CANARY in (repo / ".env.local").read_text(encoding="utf-8")


def test_out_of_root_symlink_target_never_reaches_database_cli_or_mcp_reads(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        f"{CANARY}\n"
        "-----END OPENSSH PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("SAFE = True\n", encoding="utf-8")
    # Names chosen so no mandatory exclude and no extension heuristic applies:
    # only resolving the link can keep these out.
    (repo / "notes.md").symlink_to(outside / "id_rsa")
    (repo / "deploy_key.txt").symlink_to(outside / "id_rsa")

    tools = RepoBrainTools(repo)
    assert tools.index_repo()["status"] == "ok"

    search_result = tools.search_project(CANARY, auto_index=False)
    assert search_result["status"] == "ok"
    assert search_result["results"] == []
    trace_result = tools.trace_config(CONFIG_KEY, auto_index=False)

    runner = CliRunner()
    cli_results = [
        runner.invoke(main, ["search", CANARY, "--path", str(repo), "--no-auto-index"]),
        runner.invoke(main, ["explain", "file", "notes.md", "--path", str(repo),
                             "--no-auto-index"]),
    ]

    read_results = _every_mcp_read(tools, search_result, trace_result)
    exposed = _serialized(read_results + [result.output for result in cli_results])
    assert CANARY not in exposed

    db_bytes = _database_bytes(repo / ".repobrain" / "repobrain.sqlite")
    assert CANARY.encode() not in db_bytes

    # The canary source remains present, proving the absence checks are not
    # accidentally testing a fixture that never contained the secret.
    assert CANARY in (outside / "id_rsa").read_text(encoding="utf-8")


def test_upgrade_reindex_scrubs_legacy_dotenv_canary_from_sqlite(tmp_path):
    repo = tmp_path / "legacy-repo"
    repo.mkdir()
    dotenv = repo / ".env.local"
    dotenv.write_text(f"{CONFIG_KEY}={CANARY}\n", encoding="utf-8")
    db_path = repo / ".repobrain" / "repobrain.sqlite"

    # Seed the exact legacy sinks: generic/config FTS content, ConfigKey node
    # metadata, and SETS_ENV edge metadata.  Disable secure deletion only for
    # this setup connection to emulate a database produced before the fix.
    with GraphStore(db_path) as store:
        store.conn.execute("PRAGMA secure_delete=OFF")
        store.set_meta("root", str(repo.resolve()))
        file_node = Node(
            type=NodeType.FILE,
            name=".env.local",
            qualified_name=".env.local",
            path=".env.local",
            extractor="generic_file_parser",
        )
        config_node = Node(
            type=NodeType.CONFIG_FILE,
            name=".env.local",
            qualified_name=".env.local",
            path=".env.local",
            extractor="env_file_parser",
        )
        key_node = Node(
            type=NodeType.CONFIG_KEY,
            name=CONFIG_KEY,
            qualified_name=CONFIG_KEY,
            path=".env.local",
            start_line=1,
            end_line=1,
            metadata={"format": "dotenv", "value": CANARY},
            extractor="env_file_parser",
        )
        env_node = Node(
            type=NodeType.ENV_VAR,
            name=CONFIG_KEY,
            qualified_name=CONFIG_KEY,
            path="",
            extractor="env_file_parser",
        )
        edges = [
            Edge(
                type=EdgeType.DECLARES_CONFIG,
                source_node_id=config_node.id,
                target_node_id=key_node.id,
                path=".env.local",
                start_line=1,
                extractor="env_file_parser",
            ),
            Edge(
                type=EdgeType.SETS_ENV,
                source_node_id=key_node.id,
                target_node_id=env_node.id,
                path=".env.local",
                start_line=1,
                metadata={"value": CANARY},
                extractor="env_file_parser",
            ),
        ]
        stat = dotenv.stat()
        with store.conn:
            store.upsert_nodes([file_node, config_node, key_node, env_node])
            store.upsert_edges(edges)
            store.add_fts_rows([
                FtsRow(
                    path=".env.local",
                    name=".env.local",
                    content=f"{CONFIG_KEY}={CANARY}",
                    node_id=file_node.id,
                ),
                FtsRow(
                    path=".env.local",
                    name=".env.local",
                    content=f"{CONFIG_KEY}={CANARY}",
                    node_id=config_node.id,
                ),
            ])
            store.upsert_file(
                ".env.local",
                "legacy-hash",
                stat.st_size,
                stat.st_mtime,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                "config",
            )

    assert CANARY.encode() in _database_bytes(db_path)

    tools = RepoBrainTools(repo)
    upgraded = tools.index_repo()
    assert upgraded["files_deleted"] == 1
    search_result = tools.search_project(CANARY, auto_index=False)
    trace_result = tools.trace_config(CONFIG_KEY, auto_index=False)
    assert search_result["results"] == []
    assert trace_result["definitions"] == []

    runner = CliRunner()
    cli_results = [
        runner.invoke(main, ["search", CANARY, "--path", str(repo), "--no-auto-index"]),
        runner.invoke(
            main,
            [
                "trace", "config", CONFIG_KEY,
                "--path", str(repo), "--json", "--no-auto-index",
            ],
        ),
    ]
    assert all(result.exit_code == 0 for result in cli_results)
    exposed = _serialized(
        _every_mcp_read(tools, search_result, trace_result)
        + [result.output for result in cli_results]
    )
    assert CANARY not in exposed
    assert CANARY.encode() not in _database_bytes(db_path)
    assert CANARY in dotenv.read_text(encoding="utf-8")

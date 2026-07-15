import json
from pathlib import Path

from repobrain.graph.queries import impact_analysis, trace_data_flow


def _edges(store, type_: str):
    return store.conn.execute(
        """
        SELECT e.*, s.name AS source_name, s.type AS source_type,
               s.qualified_name AS source_qname, t.name AS target_name,
               t.type AS target_type, t.qualified_name AS target_qname
        FROM edges e JOIN nodes s ON s.id=e.source_node_id
                     JOIN nodes t ON t.id=e.target_node_id
        WHERE e.type=? ORDER BY e.path, e.start_line
        """, (type_,),
    ).fetchall()


def _route_target(store, route_name: str):
    return store.conn.execute(
        """
        SELECT t.* FROM edges e JOIN nodes r ON r.id=e.source_node_id
             JOIN nodes t ON t.id=e.target_node_id
        WHERE e.type='HANDLES_ROUTE' AND r.type='Route' AND r.name=?
        """, (route_name,),
    ).fetchone()


def test_express_inline_and_named_callbacks_have_precise_handlers(indexer, store, node_app):
    indexer.index(node_app)

    inline = _route_target(store, "POST /api/users")
    assert inline is not None
    assert inline["type"] == "Function"
    assert inline["name"] == "POST /api/users callback"
    assert inline["extractor"] == "framework-route-adapter"

    calls = [e for e in _edges(store, "CALLS") if e["source_node_id"] == inline["id"]]
    assert {(e["target_name"], e["confidence"]) for e in calls} == {("createUser", 0.9)}
    assert json.loads(calls[0]["metadata_json"])["resolution"] == "framework-inline-callback"

    named = _route_target(store, "GET /api/users/:id")
    assert named is not None
    assert named["name"] == "getUserRoute"
    assert named["path"] == "src/routes/users.js"
    assert named["extractor"] == "code_treesitter"


def test_flask_decorators_emit_methods_and_exact_nested_handlers(indexer, store, small_app):
    indexer.index(small_app)
    target = _route_target(store, "POST /api/users")
    assert target is not None
    assert target["qualified_name"] == "app.api.routes.register_routes.create_user_route"

    flow = trace_data_flow(store, "POST /api/users", depth=5, direction="out")
    assert {node["name"] for node in flow["nodes"]} >= {
        "create_user_route", "handle_create_user", "create_user", "insert",
    }


def test_flask_handler_span_disambiguates_same_file_names(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.py").write_text(
        "class Shadow:\n"
        "    def show(self): ...\n"
        "@app.get('/items')\n"
        "def show(): ...\n"
    )
    indexer.index(repo)
    target = _route_target(store, "GET /items")
    assert target is not None
    assert target["type"] == "Function"
    assert target["qualified_name"] == "routes.show"


def test_sqlalchemy_exact_table_flow_reaches_shared_queries(indexer, store, small_app):
    indexer.index(small_app)
    table = store.conn.execute(
        "SELECT * FROM nodes WHERE type='Table' AND name='users'"
    ).fetchone()
    assert table is not None
    assert json.loads(table["metadata_json"])["evidence"] == "__tablename__ literal"

    reads = _edges(store, "READS_TABLE")
    writes = _edges(store, "WRITES_TABLE")
    assert {(e["source_name"], e["target_name"]) for e in reads} == {("load_by_id", "users")}
    assert {(e["source_name"], e["target_name"]) for e in writes} == {("persist", "users")}
    assert reads[0]["is_inferred"] == 1
    assert reads[0]["inference_reason"] == "sqlalchemy-convention"
    assert reads[0]["confidence"] == 0.85

    impact = impact_analysis(store, table["qualified_name"])
    assert impact is not None
    impacted = impact["high_confidence"] + impact["medium_confidence"]
    assert {item["node"]["name"] for item in impacted} >= {"load_by_id", "persist"}


def test_adapter_incremental_convergence_and_unchanged_caller(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    routes = repo / "routes.js"
    handlers = repo / "handlers.js"
    routes.write_text(
        "const { handle } = require('./handlers');\n"
        "const router = require('express').Router();\n"
        "router.get('/items', handle);\n"
    )
    handlers.write_text("// target is added later\n")

    indexer.index(repo)
    assert _route_target(store, "GET /items") is None

    # Adding the target to an already imported module reconciles the unchanged
    # route file because import-binding syntax was persisted on its Module.
    handlers.write_text("function handle(req, res) { res.json([]); }\n")
    indexer.index(repo)
    assert _route_target(store, "GET /items")["name"] == "handle"

    handlers.write_text("function renamed(req, res) { res.json([]); }\n")
    indexer.index(repo)
    assert _route_target(store, "GET /items") is None
    assert not _edges(store, "HANDLES_ROUTE")  # stale edge was replaced

    routes.write_text(
        "const { renamed } = require('./handlers');\n"
        "const router = require('express').Router();\n"
        "router.get('/items', renamed);\n"
    )
    indexer.index(repo)
    assert _route_target(store, "GET /items")["name"] == "renamed"

    before = [tuple(row) for row in store.conn.execute(
        "SELECT id, source_node_id, target_node_id, type FROM edges ORDER BY id"
    )]
    stats = indexer.index(repo)
    after = [tuple(row) for row in store.conn.execute(
        "SELECT id, source_node_id, target_node_id, type FROM edges ORDER BY id"
    )]
    assert stats.files_changed == 0
    assert after == before

    handlers.unlink()
    indexer.index(repo)
    assert _route_target(store, "GET /items") is None


def test_adapter_version_backfills_a_fresh_legacy_graph(indexer, store, node_app):
    indexer.index(node_app)
    with store.conn:
        store.delete_facts_by_extractor("framework-route-adapter")
        store.conn.execute("DELETE FROM meta WHERE key='runtime_adapter_version'")
        store.delete_orphan_edges()
    assert _route_target(store, "POST /api/users") is None

    stats = indexer.index(node_app)
    assert stats.files_changed == 0
    assert _route_target(store, "POST /api/users")["name"] == "POST /api/users callback"


def test_dynamic_and_ambiguous_framework_receivers_are_skipped(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.js").write_text(
        "const router = require('express').Router();\n"
        "const handlers = {}; const action = 'show';\n"
        "router.get('/dynamic', handlers[action]);\n"
        "router.get('/middleware', audit, show);\n"
        "api.get('/fuzzy-receiver', show);\n"
    )
    (repo / "routes.py").write_text(
        "METHODS = ['POST']\n"
        "def get_app(): ...\n"
        "@get_app().route('/dynamic-app')\n"
        "def dynamic_app(): ...\n"
        "@app.route('/dynamic-methods', methods=METHODS)\n"
        "def dynamic_methods(): ...\n"
    )
    indexer.index(repo)
    routes = store.conn.execute("SELECT name, metadata_json FROM nodes WHERE type='Route'").fetchall()
    assert {row["name"] for row in routes} == {"GET /dynamic", "GET /middleware"}
    assert all(json.loads(row["metadata_json"])["ambiguous"] for row in routes)
    assert _edges(store, "HANDLES_ROUTE") == []


def test_orm_incremental_table_rename_deletion_and_ambiguity(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    model = repo / "models.py"
    repository = repo / "repository.py"
    model.write_text("class User:\n    __tablename__ = 'users'\n")
    repository.write_text(
        "from models import User\n"
        "def load(session, id):\n"
        "    return session.get(User, id)\n"
    )

    indexer.index(repo)
    assert {(e["source_name"], e["target_name"]) for e in _edges(store, "READS_TABLE")} == {
        ("load", "users")
    }

    # The repository is unchanged; reconciliation follows the exact imported
    # model identity to the renamed table literal and removes the stale edge.
    model.write_text("class User:\n    __tablename__ = 'accounts'\n")
    indexer.index(repo)
    assert store.conn.execute(
        "SELECT 1 FROM nodes WHERE type='Table' AND name='users'"
    ).fetchone() is None
    assert {(e["source_name"], e["target_name"]) for e in _edges(store, "READS_TABLE")} == {
        ("load", "accounts")
    }

    # Multiple literal mappings for one class are retained as facts but no
    # read/write relationship is guessed.
    model.write_text(
        "class User:\n"
        "    __tablename__ = 'accounts'\n"
        "    __tablename__ = 'legacy_users'\n"
    )
    indexer.index(repo)
    assert _edges(store, "READS_TABLE") == []

    model.write_text("class User:\n    pass\n")
    indexer.index(repo)
    assert store.conn.execute("SELECT 1 FROM nodes WHERE type='Table'").fetchone() is None
    assert _edges(store, "READS_TABLE") == []

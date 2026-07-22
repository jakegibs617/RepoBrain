from repobrain.graph.schema import Edge, Node, NodeType, EdgeType, node_id, edge_id


def _sample_node() -> Node:
    return Node(
        type=NodeType.FILE,
        name="config.py",
        qualified_name="app/db/config.py",
        path="app/db/config.py",
        start_line=1,
        end_line=10,
        language="python",
        extractor="generic_file_parser",
        commit_hash="abc123",
    )


def test_node_id_deterministic():
    a = node_id("File", "app/db/config.py", "app/db/config.py")
    b = node_id("File", "app/db/config.py", "app/db/config.py")
    assert a == b
    assert a != node_id("File", "app/db/config.py", "other/path.py")
    assert a != node_id("Directory", "app/db/config.py", "app/db/config.py")


def test_node_upsert_idempotent(store):
    node = _sample_node()
    store.upsert_nodes([node])
    store.commit()
    created_at = store.conn.execute(
        "SELECT created_at FROM nodes WHERE id = ?", (node.id,)
    ).fetchone()["created_at"]

    store.upsert_nodes([node])
    store.commit()
    rows = store.conn.execute("SELECT * FROM nodes").fetchall()
    assert len(rows) == 1
    assert rows[0]["created_at"] == created_at  # preserved on conflict
    assert rows[0]["last_seen_at"] >= created_at


def test_edge_upsert_idempotent(store):
    n1, n2 = _sample_node(), Node(
        type=NodeType.DIRECTORY, name="db", qualified_name="app/db", path="app/db"
    )
    store.upsert_nodes([n1, n2])
    edge = Edge(
        type=EdgeType.CONTAINS,
        source_node_id=n2.id,
        target_node_id=n1.id,
        path="app/db/config.py",
        extractor="generic_file_parser",
        commit_hash="abc123",
    )
    store.upsert_edges([edge])
    store.upsert_edges([edge])
    store.commit()
    rows = store.conn.execute("SELECT * FROM edges").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == edge_id(
        EdgeType.CONTAINS, n2.id, n1.id, "app/db/config.py", None
    )


def test_provenance_fields_present(store):
    node = _sample_node()
    store.upsert_nodes([node])
    edge = Edge(
        type=EdgeType.CONTAINS,
        source_node_id=node.id,
        target_node_id=node.id,
        path="app/db/config.py",
        start_line=3,
        extractor="markdown_parser",
        commit_hash="abc123",
        is_inferred=True,
        inference_reason="test inference",
        confidence=0.7,
    )
    store.upsert_edges([edge])
    store.commit()

    nrow = store.conn.execute("SELECT * FROM nodes").fetchone()
    for f in ("extractor", "confidence", "commit_hash", "created_at",
              "updated_at", "last_seen_at", "path", "start_line", "end_line"):
        assert nrow[f] is not None, f
    assert nrow["confidence"] == 1.0
    assert nrow["extractor"] == "generic_file_parser"

    erow = store.conn.execute("SELECT * FROM edges").fetchone()
    assert erow["is_inferred"] == 1
    assert erow["inference_reason"] == "test inference"
    assert erow["confidence"] == 0.7
    assert erow["commit_hash"] == "abc123"


def _envvar_node(name: str) -> Node:
    return Node(
        type=NodeType.ENV_VAR, name=name, qualified_name=name, path="",
        extractor="code_treesitter",
    )


def test_delete_orphan_envvars_removes_edgeless_envvar(store):
    """D17 (deferred sweep): an EnvVar node with zero incoming READS_ENV
    edges is swept in one bounded DELETE."""
    env = _envvar_node("ORPHANED_VAR")
    store.upsert_nodes([env])
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='EnvVar'"
    ).fetchone()[0] == 1

    store.delete_orphan_envvars()
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='EnvVar'"
    ).fetchone()[0] == 0


def test_delete_orphan_envvars_keeps_envvar_with_a_reader(store):
    env = _envvar_node("KEPT_VAR")
    reader = _sample_node()
    store.upsert_nodes([env, reader])
    edge = Edge(
        type=EdgeType.READS_ENV, source_node_id=reader.id, target_node_id=env.id,
        path=reader.path, start_line=1, extractor="code_treesitter",
    )
    store.upsert_edges([edge])
    store.commit()

    store.delete_orphan_envvars()
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='EnvVar'"
    ).fetchone()[0] == 1


def test_delete_orphan_envvars_survives_a_fresh_envvar_with_no_readers(store):
    """Not reachable via current extraction (an EnvVar node is only ever
    created alongside its own READS_ENV edge), but the sweep must not crash
    on it -- it should simply be removed like any other edgeless node."""
    env = _envvar_node("NEVER_READ")
    store.upsert_nodes([env])
    store.commit()
    store.delete_orphan_envvars()  # must not raise
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='EnvVar'"
    ).fetchone()[0] == 0


def test_delete_paths_removes_everything(store):
    node = _sample_node()
    store.upsert_nodes([node])
    from repobrain.graph.schema import FtsRow
    store.add_fts_rows([FtsRow(path=node.path, name=node.name, content="database config")])
    store.commit()
    store.delete_paths([node.path])
    store.commit()
    assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0] == 0

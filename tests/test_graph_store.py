import sqlite3

import pytest

from repobrain.graph.store import GraphStore
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


def test_fresh_database_records_current_schema_version(tmp_path):
    with GraphStore(tmp_path / "fresh.sqlite") as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_legacy_database_applies_ordered_migrations_idempotently(tmp_path):
    database = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            hash TEXT,
            size INTEGER,
            mtime REAL,
            language TEXT,
            last_indexed_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE content_fts USING fts5(path, name, content)"
    )
    conn.execute(
        "INSERT INTO files (path, hash, status) VALUES ('legacy.py', 'abc', 'active')"
    )
    conn.commit()
    conn.close()

    for _ in range(2):
        with GraphStore(database) as store:
            assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 2
            file_columns = {
                row["name"]
                for row in store.conn.execute("PRAGMA table_info(files)")
            }
            fts_columns = {
                row["name"]
                for row in store.conn.execute("PRAGMA table_info(content_fts)")
            }
            assert {"mtime_ns", "ctime_ns"} <= file_columns
            assert "node_id" in fts_columns
            assert store.conn.execute(
                "SELECT hash FROM files WHERE path='legacy.py'"
            ).fetchone()["hash"] == "abc"


def test_database_newer_than_supported_schema_is_rejected(tmp_path):
    database = tmp_path / "future.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA user_version = 999")
    conn.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        GraphStore(database)

    conn = sqlite3.connect(database)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='nodes'"
    ).fetchone()[0] == 0
    conn.close()


def test_read_only_store_reads_without_touching_the_database(tmp_path):
    """A display surface must be able to poll the graph without taking a write lock.

    Opening a normal store applies the schema and commits, so a statusline
    polling every few seconds would rewrite the database forever. The
    read-only open must leave the file, and its WAL sidecars, alone.
    """
    database = tmp_path / "ro.sqlite"
    with GraphStore(database) as store:
        store.upsert_nodes([_sample_node()])
        store.commit()

    # The WAL sidecars are left behind by the read-write close above. The
    # database and the log must come through the read-only open untouched;
    # -shm is deliberately excluded, because mapping the shared-memory index
    # is how a reader finds the WAL at all, and skipping it would mean
    # immutable=1 and a torn snapshot mid-index.
    def _state() -> dict[str, tuple[int, int]]:
        return {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in tmp_path.iterdir()
            if not path.name.endswith("-shm")
        }

    before = _state()
    contents = database.read_bytes()

    with GraphStore(database, read_only=True) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.upsert_nodes([_sample_node()])

    assert _state() == before
    assert database.read_bytes() == contents


def test_read_only_store_refuses_a_database_it_cannot_migrate(tmp_path):
    """Migrations need a write lock, so an off-version database is refused.

    The ordinary open path upgrades a legacy database in place. A read-only
    store cannot, and reading a pre-migration schema as though it were
    current would hand back wrong answers rather than an error.
    """
    database = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    with pytest.raises(RuntimeError, match="cannot be read read-only"):
        GraphStore(database, read_only=True)

    with pytest.raises(FileNotFoundError):
        GraphStore(tmp_path / "absent.sqlite", read_only=True)

    assert not (tmp_path / "absent.sqlite").exists()

from pathlib import Path


def _node_paths(store, type_=None):
    sql = "SELECT path FROM nodes"
    args = ()
    if type_:
        sql += " WHERE type = ?"
        args = (type_,)
    return {r["path"] for r in store.conn.execute(sql, args).fetchall()}


def test_initial_index_creates_nodes_edges_fts(indexer, store, small_app):
    stats = indexer.index(small_app)

    assert stats.files_scanned > 0
    assert stats.files_changed == stats.files_scanned  # everything new
    assert stats.nodes_created > 0
    assert stats.edges_created > 0

    file_paths = _node_paths(store, "File")
    assert "README.md" in file_paths
    assert "app/db/config.py" in file_paths

    # Directory CONTAINS File edge exists for a nested file
    row = store.conn.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN nodes d ON d.id = e.source_node_id AND d.type = 'Directory'
        JOIN nodes f ON f.id = e.target_node_id AND f.type = 'File'
        WHERE e.type = 'CONTAINS' AND f.path = 'app/db/config.py'
        """
    ).fetchone()
    assert row[0] == 1

    # FTS has content for indexed files
    hits = store.conn.execute(
        "SELECT path FROM content_fts WHERE content_fts MATCH '\"DATABASE_URL\"'"
    ).fetchall()
    assert any(h["path"] == "app/db/config.py" for h in hits)

    # files table populated with provenance
    frow = store.conn.execute(
        "SELECT * FROM files WHERE path = 'app/db/config.py'"
    ).fetchone()
    assert frow["status"] == "active"
    assert frow["hash"] and frow["last_indexed_at"]


def test_incremental_no_changes_parses_nothing(indexer, small_app):
    indexer.index(small_app)
    stats = indexer.index(small_app)
    assert stats.files_changed == 0
    assert stats.nodes_created == 0
    assert stats.edges_created == 0


def test_incremental_touch_one_file_reindexes_only_it(indexer, store, small_app):
    indexer.index(small_app)
    target = Path(small_app) / "app" / "db" / "config.py"
    target.write_text(target.read_text() + "\n# updated comment\n")

    stats = indexer.index(small_app)
    assert stats.files_changed == 1

    run = store.last_index_run()
    assert run["files_changed"] == 1
    # The touched file's stored hash was refreshed and matches the files table.
    node = store.conn.execute(
        "SELECT hash FROM nodes WHERE type='File' AND path='app/db/config.py'"
    ).fetchone()
    frow = store.conn.execute(
        "SELECT hash FROM files WHERE path='app/db/config.py'"
    ).fetchone()
    assert node["hash"] == frow["hash"]


def test_deleting_file_removes_its_nodes(indexer, store, small_app):
    indexer.index(small_app)
    assert "app/db/config.py" in _node_paths(store)

    (Path(small_app) / "app" / "db" / "config.py").unlink()
    stats = indexer.index(small_app)
    assert stats.files_deleted == 1

    assert "app/db/config.py" not in _node_paths(store)
    frow = store.conn.execute(
        "SELECT status FROM files WHERE path='app/db/config.py'"
    ).fetchone()
    assert frow["status"] == "deleted"
    fts = store.conn.execute(
        "SELECT COUNT(*) FROM content_fts WHERE path='app/db/config.py'"
    ).fetchone()
    assert fts[0] == 0


def test_full_reindex_flag_reparses_everything(indexer, small_app):
    indexer.index(small_app)
    stats = indexer.index(small_app, incremental=False)
    assert stats.files_changed == stats.files_scanned


def test_gitignore_is_respected(indexer, store, small_app):
    (Path(small_app) / ".gitignore").write_text("ignored_dir/\n*.secret\n")
    ignored = Path(small_app) / "ignored_dir"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("x = 1\n")
    (Path(small_app) / "creds.secret").write_text("token\n")

    indexer.index(small_app)
    paths = _node_paths(store)
    assert not any(p and p.startswith("ignored_dir") for p in paths)
    assert "creds.secret" not in paths

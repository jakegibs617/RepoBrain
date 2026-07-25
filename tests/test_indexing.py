import os
from pathlib import Path

import pytest

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer, RepoRootMismatchError
from repobrain.indexing.scanner import IgnoreMatcher, detect_language, scan


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


def test_dotenv_families_are_excluded_by_default(tmp_path):
    filenames = (
        ".env",
        ".env.local",
        ".env.example",
        "service.env",
        "service.env.local",
    )
    for name in filenames:
        (tmp_path / name).write_text("SECRET=not-indexable\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".env.production").write_text("SECRET=not-indexable\n", encoding="utf-8")
    (nested / "worker.env.test").write_text("SECRET=not-indexable\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("SAFE = True\n", encoding="utf-8")

    paths = {item.path for item in scan(tmp_path)}

    assert paths == {"app.py"}
    assert scan(tmp_path, include_patterns=[".env.local"]) == []


def test_anchored_dir_pattern_matches_only_at_root():
    m = IgnoreMatcher(["/dist/"])
    assert m.matches("dist", is_dir=True)
    assert m.matches("dist/bundle.js")
    assert not m.matches("src/dist", is_dir=True)
    assert not m.matches("src/dist/bundle.js")
    # unanchored form still matches at any depth
    m2 = IgnoreMatcher(["dist/"])
    assert m2.matches("src/dist", is_dir=True)
    assert m2.matches("src/dist/bundle.js")


def test_dockerfile_variants_are_classified_without_stealing_markdown():
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("containers/dockerfile.DEV") == "dockerfile"
    assert detect_language("docs/dockerfile.md") == "markdown"


def test_gitignore_negation_and_nested_scope(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "*.generated\n!important.generated\n/root-only.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "drop.generated").write_text("ignored", encoding="utf-8")
    (tmp_path / "important.generated").write_text("kept", encoding="utf-8")
    (tmp_path / "root-only.txt").write_text("ignored", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text(
        "!keep.generated\n/only-here.txt\n",
        encoding="utf-8",
    )
    (nested / "keep.generated").write_text("kept", encoding="utf-8")
    (nested / "only-here.txt").write_text("ignored", encoding="utf-8")
    deep = nested / "deep"
    deep.mkdir()
    (deep / "only-here.txt").write_text("kept", encoding="utf-8")
    (nested / "root-only.txt").write_text("kept", encoding="utf-8")

    paths = {item.path for item in scan(tmp_path)}

    assert "drop.generated" not in paths
    assert "important.generated" in paths
    assert "root-only.txt" not in paths
    assert "nested/keep.generated" in paths
    assert "nested/only-here.txt" not in paths
    assert "nested/deep/only-here.txt" in paths
    assert "nested/root-only.txt" in paths


def test_gitignore_negation_cannot_override_mandatory_safety_excludes(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "!.env.local\n!.repobrain/\n!.repobrain/repobrain.sqlite\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
    database_dir = tmp_path / ".repobrain"
    database_dir.mkdir()
    (database_dir / "repobrain.sqlite").write_text("not-a-db", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")

    assert {item.path for item in scan(tmp_path)} == {".gitignore", "safe.txt"}


def test_stat_shortcut_detects_same_size_change_with_restored_mtime(
    indexer, store, small_app
):
    indexer.index(small_app)
    target = Path(small_app) / "app" / "db" / "config.py"
    st = target.stat()
    content = target.read_text()
    # Reproduce the adversarial freshness miss: neither size nor reported
    # mtime changes. ctime_ns must force a hash check and graph replacement.
    target.write_text(content[:-1] + "#")
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))

    stats = indexer.index(small_app)
    assert stats.files_changed == 1
    row = store.conn.execute(
        "SELECT mtime_ns, ctime_ns FROM files WHERE path='app/db/config.py'"
    ).fetchone()
    changed_stat = target.stat()
    assert row["mtime_ns"] == changed_stat.st_mtime_ns
    assert row["ctime_ns"] == changed_stat.st_ctime_ns


def test_mtime_bump_with_same_content_stays_unchanged(indexer, store, small_app):
    indexer.index(small_app)
    target = Path(small_app) / "app" / "db" / "config.py"
    os.utime(target, None)  # bump mtime, content untouched

    stats = indexer.index(small_app)
    assert stats.files_changed == 0  # rehashed, hash matched
    # stat columns were refreshed so the next run can use the shortcut
    frow = store.conn.execute(
        "SELECT mtime, size FROM files WHERE path='app/db/config.py'"
    ).fetchone()
    assert frow["mtime"] == target.stat().st_mtime
    assert frow["size"] == target.stat().st_size


def test_unreadable_file_is_skipped_with_warning(indexer, store, small_app, monkeypatch):
    indexer.index(small_app)
    target = Path(small_app) / "app" / "db" / "config.py"
    os.utime(target, None)  # force the read path (defeat the stat shortcut)

    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.name == "config.py":
            raise OSError("simulated I/O error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    stats = indexer.index(small_app)

    assert any("config.py" in w and "unreadable" in w for w in stats.warnings)
    # the run completed and the unreadable file was NOT purged or deleted
    assert stats.files_deleted == 0
    assert "app/db/config.py" in _node_paths(store)
    frow = store.conn.execute(
        "SELECT status FROM files WHERE path='app/db/config.py'"
    ).fetchone()
    assert frow["status"] == "active"


def test_subdirectory_gets_own_db_and_root_db_untouched(small_app):
    # regression for the graph-purge bug: each indexed root owns its database
    root_db = Path(small_app) / ".repobrain" / "repobrain.sqlite"
    with GraphStore(root_db) as root_store:
        Indexer(root_store).index(small_app)
        root_files = set(root_store.active_files())
        root_nodes = root_store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert "app/api/routes.py" in root_files
        assert root_store.get_meta("root") == str(Path(small_app).resolve())

        sub = Path(small_app) / "app"
        sub_db = sub / ".repobrain" / "repobrain.sqlite"
        with GraphStore(sub_db) as sub_store:
            Indexer(sub_store).index(sub)
            sub_files = set(sub_store.active_files())
            assert "api/routes.py" in sub_files
            assert sub_store.get_meta("root") == str(sub.resolve())

        # the root database saw no deletions and kept every row
        assert set(root_store.active_files()) == root_files
        assert (
            root_store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            == root_nodes
        )


def test_root_mismatch_refuses_to_index_and_preserves_graph(indexer, store, small_app):
    indexer.index(small_app)
    files_before = store.file_count()
    nodes_before = store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    with pytest.raises(RepoRootMismatchError):
        indexer.index(Path(small_app) / "app")

    assert store.file_count() == files_before
    assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == nodes_before

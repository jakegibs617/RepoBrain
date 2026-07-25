import os
import sqlite3
from pathlib import Path

from repobrain.graph.store import GraphStore
from repobrain.indexing import incremental
from repobrain.indexing.indexer import Indexer


def test_restored_mtime_same_size_replaces_deleted_symbol(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "service.py"
    source.write_text("def old_name():\n    return 1\n", encoding="utf-8")

    with GraphStore(tmp_path / "graph.sqlite") as store:
        indexer = Indexer(store)
        indexer.index(root)
        original = source.stat()
        source.write_text("def new_name():\n    return 1\n", encoding="utf-8")
        assert source.stat().st_size == original.st_size
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))

        stats = indexer.index(root)

        names = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM nodes WHERE path='service.py'"
            ).fetchall()
        }
        assert stats.files_changed == 1
        assert "new_name" in names
        assert "old_name" not in names


def test_no_change_fast_path_does_not_read_file_bodies(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text("def current():\n    return 1\n", encoding="utf-8")

    with GraphStore(tmp_path / "graph.sqlite") as store:
        indexer = Indexer(store)
        indexer.index(root)

        def unexpected_read(_self):
            raise AssertionError("unchanged incremental run read a file body")

        monkeypatch.setattr(Path, "read_bytes", unexpected_read)
        stats = indexer.index(root)

    assert stats.files_changed == 0
    assert stats.nodes_created == 0
    assert stats.edges_created == 0


def test_platform_without_change_time_hashes_but_does_not_reparse_or_refresh_stat(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "service.py"
    source.write_text("def current():\n    return 1\n", encoding="utf-8")

    with GraphStore(tmp_path / "graph.sqlite") as store:
        indexer = Indexer(store)
        indexer.index(root)
        monkeypatch.setattr(incremental, "_ctime_tracks_changes", lambda: False)

        stats = indexer.index(root)

    assert stats.files_changed == 0
    assert stats.nodes_created == 0
    assert stats.edges_created == 0


def test_legacy_database_migrates_file_stat_columns_and_backfills(tmp_path):
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
        """
        INSERT INTO files
            (path, hash, size, mtime, language, last_indexed_at, status)
        VALUES ('service.py', 'stale', 28, 0, 'python', '', 'active')
        """
    )
    conn.commit()
    conn.close()

    root = tmp_path / "repo"
    root.mkdir()
    source = root / "service.py"
    source.write_text("def current():\n    return 1\n", encoding="utf-8")

    with GraphStore(database) as store:
        columns = {
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(files)").fetchall()
        }
        assert {"mtime_ns", "ctime_ns"} <= columns
        Indexer(store).index(root)
        row = store.conn.execute(
            "SELECT mtime_ns, ctime_ns FROM files WHERE path='service.py'"
        ).fetchone()

    assert row["mtime_ns"] == source.stat().st_mtime_ns
    assert row["ctime_ns"] == source.stat().st_ctime_ns

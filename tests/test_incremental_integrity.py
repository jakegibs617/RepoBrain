import os
import sqlite3
from pathlib import Path

from repobrain.graph.store import GraphStore
from repobrain.indexing import incremental
from repobrain.indexing.indexer import Indexer
from repobrain.parsers.base import (
    RUN_SCOPED_MANIFESTS,
    GenericFileParser,
    ParserRegistry,
)


def _older_extractor() -> ParserRegistry:
    """A registry that cannot read structured config, standing in for a past
    RepoBrain whose JSON extraction did not exist yet."""
    registry = ParserRegistry()
    registry.register(GenericFileParser())
    return registry


def _config_keys(store: GraphStore) -> set[str]:
    return {
        row["name"]
        for row in store.conn.execute("SELECT name FROM nodes WHERE type = 'ConfigKey'")
    }


def test_extractor_upgrade_reparses_files_the_working_tree_never_touched(tmp_path):
    """A parser improvement must reach files whose bytes never changed.

    Stat-and-hash freshness answers "did the tree move", which is the wrong
    question after RepoBrain itself changes: the file is byte-identical and the
    facts extractable from it are not. Without a fingerprint the second index
    below takes the unchanged fast path and the config keys never appear, which
    is exactly how a live index goes semantically stale while reporting current.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").write_text(
        '{"name": "svc", "version": "1.0.0"}\n', encoding="utf-8"
    )

    with GraphStore(tmp_path / "graph.sqlite") as store:
        Indexer(store, registry=_older_extractor()).index(root)
        assert _config_keys(store) == set()

        stats = Indexer(store).index(root)

        assert _config_keys(store) == {"name", "version"}
        assert stats.files_changed == 1


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

        # Scoped to the indexed tree on purpose. The invariant is that the fast
        # path does not read the bodies of the files it is indexing; RepoBrain
        # reading its own installed sources to fingerprint its extractor is a
        # different thing, and a blanket patch would conflate the two.
        #
        # Both read methods, and an allowlist rather than a flat refusal. A
        # guard on `read_bytes` alone passed for a year while D19's `go.mod`
        # read went through `read_text` — the exemption existed and nothing
        # enforced its bounds, so a second manifest reader could be added
        # without anyone deciding to. `RUN_SCOPED_MANIFESTS` is that decision,
        # and this is what holds the code to it.
        real = {name: getattr(Path, name) for name in ("read_bytes", "read_text")}

        def guard(method: str):
            def read(self, *args, **kwargs):
                if (self.resolve().is_relative_to(root.resolve())
                        and self.name not in RUN_SCOPED_MANIFESTS):
                    raise AssertionError(
                        f"unchanged incremental run read {self.name} via {method}"
                    )
                return real[method](self, *args, **kwargs)
            return read

        for method in real:
            monkeypatch.setattr(Path, method, guard(method))
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

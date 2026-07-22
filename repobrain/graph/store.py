"""SQLite-backed graph store (WAL mode) with FTS5 full-text index."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .schema import Edge, FtsRow, Node

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    language TEXT,
    hash TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    extractor TEXT,
    commit_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    type TEXT NOT NULL,
    path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    metadata_json TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    extractor TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    commit_hash TEXT,
    is_inferred INTEGER NOT NULL DEFAULT 0,
    inference_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_path ON edges(path);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node_id);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    hash TEXT,
    size INTEGER,
    mtime REAL,
    language TEXT,
    last_indexed_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_scanned INTEGER,
    files_changed INTEGER,
    nodes_created INTEGER,
    edges_created INTEGER,
    warnings_json TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS git_commits (
    sha TEXT PRIMARY KEY,
    committed_at INTEGER NOT NULL,
    author_name TEXT,
    author_email TEXT,
    files_changed INTEGER NOT NULL,
    additions INTEGER NOT NULL,
    deletions INTEGER NOT NULL,
    co_change_excluded TEXT
);

CREATE TABLE IF NOT EXISTS git_commit_files (
    sha TEXT NOT NULL,
    path TEXT NOT NULL,
    original_path TEXT NOT NULL,
    additions INTEGER,
    deletions INTEGER,
    PRIMARY KEY (sha, path)
);
CREATE INDEX IF NOT EXISTS idx_git_commit_files_path ON git_commit_files(path);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(path, name, content, node_id UNINDEXED);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: well under SQLite's default SQLITE_MAX_VARIABLE_NUMBER (999 on older
#: builds); batched IN(...) statements use one slot per chunked path.
_TOUCH_CHUNK_SIZE = 500


def _chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class GraphStore:
    """Thin wrapper around a SQLite database holding the project graph."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        #: the last freshness-gate result for this store, set by gated
        #: surfaces so downstream queries can honor history serveability
        self.last_freshness: dict | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._migrate_fts()
        self.conn.commit()

    def _migrate_fts(self) -> None:
        """Rebuild content_fts if it predates the node_id column (pre-release
        databases only). A full re-index repopulates the dropped rows."""
        cols = {r[1] for r in self.conn.execute("SELECT * FROM pragma_table_info('content_fts')")}
        if "node_id" not in cols:
            self.conn.execute("DROP TABLE content_fts")
            self.conn.execute(
                "CREATE VIRTUAL TABLE content_fts USING fts5(path, name, content, node_id UNINDEXED)"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- nodes / edges -----------------------------------------------------

    def upsert_nodes(self, nodes: Sequence[Node]) -> None:
        """Insert or update nodes; created_at is preserved on conflict."""
        now = _now()
        rows = [
            (
                n.id, n.type, n.name, n.qualified_name or None, n.path,
                n.start_line, n.end_line, n.language, n.hash, n.metadata_json,
                now, now, now, n.confidence, n.extractor, n.commit_hash,
            )
            for n in nodes
        ]
        self.conn.executemany(
            """
            INSERT INTO nodes (id, type, name, qualified_name, path, start_line,
                end_line, language, hash, metadata_json, created_at, updated_at,
                last_seen_at, confidence, extractor, commit_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, qualified_name=excluded.qualified_name,
                start_line=excluded.start_line, end_line=excluded.end_line,
                language=excluded.language, hash=excluded.hash,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at, last_seen_at=excluded.last_seen_at,
                confidence=excluded.confidence, extractor=excluded.extractor,
                commit_hash=excluded.commit_hash
            """,
            rows,
        )

    def upsert_edges(self, edges: Sequence[Edge]) -> None:
        now = _now()
        rows = [
            (
                e.id, e.source_node_id, e.target_node_id, e.type, e.path,
                e.start_line, e.end_line, e.metadata_json, e.confidence,
                e.extractor, now, now, now, e.commit_hash,
                1 if e.is_inferred else 0, e.inference_reason,
            )
            for e in edges
        ]
        self.conn.executemany(
            """
            INSERT INTO edges (id, source_node_id, target_node_id, type, path,
                start_line, end_line, metadata_json, confidence, extractor,
                created_at, updated_at, last_seen_at, commit_hash, is_inferred,
                inference_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                metadata_json=excluded.metadata_json,
                end_line=excluded.end_line,
                confidence=excluded.confidence, extractor=excluded.extractor,
                updated_at=excluded.updated_at, last_seen_at=excluded.last_seen_at,
                commit_hash=excluded.commit_hash, is_inferred=excluded.is_inferred,
                inference_reason=excluded.inference_reason
            """,
            rows,
        )

    def delete_paths(self, paths: Iterable[str]) -> None:
        """Remove all nodes, edges, and FTS rows whose provenance path matches."""
        rows = [(p,) for p in paths]
        if not rows:
            return
        self.conn.executemany("DELETE FROM nodes WHERE path = ?", rows)
        self.conn.executemany("DELETE FROM edges WHERE path = ?", rows)
        self.conn.executemany("DELETE FROM content_fts WHERE path = ?", rows)

    def delete_orphan_edges(self) -> None:
        self.conn.execute(
            """
            DELETE FROM edges WHERE source_node_id NOT IN (SELECT id FROM nodes)
                OR target_node_id NOT IN (SELECT id FROM nodes)
            """
        )

    def delete_edges(self, type_: str, extractor: str | None = None) -> None:
        """Delete a family of edges, optionally scoped to its owner.

        Reconcilers use this to replace only the derived facts they own,
        without reaching through the persistence boundary with raw SQL.
        """
        if extractor is None:
            self.conn.execute("DELETE FROM edges WHERE type = ?", (str(type_),))
        else:
            self.conn.execute(
                "DELETE FROM edges WHERE type = ? AND extractor = ?",
                (str(type_), extractor),
            )

    def delete_facts_by_extractor(self, extractor: str) -> None:
        """Delete every derived node/edge owned by one reconciler.

        Cross-file reconcilers rebuild their complete, bounded fact family in
        one transaction.  Keeping ownership-based deletion here prevents
        adapters from reaching through the persistence boundary with raw SQL.
        """
        self.conn.execute("DELETE FROM edges WHERE extractor = ?", (extractor,))
        self.conn.execute("DELETE FROM nodes WHERE extractor = ?", (extractor,))

    def touch_paths(self, paths: Iterable[str]) -> None:
        """Refresh last_seen_at for nodes/edges of unchanged files.

        Every path gets the same timestamp, so this batches into bounded
        ``WHERE path IN (...)`` statements (chunked to stay well under
        SQLite's default host-parameter limit) instead of one UPDATE per
        path. On a large unchanged corpus this is the difference between an
        O(files) list of single-row statements and a small, bounded number
        of statements each run — the SQL work stays proportional to changed
        work, not total repository size.
        """
        path_list = list(paths)
        if not path_list:
            return
        now = _now()
        for table in ("nodes", "edges"):
            for chunk in _chunked(path_list, _TOUCH_CHUNK_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                self.conn.execute(
                    f"UPDATE {table} SET last_seen_at = ? WHERE path IN ({placeholders})",
                    (now, *chunk),
                )

    # -- FTS ---------------------------------------------------------------

    def add_fts_rows(self, rows: Sequence[FtsRow]) -> None:
        self.conn.executemany(
            "INSERT INTO content_fts (path, name, content, node_id) VALUES (?, ?, ?, ?)",
            [(r.path, r.name, r.content, r.node_id) for r in rows],
        )

    # -- git history ---------------------------------------------------------

    def replace_git_history(
        self, commits: Sequence[tuple], commit_files: Sequence[tuple]
    ) -> None:
        """Atomically replace the extractor-owned Git history evidence tables.

        The extraction window is bounded, so a full rebuild is the simplest
        convergent strategy: re-extracting identical history is idempotent and
        rewritten/shortened history leaves no stale rows behind.
        """
        self.conn.execute("DELETE FROM git_commit_files")
        self.conn.execute("DELETE FROM git_commits")
        self.conn.executemany(
            """
            INSERT INTO git_commits (sha, committed_at, author_name, author_email,
                files_changed, additions, deletions, co_change_excluded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            commits,
        )
        self.conn.executemany(
            """
            INSERT INTO git_commit_files (sha, path, original_path, additions, deletions)
            VALUES (?, ?, ?, ?, ?)
            """,
            commit_files,
        )

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- files -------------------------------------------------------------

    def upsert_file(self, path: str, hash_: str, size: int, mtime: float, language: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO files (path, hash, size, mtime, language, last_indexed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, size=excluded.size,
                mtime=excluded.mtime, language=excluded.language,
                last_indexed_at=excluded.last_indexed_at, status='active'
            """,
            (path, hash_, size, mtime, language, _now()),
        )

    def update_file_stat(self, path: str, size: int, mtime: float) -> None:
        """Refresh stat columns for a file whose content hash was unchanged."""
        self.conn.execute(
            "UPDATE files SET size = ?, mtime = ? WHERE path = ?", (size, mtime, path)
        )

    def mark_files_deleted(self, paths: Iterable[str]) -> None:
        rows = [(_now(), p) for p in paths]
        if rows:
            self.conn.executemany(
                "UPDATE files SET status='deleted', last_indexed_at=? WHERE path=?", rows
            )

    def active_files(self) -> dict[str, sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM files WHERE status = 'active'")
        return {row["path"]: row for row in cur.fetchall()}

    # -- index runs ----------------------------------------------------------

    def record_index_run(
        self,
        started_at: str,
        finished_at: str,
        files_scanned: int,
        files_changed: int,
        nodes_created: int,
        edges_created: int,
        warnings: list[str],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO index_runs (started_at, finished_at, files_scanned,
                files_changed, nodes_created, edges_created, warnings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (started_at, finished_at, files_scanned, files_changed,
             nodes_created, edges_created, json.dumps(warnings)),
        )

    def last_index_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM index_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # -- stats ---------------------------------------------------------------

    def counts_by_type(self, table: str) -> dict[str, int]:
        assert table in ("nodes", "edges")
        cur = self.conn.execute(f"SELECT type, COUNT(*) AS c FROM {table} GROUP BY type ORDER BY c DESC")
        return {row["type"]: row["c"] for row in cur.fetchall()}

    def file_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files WHERE status='active'").fetchone()[0]

    def commit(self) -> None:
        self.conn.commit()

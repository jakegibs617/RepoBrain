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
    mtime_ns INTEGER,
    ctime_ns INTEGER,
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

# SQLite records the last successfully applied migration in the database
# header.  Version zero is reserved for databases created before versioned
# migrations were introduced.
_SCHEMA_VERSION = 2


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

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        #: the last freshness-gate result for this store, set by gated
        #: surfaces so downstream queries can honor history serveability
        self.last_freshness: dict | None = None
        self.read_only = read_only
        if read_only:
            self._connect_read_only()
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        # Excluded files may have been indexed by an older RepoBrain version.
        # Overwrite deleted cells so credentials do not survive in SQLite
        # freelist pages after the upgrade re-index removes their live rows.
        self.conn.execute("PRAGMA secure_delete=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > _SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError(
                "RepoBrain database schema version "
                f"{current_version} is newer than supported version {_SCHEMA_VERSION}"
            )
        self.conn.executescript(_SCHEMA)
        self._migrate_schema(current_version)
        self.conn.commit()

    def _connect_read_only(self) -> None:
        """Open an existing database without writing to it.

        Display surfaces poll the graph far more often than anything indexes
        it, and the ordinary open path writes every time: it creates the
        directory, sets ``journal_mode``, replays ``_SCHEMA``, runs pending
        migrations and commits. None of that is safe to repeat on a timer
        beside a live indexing run, so read-only callers skip all of it.

        ``mode=ro`` is preferred and deliberately does *not* set
        ``immutable=1``: immutable tells SQLite no other process can be writing
        and lets it ignore the WAL, which would serve a torn pre-WAL snapshot
        mid-index. The price is that opening a WAL-mode database creates an
        empty ``-wal``/``-shm`` pair on platforms that removed them at the last
        close. Nothing is written to the database and no write lock is taken;
        an empty log is the proof.
        """
        if not self.db_path.exists():
            raise FileNotFoundError(f"No RepoBrain database at {self.db_path}")
        self.conn = self._read_only_connection()
        self.conn.row_factory = sqlite3.Row
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version != _SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError(
                "RepoBrain database schema version "
                f"{current_version} cannot be read read-only; supported version is "
                f"{_SCHEMA_VERSION}. Run `repobrain index` to migrate it."
            )

    def _read_only_connection(self) -> sqlite3.Connection:
        """Connect read-only, falling back to ``immutable=1`` only when safe.

        A read-only connection is not permitted to create the ``-shm``
        shared-memory index, so ``mode=ro`` fails the open outright — not
        degrades, fails — on a WAL-mode database whose sidecars are absent.
        That is the resting state of any idle index on platforms that remove
        them at the last close, and of any database that was copied or restored
        without them, so leaving it unhandled makes a perfectly intact index
        report ``unavailable`` to every display that polls it.

        ``immutable=1`` opens those, at the documented cost of ignoring the
        WAL. The reason that cost is not being paid here: the sidecars are
        absent *because* no connection holds the database, and a live indexing
        run necessarily has them present, in which case ``mode=ro`` succeeded
        and this path was never reached. The fallback is therefore only taken
        when the thing ``immutable`` asserts — no concurrent writer — is what
        the failed open just demonstrated.

        The preferred open is probed with a real statement, because
        ``sqlite3.connect`` is lazy: it returns a handle without touching the
        file, so a missing ``-shm`` only surfaces on first use.
        """
        preferred = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            preferred.execute("PRAGMA user_version")
            return preferred
        except sqlite3.OperationalError:
            preferred.close()
        return sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)

    def _migrate_schema(self, current: int) -> None:
        """Apply pending schema migrations in order.

        ``_SCHEMA`` always describes the latest shape so new databases can be
        created in one pass.  Migrations remain defensive and inspect the
        actual schema, allowing both fresh databases and pre-versioning
        databases (whose ``user_version`` is zero) to take the same path.
        Each version marker is committed atomically with its migration.
        """
        migrations = (
            (1, self._migrate_file_stats),
            (2, self._migrate_fts),
        )
        for version, migrate in migrations:
            if current >= version:
                continue
            with self.conn:
                migrate()
                self.conn.execute(f"PRAGMA user_version = {version}")
            current = version

    def _migrate_file_stats(self) -> None:
        """Add high-resolution freshness signals to pre-upgrade databases.

        Existing rows intentionally remain NULL: the first incremental run
        hashes each active file once and backfills both columns, after which
        unchanged runs retain the read-free stat shortcut.
        """
        cols = {
            r[1] for r in self.conn.execute("SELECT * FROM pragma_table_info('files')")
        }
        if "mtime_ns" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN mtime_ns INTEGER")
        if "ctime_ns" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN ctime_ns INTEGER")

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

    def delete_orphan_envvars(self) -> None:
        """Remove EnvVar nodes with no remaining incoming READS_ENV edge.

        EnvVar nodes are repo-global (id keyed on ``("EnvVar", name, "")``,
        path="") and deliberately excluded from `delete_paths`' path-based
        cleanup so one reader's deletion never destroys a node shared by
        other readers (D17). That means an EnvVar whose *last* reader is
        deleted, or edited to stop reading it, would otherwise linger as an
        edgeless node forever. This closes that gap with one bounded
        DELETE/subquery pair -- not a per-row loop -- so it stays consistent
        with D30's batching invariant regardless of how many EnvVar nodes
        exist. Call after `delete_orphan_edges` in the same transaction, so
        this run's final READS_ENV edge set (post orphan-edge cleanup) is
        what "no remaining reader" is judged against.
        """
        self.conn.execute(
            """
            DELETE FROM nodes WHERE type = 'EnvVar' AND id NOT IN (
                SELECT target_node_id FROM edges WHERE type = 'READS_ENV'
            )
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

    def upsert_file(
        self,
        path: str,
        hash_: str,
        size: int,
        mtime: float,
        mtime_ns: int,
        ctime_ns: int,
        language: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO files (
                path, hash, size, mtime, mtime_ns, ctime_ns, language,
                last_indexed_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, size=excluded.size,
                mtime=excluded.mtime, mtime_ns=excluded.mtime_ns,
                ctime_ns=excluded.ctime_ns, language=excluded.language,
                last_indexed_at=excluded.last_indexed_at, status='active'
            """,
            (path, hash_, size, mtime, mtime_ns, ctime_ns, language, _now()),
        )

    def update_file_stat(
        self, path: str, size: int, mtime: float, mtime_ns: int, ctime_ns: int
    ) -> None:
        """Refresh stat columns for a file whose content hash was unchanged."""
        self.conn.execute(
            """
            UPDATE files
            SET size = ?, mtime = ?, mtime_ns = ?, ctime_ns = ?
            WHERE path = ?
            """,
            (size, mtime, mtime_ns, ctime_ns, path),
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

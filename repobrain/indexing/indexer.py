"""Indexer: orchestrates scan -> diff -> parse -> store."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import RepoBrainConfig
from ..graph.store import GraphStore
from ..parsers.base import ParseResult, ParserRegistry, default_registry
from .incremental import compute_diff
from .scanner import ScannedFile, scan


class RepoRootMismatchError(RuntimeError):
    """Raised when a database pinned to one repo root is asked to index another."""


@dataclass
class IndexStats:
    files_scanned: int = 0
    files_changed: int = 0
    files_deleted: int = 0
    nodes_created: int = 0
    edges_created: int = 0
    warnings: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_commit_hash(root: str | Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


class Indexer:
    def __init__(
        self,
        store: GraphStore,
        config: RepoBrainConfig | None = None,
        registry: ParserRegistry | None = None,
    ):
        self.store = store
        self.config = config or RepoBrainConfig()
        self.registry = registry or default_registry()

    def index(self, root: str | Path, incremental: bool = True) -> IndexStats:
        """Index `root`. Incremental runs only re-parse changed/added files."""
        root = Path(root).resolve()
        self._check_root(root)
        started_at = _now()
        commit = git_commit_hash(root)
        stats = IndexStats()

        scanned = scan(
            root,
            extra_excludes=self.config.exclude_patterns,
            include_patterns=self.config.include_patterns or None,
            max_file_size=self.config.max_file_size_bytes,
        )
        stats.files_scanned = len(scanned)

        diff = compute_diff(scanned, self.store, incremental=incremental)
        stats.files_changed = len(diff.to_parse)
        stats.files_deleted = len(diff.deleted)

        # Parsers that resolve cross-file references (e.g. imports) get the
        # full set of scanned paths before any file is parsed.
        known_paths = {f.path for f in scanned}
        for parser in self.registry.all():
            begin = getattr(parser, "begin_run", None)
            if begin is not None:
                begin(known_paths)

        combined = ParseResult()
        combined.warnings.extend(diff.warnings)
        for f in diff.to_parse:
            combined.extend(self._parse_file(f, diff.contents[f.path]))
        stats.warnings = combined.warnings

        for node in combined.nodes:
            node.commit_hash = commit
        for edge in combined.edges:
            edge.commit_hash = commit
        for node in combined.nodes:
            if node.hash is None and node.path in diff.hashes:
                node.hash = diff.hashes[node.path]

        with self.store.conn:  # single transaction
            self.store.delete_paths(diff.stale_paths)
            self.store.mark_files_deleted(diff.deleted)
            self.store.upsert_nodes(combined.nodes)
            self.store.upsert_edges(combined.edges)
            self.store.add_fts_rows(combined.fts_rows)
            for f in diff.to_parse:
                self.store.upsert_file(f.path, diff.hashes[f.path], f.size, f.mtime, f.language)
            for f in diff.stat_changed:
                self.store.update_file_stat(f.path, f.size, f.mtime)
            self.store.touch_paths([f.path for f in diff.unchanged])
            # Reconciliation pass: parsers may resolve cross-file relationships
            # (e.g. name-only CALLS) now that this run's nodes are stored.
            extra_edges = []
            for parser in self.registry.all():
                finish = getattr(parser, "finish_run", None)
                if finish is not None:
                    extra_edges.extend(finish(self.store))
            for edge in extra_edges:
                edge.commit_hash = commit
            self.store.upsert_edges(extra_edges)
            combined.edges.extend(extra_edges)
            self._cleanup_directories(diff)
            self.store.delete_orphan_edges()
            stats.nodes_created = len({n.id for n in combined.nodes})
            stats.edges_created = len({e.id for e in combined.edges})
            self.store.record_index_run(
                started_at, _now(), stats.files_scanned, stats.files_changed,
                stats.nodes_created, stats.edges_created, stats.warnings,
            )
        return stats

    def _check_root(self, root: Path) -> None:
        """Pin the database to one repo root; refuse to index any other.

        Without this, indexing a different root would collide relative paths
        and purge the previous root's entire graph as 'deleted files'.
        """
        stored = self.store.get_meta("root")
        if stored is None:
            self.store.set_meta("root", str(root))
        elif stored != str(root):
            raise RepoRootMismatchError(
                f"This database indexes '{stored}' but you asked to index "
                f"'{root}'. Run repobrain from that root, or index the new "
                f"path with its own database (repobrain index {root})."
            )

    def _parse_file(self, f: ScannedFile, content: str) -> ParseResult:
        result = ParseResult()
        for parser in self.registry.parsers_for(f.path, f.language):
            try:
                result.extend(parser.parse(f.path, content))
            except Exception as exc:  # a parser bug must not sink the run
                result.warnings.append(f"{f.path}: {parser.name} failed: {exc}")
        return result

    def _cleanup_directories(self, diff) -> None:
        """Drop Directory nodes whose path no longer contains any active file."""
        if not diff.deleted:
            return
        live_dirs: set[str] = set()
        for path in self.store.active_files():
            parts = path.split("/")[:-1]
            for i in range(len(parts)):
                live_dirs.add("/".join(parts[: i + 1]))
        cur = self.store.conn.execute("SELECT id, path FROM nodes WHERE type = 'Directory'")
        dead = [(row["id"],) for row in cur.fetchall() if row["path"] not in live_dirs]
        if dead:
            self.store.conn.executemany("DELETE FROM nodes WHERE id = ?", dead)

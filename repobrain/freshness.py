"""Freshness checks and the shared pre-query auto-index gate."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import RepoBrainConfig
from .graph.store import GraphStore
from .history import refresh_history
from .indexing.indexer import Indexer
from .indexing.scanner import scan

DEFAULT_MAX_CHANGED_FILES = 10
DEFAULT_MAX_CHANGED_BYTES = 256 * 1024


class FreshnessBlockedError(RuntimeError):
    """Raised when a query must not run against stale indexed facts."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(result["message"])


@dataclass(frozen=True)
class FreshnessPolicy:
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    max_changed_bytes: int = DEFAULT_MAX_CHANGED_BYTES


def check_freshness(root: str | Path, store: GraphStore,
                    config: RepoBrainConfig | None = None) -> dict:
    """Return a cheap size+mtime staleness summary without reading file content."""
    root = Path(root).resolve()
    config = config or RepoBrainConfig.load(root)
    scanned = scan(
        root,
        extra_excludes=config.exclude_patterns,
        include_patterns=config.include_patterns or None,
        max_file_size=config.max_file_size_bytes,
    )
    known = store.active_files()
    current = {item.path: item for item in scanned}
    added = sorted(set(current) - set(known))
    deleted = sorted(set(known) - set(current))
    changed = sorted(
        path for path in set(current) & set(known)
        if current[path].size != known[path]["size"]
        or current[path].mtime != known[path]["mtime"]
    )
    changed_bytes = (
        sum(current[path].size for path in added + changed)
        + sum(known[path]["size"] for path in deleted)
    )
    return {
        "is_stale": bool(added or changed or deleted),
        "out_of_date_count": len(added) + len(changed) + len(deleted),
        "changed_bytes": changed_bytes,
        "added": added,
        "changed": changed,
        "deleted": deleted,
    }


def ensure_fresh(
    root: str | Path,
    store: GraphStore,
    *,
    auto_index: bool = True,
    policy: FreshnessPolicy | None = None,
) -> dict:
    """Ensure a query can safely read the graph, auto-indexing only small diffs.

    The function never serves data itself. Callers may execute their query only
    when ``can_query`` is true; blocked and failed repairs always return false.
    """
    root = Path(root).resolve()
    policy = policy or FreshnessPolicy()
    # one config load per gate pass: staleness check, repair, and history
    # refresh must all see the same include/exclude/window settings
    config = RepoBrainConfig.load(root)
    before = check_freshness(root, store, config=config)
    thresholds = {
        "max_changed_files": policy.max_changed_files,
        "max_changed_bytes": policy.max_changed_bytes,
    }
    base = {"before": before, "thresholds": thresholds}
    if not before["is_stale"]:
        # Pure commits (or rebases) move HEAD without touching the working
        # tree, so history freshness is checked even when files are current.
        return {"status": "current", "can_query": True, "auto_indexed": False,
                "message": "Index is current.",
                "history": refresh_history(root, store, auto_index=auto_index,
                                           config=config),
                **base}

    if not auto_index:
        return {"status": "blocked", "can_query": False, "auto_indexed": False,
                "reason": "auto_index_disabled",
                "message": _blocked_message(before, thresholds, "automatic repair is disabled"),
                **base}

    if (before["out_of_date_count"] > policy.max_changed_files
            or before["changed_bytes"] > policy.max_changed_bytes):
        return {"status": "blocked", "can_query": False, "auto_indexed": False,
                "reason": "threshold_exceeded",
                "message": _blocked_message(before, thresholds, "diff exceeds the safe threshold"),
                **base}

    try:
        stats = Indexer(store, config=config).index(root, incremental=True)
    except Exception as exc:
        return {"status": "error", "can_query": False, "auto_indexed": False,
                "reason": "auto_index_failed", "error": str(exc),
                "message": f"Automatic reindex failed; stale facts were not served: {exc}", **base}

    after = check_freshness(root, store, config=config)
    if after["is_stale"]:
        return {"status": "error", "can_query": False, "auto_indexed": True,
                "reason": "still_stale", "after": after,
                "message": "Automatic reindex completed but the working tree is still stale; "
                           "stale facts were not served.", **base}
    return {
        "status": "reindexed", "can_query": True, "auto_indexed": True,
        "message": f"Automatically reindexed {before['out_of_date_count']} changed file(s).",
        "history": refresh_history(root, store, auto_index=auto_index,
                                   config=config),
        "after": after,
        "index_stats": {
            "files_scanned": stats.files_scanned,
            "files_changed": stats.files_changed,
            "files_deleted": stats.files_deleted,
            "nodes_created": stats.nodes_created,
            "edges_created": stats.edges_created,
            "warnings": stats.warnings,
        },
        **base,
    }


def require_fresh(*args, **kwargs) -> dict:
    """Run :func:`ensure_fresh` and raise before a stale query can execute."""
    result = ensure_fresh(*args, **kwargs)
    if not result["can_query"]:
        raise FreshnessBlockedError(result)
    return result


def _blocked_message(diff: dict, thresholds: dict, reason: str) -> str:
    return (
        f"Index is stale ({diff['out_of_date_count']} file(s), "
        f"{diff['changed_bytes']} changed byte(s)); {reason}. Run `repobrain index`. "
        f"Safe auto-index limits are {thresholds['max_changed_files']} file(s) and "
        f"{thresholds['max_changed_bytes']} byte(s)."
    )

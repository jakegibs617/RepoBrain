"""Read-only checks comparing the indexed file set with the working tree."""
from __future__ import annotations

from pathlib import Path

from .config import RepoBrainConfig
from .graph.store import GraphStore
from .indexing.scanner import scan


def check_freshness(root: str | Path, store: GraphStore) -> dict:
    """Return a cheap size+mtime staleness summary without reading file content."""
    root = Path(root).resolve()
    config = RepoBrainConfig.load(root)
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
    return {
        "is_stale": bool(added or changed or deleted),
        "out_of_date_count": len(added) + len(changed) + len(deleted),
        "added": added,
        "changed": changed,
        "deleted": deleted,
    }

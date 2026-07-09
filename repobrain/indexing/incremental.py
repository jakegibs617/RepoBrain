"""Incremental diffing: compare scanned files against the stored file table."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..graph.store import GraphStore
from .scanner import ScannedFile


@dataclass
class IndexDiff:
    added: list[ScannedFile] = field(default_factory=list)
    changed: list[ScannedFile] = field(default_factory=list)
    unchanged: list[ScannedFile] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def to_parse(self) -> list[ScannedFile]:
        return self.added + self.changed

    @property
    def stale_paths(self) -> list[str]:
        """Paths whose existing graph rows must be removed before re-adding."""
        return [f.path for f in self.changed] + self.deleted


def compute_diff(
    scanned: list[ScannedFile], hashes: dict[str, str], store: GraphStore
) -> IndexDiff:
    """Classify scanned files as added/changed/unchanged and find deletions.

    `hashes` maps each scanned path to its current sha256 content hash.
    """
    known = store.active_files()
    diff = IndexDiff()
    seen: set[str] = set()
    for f in scanned:
        seen.add(f.path)
        prev = known.get(f.path)
        if prev is None:
            diff.added.append(f)
        elif prev["hash"] != hashes[f.path]:
            diff.changed.append(f)
        else:
            diff.unchanged.append(f)
    diff.deleted = [p for p in known if p not in seen]
    return diff

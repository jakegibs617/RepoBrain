"""Incremental diffing: compare scanned files against the stored file table.

Files whose size, nanosecond mtime, and nanosecond ctime match the stored row
are trusted as unchanged without being read or re-hashed. The ctime signal
closes the same-size/restored-mtime hole on platforms where ctime tracks
metadata changes. Content is read (once) only for files that may need parsing;
unreadable files are skipped with a warning instead of failing the run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..graph.store import GraphStore
from .hasher import hash_bytes
from .scanner import ScannedFile


def _ctime_tracks_changes() -> bool:
    """Whether ctime is a usable change signal on this platform.

    POSIX ctime changes when file content or metadata changes. On Windows,
    Python exposes creation time as ctime, so identical size/mtime cannot be
    trusted and the conservative fallback is to hash the file.
    """
    return os.name != "nt"


@dataclass
class IndexDiff:
    added: list[ScannedFile] = field(default_factory=list)
    changed: list[ScannedFile] = field(default_factory=list)
    unchanged: list[ScannedFile] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    #: files whose content hash is unchanged but stat (mtime/size) moved;
    #: their file rows need a stat refresh so the shortcut works next run.
    stat_changed: list[ScannedFile] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    #: decoded content, populated only for files that need parsing
    contents: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def to_parse(self) -> list[ScannedFile]:
        return self.added + self.changed

    @property
    def stale_paths(self) -> list[str]:
        """Paths whose existing graph rows must be removed before re-adding."""
        return [f.path for f in self.changed] + self.deleted


def compute_diff(
    scanned: list[ScannedFile], store: GraphStore, incremental: bool = True
) -> IndexDiff:
    """Classify scanned files as added/changed/unchanged and find deletions.

    With incremental=False every readable file is treated as changed (full
    re-parse) but deletions are still detected.
    """
    known = store.active_files()
    diff = IndexDiff()
    seen: set[str] = set()
    for f in scanned:
        seen.add(f.path)
        prev = known.get(f.path)
        if (
            incremental
            and prev is not None
            and _ctime_tracks_changes()
            and prev["size"] == f.size
            and prev["mtime_ns"] == f.mtime_ns
            and prev["ctime_ns"] == f.ctime_ns
        ):
            # stat unchanged: trust the stored hash, skip reading entirely
            diff.unchanged.append(f)
            continue
        try:
            data = Path(f.abs_path).read_bytes()
        except OSError as exc:
            # Keep the path in `seen` so a transient read failure never
            # cascades into deleting the file's existing graph rows.
            diff.warnings.append(f"{f.path}: unreadable, skipped: {exc}")
            continue
        hash_ = hash_bytes(data)
        diff.hashes[f.path] = hash_
        if prev is None:
            diff.added.append(f)
            diff.contents[f.path] = data.decode("utf-8", errors="replace")
        elif not incremental or prev["hash"] != hash_:
            diff.changed.append(f)
            diff.contents[f.path] = data.decode("utf-8", errors="replace")
        else:
            # content identical, only the stat moved
            diff.unchanged.append(f)
            if (
                prev["size"] != f.size
                or prev["mtime_ns"] != f.mtime_ns
                or prev["ctime_ns"] != f.ctime_ns
            ):
                diff.stat_changed.append(f)
    diff.deleted = [p for p in known if p not in seen]
    return diff

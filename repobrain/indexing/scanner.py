"""File scanner: walks a repo applying ignore rules and language detection.

Gitignore support is intentionally simple (fnmatch-based). Supported:
- blank lines and `#` comments are skipped
- trailing `/` marks a directory pattern (matches the dir and everything in it)
- leading `/` anchors the pattern to the scan root
- `*`, `?`, `[...]` globbing via fnmatch, matched against both the full
  relative POSIX path and the basename / individual path segments

NOT supported (documented limitations):
- negation (`!pattern`) — such lines are skipped
- nested .gitignore files in subdirectories (only the scan root's
  .gitignore / .repobrainignore are read)
- true gitignore `*` / `**` semantics: fnmatch's `*` crosses `/`, so a
  pattern like `docs/*` over-matches nested paths such as `docs/a/b.md`
  (real gitignore would only match direct children)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_EXCLUDES = [
    ".git/",
    ".repobrain/",
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    "coverage/",
    ".cache/",
    ".next/",
    ".nuxt/",
    "venv/",
    ".venv/",
    "__pycache__/",
    "target/",
    ".DS_Store",
    "*.lock",
]

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".php": "php",
    ".rb": "ruby",
    ".go": "go",
    ".java": "java",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".md": "markdown",
    ".markdown": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".txt": "text",
    ".cfg": "config",
    ".ini": "config",
    ".env": "config",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
_SNIFF_BYTES = 8192


@dataclass
class ScannedFile:
    path: str  # POSIX relative path from scan root
    abs_path: str
    size: int
    mtime: float
    language: str | None


@dataclass
class _Pattern:
    pattern: str
    is_dir: bool
    anchored: bool


class IgnoreMatcher:
    """fnmatch-based subset of gitignore semantics. See module docstring."""

    def __init__(self, patterns: list[str]):
        self.patterns: list[_Pattern] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue  # negation unsupported; skipped
            anchored = line.startswith("/")
            line = line.lstrip("/")
            is_dir = line.endswith("/")
            line = line.rstrip("/")
            if line:
                self.patterns.append(_Pattern(line, is_dir, anchored))

    def matches(self, rel_path: str, is_dir: bool = False) -> bool:
        segments = rel_path.split("/")
        basename = segments[-1]
        for p in self.patterns:
            if p.is_dir:
                if p.anchored:
                    # anchored dir pattern (`/dist/`): only a root-level
                    # directory (or its contents) may match
                    if fnmatch(segments[0], p.pattern) and (is_dir or len(segments) > 1):
                        return True
                else:
                    # any directory segment on the path may match
                    dir_segments = segments if is_dir else segments[:-1]
                    if any(fnmatch(seg, p.pattern) for seg in dir_segments):
                        return True
                    if is_dir and fnmatch(rel_path, p.pattern):
                        return True
            else:
                if p.anchored:
                    if fnmatch(rel_path, p.pattern):
                        return True
                elif fnmatch(rel_path, p.pattern) or fnmatch(basename, p.pattern):
                    return True
        return False


def _load_ignore_file(path: Path) -> list[str]:
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    return []


def detect_language(path: str) -> str | None:
    name = os.path.basename(path)
    if name == "Dockerfile":
        return "dockerfile"
    if name == "Makefile":
        return "makefile"
    return LANGUAGE_BY_EXTENSION.get(os.path.splitext(name)[1].lower())


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_SNIFF_BYTES]


def build_ignore_matcher(
    root: str | Path, extra_excludes: list[str] | None = None
) -> IgnoreMatcher:
    """Build the matcher scan() uses, so other extractors share one file universe."""
    root = Path(root).resolve()
    patterns = list(DEFAULT_EXCLUDES)
    patterns += _load_ignore_file(root / ".gitignore")
    patterns += _load_ignore_file(root / ".repobrainignore")
    patterns += extra_excludes or []
    return IgnoreMatcher(patterns)


def scan(
    root: str | Path,
    extra_excludes: list[str] | None = None,
    include_patterns: list[str] | None = None,
    max_file_size: int = MAX_FILE_SIZE,
) -> list[ScannedFile]:
    """Walk `root` and return indexable text files, applying ignore rules."""
    root = Path(root).resolve()
    matcher = build_ignore_matcher(root, extra_excludes)

    results: list[ScannedFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        # prune ignored directories in place
        dirnames[:] = sorted(
            d for d in dirnames
            if not matcher.matches(f"{rel_dir}/{d}".lstrip("/"), is_dir=True)
        )
        for fname in sorted(filenames):
            rel = f"{rel_dir}/{fname}".lstrip("/")
            if matcher.matches(rel):
                continue
            if include_patterns and not any(fnmatch(rel, p) for p in include_patterns):
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            if st.st_size > max_file_size:
                continue
            try:
                with open(abs_path, "rb") as fh:
                    head = fh.read(_SNIFF_BYTES)
            except OSError:
                continue
            if is_binary(head):
                continue
            results.append(
                ScannedFile(
                    path=rel,
                    abs_path=abs_path,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    language=detect_language(rel),
                )
            )
    return results

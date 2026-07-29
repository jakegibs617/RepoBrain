"""File scanner: walks a repo applying gitignore rules and language detection."""
from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from pathspec import GitIgnoreSpec


# These inputs are never indexable. Keep them in a separate, final matcher so
# repository/user negation rules cannot accidentally re-include secrets or the
# index database itself.
MANDATORY_EXCLUDES = [
    ".git/",
    ".repobrain/",
    ".env",
    ".env.*",
    "*.env",
    "*.env.*",
]

DEFAULT_EXCLUDES = [
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
    mtime_ns: int
    ctime_ns: int
    language: str | None


@dataclass(frozen=True)
class _IgnoreLayer:
    base_path: str
    spec: GitIgnoreSpec


class IgnoreMatcher:
    """Ordered gitwildmatch rules, optionally scoped to nested directories."""

    def __init__(
        self,
        patterns: list[str],
        mandatory_patterns: list[str] | None = None,
    ):
        self._layers: list[_IgnoreLayer] = []
        self._mandatory = GitIgnoreSpec.from_lines(mandatory_patterns or [])
        self.add_patterns(patterns)

    def add_patterns(self, patterns: list[str], base_path: str = "") -> None:
        """Append rules as if read from ``base_path/.gitignore``."""
        if not patterns:
            return
        base_path = base_path.strip("/")
        self._layers.append(
            _IgnoreLayer(base_path, GitIgnoreSpec.from_lines(patterns))
        )

    def matches(self, rel_path: str, is_dir: bool = False) -> bool:
        rel_path = rel_path.strip("/")
        candidate = f"{rel_path}/" if is_dir else rel_path
        if self._mandatory.check_file(candidate).include is True:
            return True

        ignored: bool | None = None
        for layer in self._layers:
            if layer.base_path:
                prefix = f"{layer.base_path}/"
                if rel_path == layer.base_path:
                    scoped = ""
                elif rel_path.startswith(prefix):
                    scoped = rel_path[len(prefix):]
                else:
                    continue
            else:
                scoped = rel_path
            scoped_candidate = f"{scoped}/" if is_dir and scoped else scoped
            decision = layer.spec.check_file(scoped_candidate).include
            if decision is not None:
                ignored = decision
        return ignored is True


def _load_ignore_file(path: Path) -> list[str]:
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    return []


def detect_language(path: str) -> str | None:
    name = os.path.basename(path)
    if is_dockerfile_name(name):
        return "dockerfile"
    if name == "Makefile":
        return "makefile"
    return LANGUAGE_BY_EXTENSION.get(os.path.splitext(name)[1].lower())


def is_dockerfile_name(name: str) -> bool:
    """Recognize Dockerfile variants without stealing known source/doc files."""
    lowered = name.lower()
    if lowered == "dockerfile":
        return True
    if not lowered.startswith("dockerfile."):
        return False
    return os.path.splitext(lowered)[1] not in LANGUAGE_BY_EXTENSION


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_SNIFF_BYTES]


def resolves_within_root(abs_path: str, resolved_root: str) -> bool:
    """True when `abs_path` still lands inside the indexed root once symlinks resolve.

    The ignore rules match path *names*, so they cannot see where a symlink
    actually points: a link named `notes.md` would sail past the mandatory
    dotenv excludes and pull in whatever it targets. The trust boundary has to
    be the resolved object, not the name used to reach it.
    """
    try:
        real = os.path.realpath(abs_path)
    except OSError:
        return False
    return real == resolved_root or real.startswith(resolved_root + os.sep)


def build_ignore_matcher(
    root: str | Path, extra_excludes: list[str] | None = None
) -> IgnoreMatcher:
    """Build the matcher scan() uses, so other extractors share one file universe."""
    root = Path(root).resolve()
    patterns = list(DEFAULT_EXCLUDES)
    patterns += _load_ignore_file(root / ".gitignore")
    patterns += _load_ignore_file(root / ".repobrainignore")
    patterns += extra_excludes or []
    return IgnoreMatcher(patterns, mandatory_patterns=MANDATORY_EXCLUDES)


def scan(
    root: str | Path,
    extra_excludes: list[str] | None = None,
    include_patterns: list[str] | None = None,
    max_file_size: int = MAX_FILE_SIZE,
) -> list[ScannedFile]:
    """Walk `root` and return indexable text files, applying ignore rules."""
    root = Path(root).resolve()
    matcher = build_ignore_matcher(root, extra_excludes)
    resolved_root = os.path.realpath(root)

    results: list[ScannedFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        elif ".gitignore" in filenames:
            matcher.add_patterns(
                _load_ignore_file(Path(dirpath) / ".gitignore"),
                base_path=rel_dir,
            )
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
            if not resolves_within_root(abs_path, resolved_root):
                continue
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
                    mtime_ns=st.st_mtime_ns,
                    ctime_ns=st.st_ctime_ns,
                    language=detect_language(rel),
                )
            )
    return results

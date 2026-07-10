"""Deterministic Git history extraction: co-change, churn, and ownership evidence.

All Git access is read-only plumbing over the local repository (argument
arrays, never a shell; no hosted APIs). Facts are mined from a bounded recent
commit window and are correlation evidence, not dependencies: co-change and
ownership must stay visibly heuristic and lower-confidence than observed
imports/calls/config/doc edges (DECISIONS D25).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from .config import RepoBrainConfig
from .graph.schema import Edge, EdgeType, NodeType, node_id
from .graph.store import GraphStore
from .indexing.scanner import build_ignore_matcher

EXTRACTOR = "git-history"
#: bump when extraction/scoring output changes shape, so refresh_history
#: re-extracts on upgrade even when HEAD is unchanged
EXTRACTOR_VERSION = 1

#: a pair must co-change in at least this many qualifying commits to earn an edge
MIN_CO_CHANGE_SUPPORT = 2
#: co-change confidence ceiling: always below the 0.6 medium-impact boundary,
#: so history alone can never masquerade as static impact
MAX_CO_CHANGE_CONFIDENCE = 0.55

CO_CHANGE_EXPLANATION = (
    "Files that repeatedly changed in the same commits within the recent "
    "history window. Broad commits are discounted (each pair in a commit "
    "touching k files contributes 1/(k-1)); the score normalizes that weighted "
    "support by the less frequently changed file's commit count. This is "
    "correlation observed in local Git history, not a static dependency."
)
OWNERSHIP_DISCLAIMER = (
    "Observed contribution history from the local commit window only. This is "
    "not an authorization model and not a CODEOWNERS claim."
)


class GitHistoryError(RuntimeError):
    """Raised when history cannot be extracted trustworthily."""


@dataclass
class _Commit:
    sha: str
    committed_at: int
    author_name: str
    author_email: str
    #: (additions|None, deletions|None, path, old_path|None); None counts = binary
    entries: list[tuple[int | None, int | None, str, str | None]] = field(
        default_factory=list
    )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHistoryError(f"git {args[0]} failed: {exc}") from exc


def _git_text(root: Path, *args: str) -> str:
    process = _run_git(root, *args)
    if process.returncode != 0:
        detail = process.stderr.decode(errors="replace").strip() or "Git command failed"
        raise GitHistoryError(detail)
    return process.stdout.decode("utf-8", errors="surrogateescape")


def probe_repository(root: str | Path) -> dict:
    """Read-only availability probe: full local Git history or an honest reason."""
    root = Path(root).resolve()
    try:
        inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    except GitHistoryError as exc:
        return {"available": False, "reason": "git_unavailable", "detail": str(exc), "head": None}
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        return {"available": False, "reason": "not_a_git_repository", "head": None}
    shallow = _run_git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode == 0 and shallow.stdout.strip() == b"true":
        return {"available": False, "reason": "shallow_repository", "head": None}
    head = _run_git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return {"available": False, "reason": "no_commits", "head": None}
    return {"available": True, "reason": None, "head": head.stdout.decode().strip()}


# -- log parsing -------------------------------------------------------------

_HEADER_FIELDS = 4  # sha, committer time, author name, author email


def _parse_numstat_log(text: str) -> list[_Commit]:
    """Parse `git log -z --numstat --pretty=format:%H%x01%ct%x01%aN%x01%aE`.

    Byte layout (locked by tests): each commit starts with the \\x01-separated
    header. A commit with changes continues after one newline with numstat
    entries `added\\tdeleted\\tpath` NUL-terminated; a rename entry is
    `added\\tdeleted\\t` NUL old NUL new NUL. Commits are NUL-separated, so a
    commit with entries ends with an empty token while an empty commit's
    header token is followed directly by the next header.
    """
    commits: list[_Commit] = []
    expect_header = True
    pending_counts: tuple[int | None, int | None] | None = None
    pending_paths: list[str] = []
    for token in text.split("\0"):
        if pending_counts is not None:
            pending_paths.append(token)
            if len(pending_paths) == 2:
                old_path, new_path = pending_paths
                commits[-1].entries.append(
                    (pending_counts[0], pending_counts[1], new_path, old_path)
                )
                pending_counts, pending_paths = None, []
            continue
        if not token:
            expect_header = True
            continue
        if expect_header:
            header, separator, token = token.partition("\n")
            parts = header.split("\x01")
            if len(parts) != _HEADER_FIELDS:
                raise GitHistoryError("Malformed commit header from git log")
            commits.append(
                _Commit(parts[0], int(parts[1]), parts[2], parts[3])
            )
            expect_header = not separator
            if not token:
                continue
        added, deleted, path = token.split("\t", 2)
        counts = (
            None if added == "-" else int(added),
            None if deleted == "-" else int(deleted),
        )
        if path == "":
            pending_counts = counts
        else:
            commits[-1].entries.append((counts[0], counts[1], path, None))
    return commits


def _read_window(root: Path, max_commits: int) -> list[_Commit]:
    text = _git_text(
        root, "log", "-z", "--numstat", "--no-merges", "--find-renames",
        f"--max-count={max_commits}",
        "--pretty=format:%H%x01%ct%x01%aN%x01%aE", "HEAD", "--",
    )
    if not text:
        return []
    return _parse_numstat_log(text)


def _resolve_identities(commits: list[_Commit]) -> dict[str, dict[str, list]]:
    """Map every historical path to its current identity (rename continuity).

    Commits arrive newest-first, so registering old->new aliases after each
    commit lets older occurrences chain forward through multiple renames.
    Returns {sha: {current_path: [additions, deletions, original_path]}}.
    """
    alias: dict[str, str] = {}

    def resolve(path: str) -> str:
        seen = set()
        while path in alias and path not in seen:
            seen.add(path)
            path = alias[path]
        return path

    resolved: dict[str, dict[str, list]] = {}
    for commit in commits:
        rows: dict[str, list] = {}
        for added, deleted, path, old_path in commit.entries:
            current = resolve(path)
            row = rows.setdefault(current, [0, 0, path])
            row[0] += added or 0
            row[1] += deleted or 0
        resolved[commit.sha] = rows
        for _added, _deleted, path, old_path in commit.entries:
            if old_path and old_path != resolve(path):
                alias[old_path] = resolve(path)
    return resolved


# -- extraction --------------------------------------------------------------

def _history_params(config: RepoBrainConfig) -> dict:
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "max_commits": config.history_max_commits,
        "max_files_per_commit": config.history_max_files_per_commit,
    }


def extract_history(
    root: str | Path, store: GraphStore, *, config: RepoBrainConfig | None = None
) -> dict:
    """Extract the recent commit window and rebuild all extractor-owned facts.

    Never mutates branches, refs, the index, or the working tree; the only
    writes are to RepoBrain's own SQLite store, inside one transaction.
    """
    root = Path(root).resolve()
    config = config or RepoBrainConfig.load(root)
    probe = probe_repository(root)
    if not probe["available"]:
        raise GitHistoryError(f"Git history is unavailable: {probe['reason']}")
    commits = _read_window(root, config.history_max_commits)
    resolved = _resolve_identities(commits)

    matcher = build_ignore_matcher(root, config.exclude_patterns)
    includes = config.include_patterns or None

    def qualifies(path: str) -> bool:
        if matcher.matches(path):
            return False
        return not includes or any(fnmatch(path, pattern) for pattern in includes)

    commit_rows, file_rows = [], []
    filtered: dict[str, dict[str, list]] = {}
    oversized = 0
    for commit in commits:
        rows = {
            path: counts
            for path, counts in resolved[commit.sha].items()
            if qualifies(path)
        }
        excluded = None
        if len(rows) > config.history_max_files_per_commit:
            excluded = "oversized"
            oversized += 1
        additions = sum(row[0] for row in rows.values())
        deletions = sum(row[1] for row in rows.values())
        commit_rows.append((
            commit.sha, commit.committed_at, commit.author_name,
            commit.author_email, len(rows), additions, deletions, excluded,
        ))
        file_rows.extend(
            (commit.sha, path, row[2], row[0], row[1])
            for path, row in sorted(rows.items())
        )
        if excluded is None:
            filtered[commit.sha] = rows

    edges = _co_change_edges(store, commits, filtered, probe["head"], config)
    now = datetime.now(timezone.utc).isoformat()
    with store.conn:
        store.replace_git_history(commit_rows, file_rows)
        store.delete_edges(EdgeType.CO_CHANGED_WITH, EXTRACTOR)
        store.upsert_edges(edges)
        store.set_meta("history_head", probe["head"])
        store.set_meta("history_extracted_at", now)
        store.set_meta("history_params", json.dumps(_history_params(config), sort_keys=True))
        store.set_meta("history_commit_count", str(len(commits)))
    return {
        "status": "ok",
        "head": probe["head"],
        "commits": len(commits),
        "oversized_commits": oversized,
        "files": len({row[1] for row in file_rows}),
        "co_change_edges": len(edges),
    }


def _co_change_edges(
    store: GraphStore,
    commits: list[_Commit],
    filtered: dict[str, dict[str, list]],
    head: str,
    config: RepoBrainConfig,
) -> list[Edge]:
    """File-level co-change coupling with support counts and broad-commit discount."""
    pair_stats: dict[tuple[str, str], dict] = {}
    path_commits: dict[str, int] = {}
    for commit in commits:  # newest-first, so supporting commit lists stay ordered
        rows = filtered.get(commit.sha)
        if rows is None:
            continue
        paths = sorted(rows)
        for path in paths:
            path_commits[path] = path_commits.get(path, 0) + 1
        if len(paths) < 2:
            continue
        weight = 1.0 / (len(paths) - 1)
        for i, first in enumerate(paths):
            for second in paths[i + 1:]:
                stats = pair_stats.setdefault(
                    (first, second),
                    {"support": 0, "weighted": 0.0, "commits": []},
                )
                stats["support"] += 1
                stats["weighted"] += weight
                stats["commits"].append(commit.sha)

    active = set(store.active_files())
    window = len(commits)
    edges = []
    for (first, second), stats in sorted(pair_stats.items()):
        if stats["support"] < MIN_CO_CHANGE_SUPPORT:
            continue
        if first not in active or second not in active:
            continue
        denominator = min(path_commits[first], path_commits[second])
        score = min(1.0, stats["weighted"] / denominator) if denominator else 0.0
        edges.append(Edge(
            type=EdgeType.CO_CHANGED_WITH,
            # File nodes are keyed on qualified_name == path (GenericFileParser)
            source_node_id=node_id(NodeType.FILE, first, first),
            target_node_id=node_id(NodeType.FILE, second, second),
            path="",  # outside path-based cleanup; the orphan sweep owns removal
            metadata={
                "support": stats["support"],
                "weighted_support": round(stats["weighted"], 4),
                "score": round(score, 4),
                "supporting_commits": stats["commits"],
                "window_commits": window,
                "head": head,
                "paths": [first, second],
            },
            confidence=round(MAX_CO_CHANGE_CONFIDENCE * score, 3),
            extractor=EXTRACTOR,
            commit_hash=head,
            is_inferred=True,
            inference_reason="git-co-change",
        ))
    return edges


# -- freshness integration ---------------------------------------------------

def refresh_history(
    root: str | Path,
    store: GraphStore,
    *,
    auto_index: bool = True,
    config: RepoBrainConfig | None = None,
) -> dict:
    """Bring extracted history up to the current HEAD, mirroring the M12 rules.

    New commits and rebases move HEAD, so a HEAD (or window-config) mismatch
    triggers one bounded re-extraction when mutation is allowed. Non-Git and
    shallow repositories report honestly instead of failing; ``stale`` means
    history-backed facts must not be served (auto repair was disabled).
    """
    root = Path(root).resolve()
    config = config or RepoBrainConfig.load(root)
    try:
        probe = probe_repository(root)
    except GitHistoryError as exc:
        return {"status": "error", "error": str(exc)}
    if not probe["available"]:
        return {"status": "unavailable", "reason": probe["reason"]}
    params = json.dumps(_history_params(config), sort_keys=True)
    if (store.get_meta("history_head") == probe["head"]
            and store.get_meta("history_params") == params):
        return {"status": "current", "head": probe["head"]}
    if not auto_index:
        return {
            "status": "stale", "head": probe["head"],
            "extracted_head": store.get_meta("history_head"),
            "message": "Git history facts are stale and automatic repair is "
                       "disabled; run `repobrain index` to re-extract.",
        }
    try:
        return {**extract_history(root, store, config=config), "status": "extracted"}
    except GitHistoryError as exc:
        return {"status": "error", "error": str(exc)}


def history_serveable(history: dict | None) -> bool:
    return bool(history) and history.get("status") in {"current", "extracted"}


def history_provenance(store: GraphStore) -> dict:
    """Provenance stamp for any answer built on extracted history."""
    try:
        params = json.loads(store.get_meta("history_params") or "{}")
    except json.JSONDecodeError:
        params = {}
    count = store.get_meta("history_commit_count")
    return {
        "head": store.get_meta("history_head"),
        "extracted_at": store.get_meta("history_extracted_at"),
        "window": params,
        "commit_count": int(count) if count else 0,
        "extractor": EXTRACTOR,
    }


# -- shared queries (pure reads over extracted facts) -------------------------

def _iso(timestamp) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()


def co_change_partners(store: GraphStore, path: str, limit: int = 20) -> list[dict]:
    """Historically coupled partners of one file, strongest first."""
    rows = store.conn.execute(
        """
        SELECT s.path AS source_path, t.path AS target_path,
               e.confidence, e.metadata_json
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'CO_CHANGED_WITH' AND (s.path = ? OR t.path = ?)
        ORDER BY e.confidence DESC, s.path, t.path
        LIMIT ?
        """,
        (path, path, limit),
    ).fetchall()
    items = []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        partner = row["target_path"] if row["source_path"] == path else row["source_path"]
        items.append({
            "path": path,
            "partner_path": partner,
            "support": meta.get("support"),
            "weighted_support": meta.get("weighted_support"),
            "score": meta.get("score"),
            "confidence": row["confidence"],
            "supporting_commits": meta.get("supporting_commits", []),
            "inference_reason": "git-co-change",
        })
    return items


def churn_hotspots(store: GraphStore, limit: int = 20) -> list[dict]:
    """Commit-count and line-churn hotspots among currently active files."""
    rows = store.conn.execute(
        """
        SELECT gcf.path, COUNT(*) AS commits,
               SUM(COALESCE(gcf.additions, 0)) AS additions,
               SUM(COALESCE(gcf.deletions, 0)) AS deletions,
               COUNT(DISTINCT gc.author_email) AS authors,
               MAX(gc.committed_at) AS last_committed_at
        FROM git_commit_files gcf
        JOIN git_commits gc ON gc.sha = gcf.sha
        JOIN files f ON f.path = gcf.path AND f.status = 'active'
        GROUP BY gcf.path
        ORDER BY commits DESC,
                 SUM(COALESCE(gcf.additions, 0)) + SUM(COALESCE(gcf.deletions, 0)) DESC,
                 gcf.path
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "path": row["path"], "commits": row["commits"],
            "additions": row["additions"], "deletions": row["deletions"],
            "authors": row["authors"],
            "last_committed_at": _iso(row["last_committed_at"]),
        }
        for row in rows
    ]


def ownership(store: GraphStore, path: str | None = None, limit: int = 10) -> list[dict]:
    """Observed author contributions (repo-wide or for one active file)."""
    where, args = "", []
    if path is not None:
        where = "WHERE gcf.path = ?"
        args.append(path)
    rows = store.conn.execute(
        f"""
        SELECT gc.author_name, gc.author_email,
               COUNT(DISTINCT gc.sha) AS commits,
               SUM(COALESCE(gcf.additions, 0)) AS additions,
               SUM(COALESCE(gcf.deletions, 0)) AS deletions,
               MIN(gc.committed_at) AS first_committed_at,
               MAX(gc.committed_at) AS last_committed_at
        FROM git_commits gc
        JOIN git_commit_files gcf ON gcf.sha = gc.sha
        {where}
        GROUP BY gc.author_email, gc.author_name
        ORDER BY commits DESC, last_committed_at DESC, gc.author_email
        LIMIT ?
        """,
        (*args, limit),
    ).fetchall()
    total = sum(row["commits"] for row in rows) or 1
    return [
        {
            "author_name": row["author_name"], "author_email": row["author_email"],
            "commits": row["commits"], "additions": row["additions"],
            "deletions": row["deletions"],
            "share": round(row["commits"] / total, 3),
            "first_committed_at": _iso(row["first_committed_at"]),
            "last_committed_at": _iso(row["last_committed_at"]),
        }
        for row in rows
    ]


# -- CLI/MCP surface reports ---------------------------------------------------

def _gated_report(root: Path, store: GraphStore, auto_index: bool, build) -> dict:
    from .freshness import ensure_fresh

    freshness = ensure_fresh(root, store, auto_index=auto_index)
    if not freshness["can_query"]:
        return {"status": freshness["status"], "freshness": freshness,
                "message": freshness["message"]}
    history = freshness.get("history") or {"status": "unknown"}
    if not history_serveable(history):
        return {
            "status": f"history_{history['status']}",
            "freshness": freshness, "history": history,
            "message": history.get("message")
                or history.get("error")
                or f"Git history is {history['status']}"
                   + (f": {history['reason']}" if history.get("reason") else "."),
        }
    result = build(store)
    result.update({
        "status": "ok", "freshness": freshness,
        "history": {**history, **history_provenance(store)},
    })
    result["text"] = _render_report(result)
    return result


def co_change_report(
    root: str | Path, store: GraphStore, filepath: str,
    *, limit: int = 20, auto_index: bool = True,
) -> dict:
    """Grounded co-change report for one indexed file."""
    from .graph.queries import resolve_file_path

    def build(store: GraphStore) -> dict:
        path = resolve_file_path(store, filepath)
        if path is None:
            return {"kind": "co_change", "target": filepath, "resolved_path": None,
                    "items": [], "explanation": CO_CHANGE_EXPLANATION}
        return {"kind": "co_change", "target": filepath, "resolved_path": path,
                "items": co_change_partners(store, path, limit=limit),
                "explanation": CO_CHANGE_EXPLANATION}

    return _gated_report(Path(root).resolve(), store, auto_index, build)


def churn_report(
    root: str | Path, store: GraphStore, *, limit: int = 20, auto_index: bool = True,
) -> dict:
    """Churn hotspot report over the extracted window."""
    return _gated_report(
        Path(root).resolve(), store, auto_index,
        lambda store: {"kind": "hotspots",
                       "items": churn_hotspots(store, limit=limit)},
    )


def ownership_report(
    root: str | Path, store: GraphStore, filepath: str | None = None,
    *, limit: int = 10, auto_index: bool = True,
) -> dict:
    """Observed-contribution report (never an authorization claim)."""
    from .graph.queries import resolve_file_path

    def build(store: GraphStore) -> dict:
        path = None
        if filepath is not None:
            path = resolve_file_path(store, filepath)
            if path is None:
                return {"kind": "ownership", "target": filepath,
                        "resolved_path": None, "items": [],
                        "disclaimer": OWNERSHIP_DISCLAIMER}
        return {"kind": "ownership", "target": filepath, "resolved_path": path,
                "items": ownership(store, path=path, limit=limit),
                "disclaimer": OWNERSHIP_DISCLAIMER}

    return _gated_report(Path(root).resolve(), store, auto_index, build)


def _render_report(result: dict) -> str:
    provenance = result["history"]
    header = (
        f"Git history evidence (head {str(provenance.get('head'))[:12]}, "
        f"{provenance.get('commit_count')} commit window)"
    )
    lines = [header]
    kind = result["kind"]
    if kind == "co_change":
        lines.append(f"Co-change partners of {result.get('resolved_path') or result['target']}")
        for item in result["items"]:
            commits = ", ".join(sha[:10] for sha in item["supporting_commits"][:5])
            more = len(item["supporting_commits"]) - 5
            if more > 0:
                commits += f" (+{more} more)"
            lines.append(
                f"- {item['partner_path']}  support={item['support']} "
                f"score={item['score']:.2f} confidence={item['confidence']:.2f} "
                f"commits: {commits}"
            )
        if not result["items"]:
            lines.append("- No co-change evidence in the extracted window."
                         if result.get("resolved_path")
                         else f"- '{result['target']}' is not an indexed file.")
        lines.append(f"\nHeuristic: {result['explanation']}")
    elif kind == "hotspots":
        lines.append("Churn hotspots")
        for item in result["items"]:
            lines.append(
                f"- {item['path']}  commits={item['commits']} "
                f"+{item['additions']}/-{item['deletions']} "
                f"authors={item['authors']} last={item['last_committed_at']}"
            )
        if not result["items"]:
            lines.append("- No commits touch currently indexed files in the window.")
    else:
        scope = result.get("resolved_path") or "repository"
        lines.append(f"Observed contributions for {scope}")
        for item in result["items"]:
            lines.append(
                f"- {item['author_name']} <{item['author_email']}>  "
                f"commits={item['commits']} share={item['share']:.0%} "
                f"+{item['additions']}/-{item['deletions']} "
                f"last={item['last_committed_at']}"
            )
        if not result["items"]:
            lines.append("- No contributions recorded in the extracted window."
                         if result.get("resolved_path") or result.get("target") is None
                         else f"- '{result['target']}' is not an indexed file.")
        lines.append(f"\nNote: {result['disclaimer']}")
    return "\n".join(lines) + "\n"

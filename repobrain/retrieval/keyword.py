"""Keyword search: FTS5 bm25 ranking + exact name and path match boosts."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..graph.store import GraphStore

# Score weights. FTS relevance is -bm25 (higher is better); boosts stack on top
# so that exact name matches outrank pure content matches.
EXACT_NAME_BOOST = 100.0
PARTIAL_NAME_BOOST = 25.0
PATH_MATCH_BOOST = 10.0


@dataclass
class SearchResult:
    path: str
    name: str
    node_type: str | None
    start_line: int | None
    end_line: int | None
    snippet: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "type": self.node_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "score": round(self.score, 3),
            "reason": ", ".join(self.reasons),
        }


def _fts_query(query: str) -> str:
    """Quote each token so user input can't break FTS5 query syntax."""
    tokens = [t.replace('"', '""') for t in query.split() if t]
    return " ".join(f'"{t}"' for t in tokens)


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so `%`/`_` in the query match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(
    store: GraphStore,
    query: str,
    limit: int = 10,
    node_type: str | None = None,
) -> list[SearchResult]:
    """Rank FTS hits, boosting exact/partial name matches and path substrings.

    Results are keyed by node id (via content_fts.node_id), so a hit is
    attributed to exactly one graph node and each boost applies at most once
    per result.
    """
    q = query.strip()
    if not q:
        return []
    results: dict[str, SearchResult] = {}

    fts = _fts_query(q)
    fts_rows: list[sqlite3.Row] = []
    if fts:
        try:
            type_clause = "AND n.type = ?" if node_type else ""
            params: list = [fts]
            if node_type:
                params.append(node_type)
            params.append(max(limit * 5, 50))
            fts_rows = store.conn.execute(
                f"""
                SELECT content_fts.path, content_fts.name, content_fts.node_id,
                       snippet(content_fts, 2, '[', ']', '…', 12) AS snip,
                       bm25(content_fts) AS rank,
                       n.type, n.start_line, n.end_line
                FROM content_fts
                LEFT JOIN nodes n ON n.id = content_fts.node_id
                WHERE content_fts MATCH ? {type_clause}
                ORDER BY rank LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            # e.g. punctuation-only query that tokenizes to nothing; fall
            # back to name/path matching only
            fts_rows = []
        for row in fts_rows:
            key = row["node_id"] or f"{row['path']}\x00{row['name']}"
            res = results.get(key)
            score = -row["rank"]
            if res is None:
                results[key] = SearchResult(
                    path=row["path"], name=row["name"], node_type=row["type"],
                    start_line=row["start_line"], end_line=row["end_line"],
                    snippet=row["snip"],
                    score=score, reasons=["full-text match"],
                )
            else:
                res.score = max(res.score, score)

    # Exact / partial node-name matches (may add rows FTS missed, e.g. names
    # not present in indexed content). One row per node id, so the boost is
    # applied at most once per result.
    like = f"%{_escape_like(q)}%"
    type_clause = "AND type = ?" if node_type else ""
    params = [q, like]
    if node_type:
        params.append(node_type)
    cur = store.conn.execute(
        rf"""
        SELECT id, name, path, type, start_line, end_line FROM nodes
        WHERE (lower(name) = lower(?) OR lower(name) LIKE lower(?) ESCAPE '\')
          {type_clause}
        LIMIT 200
        """,
        params,
    )
    for row in cur.fetchall():
        key = row["id"]
        exact = row["name"].lower() == q.lower()
        boost = EXACT_NAME_BOOST if exact else PARTIAL_NAME_BOOST
        reason = "exact name match" if exact else "name contains query"
        res = results.get(key)
        if res is None:
            res = results[key] = SearchResult(
                path=row["path"], name=row["name"], node_type=row["type"],
                start_line=row["start_line"], end_line=row["end_line"],
                snippet="", score=0.0, reasons=[],
            )
        else:
            res.node_type = row["type"]
            res.start_line = row["start_line"]
            res.end_line = row["end_line"]
        res.score += boost
        res.reasons.append(reason)

    # Path substring boost (once per result).
    q_lower = q.lower()
    for res in results.values():
        if q_lower in res.path.lower():
            res.score += PATH_MATCH_BOOST
            res.reasons.append("path match")

    # Attribute node type / line range for FTS-only hits via their node id.
    for key, res in results.items():
        if res.node_type is None and "\x00" not in key:
            row = store.conn.execute(
                "SELECT type, start_line, end_line FROM nodes WHERE id = ?",
                (key,),
            ).fetchone()
            if row is not None:
                res.node_type = row["type"]
                res.start_line = row["start_line"]
                res.end_line = row["end_line"]

    ranked = sorted(results.values(), key=lambda r: r.score, reverse=True)
    return ranked[:limit]

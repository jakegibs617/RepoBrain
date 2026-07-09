"""Keyword search: FTS5 bm25 ranking + exact name and path match boosts."""
from __future__ import annotations

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


def search(
    store: GraphStore,
    query: str,
    limit: int = 10,
    node_type: str | None = None,
) -> list[SearchResult]:
    """Rank FTS hits, boosting exact/partial name matches and path substrings."""
    results: dict[tuple[str, str], SearchResult] = {}
    q = query.strip()
    if not q:
        return []

    fts = _fts_query(q)
    if fts:
        cur = store.conn.execute(
            """
            SELECT path, name,
                   snippet(content_fts, 2, '[', ']', '…', 12) AS snip,
                   bm25(content_fts) AS rank
            FROM content_fts WHERE content_fts MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (fts, max(limit * 5, 50)),
        )
        for row in cur.fetchall():
            key = (row["path"], row["name"])
            res = results.get(key)
            score = -row["rank"]
            if res is None:
                results[key] = SearchResult(
                    path=row["path"], name=row["name"], node_type=None,
                    start_line=None, end_line=None, snippet=row["snip"],
                    score=score, reasons=["full-text match"],
                )
            else:
                res.score = max(res.score, score)

    # Exact / partial node-name matches (may add rows FTS missed, e.g. names
    # not present in indexed content).
    cur = store.conn.execute(
        """
        SELECT name, path, type, start_line, end_line FROM nodes
        WHERE lower(name) = lower(?) OR lower(name) LIKE lower(?)
        LIMIT 200
        """,
        (q, f"%{q}%"),
    )
    for row in cur.fetchall():
        key = (row["path"], row["name"])
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
        res.score += boost
        res.reasons.append(reason)

    # Path substring boost.
    q_lower = q.lower()
    for res in results.values():
        if q_lower in res.path.lower():
            res.score += PATH_MATCH_BOOST
            res.reasons.append("path match")

    # Attach node type / line range for FTS-only hits by joining on (path, name).
    # Whole-file content rows can map to both a File and a MarkdownDocument
    # node; prefer the caller's requested type, then File.
    for res in results.values():
        if res.node_type is None:
            row = store.conn.execute(
                """
                SELECT type, start_line, end_line FROM nodes
                WHERE path = ? AND name = ?
                ORDER BY CASE WHEN type = ? THEN 0
                              WHEN type = 'File' THEN 1
                              ELSE 2 END, start_line
                LIMIT 1
                """,
                (res.path, res.name, node_type or "File"),
            ).fetchone()
            if row is not None:
                res.node_type = row["type"]
                res.start_line = row["start_line"]
                res.end_line = row["end_line"]

    ranked = sorted(results.values(), key=lambda r: r.score, reverse=True)
    if node_type:
        ranked = [r for r in ranked if r.node_type == node_type]
    return ranked[:limit]

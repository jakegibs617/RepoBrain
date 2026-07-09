"""Reusable graph traversal queries (shared by the CLI now, MCP tools in M8).

Every function takes a GraphStore and returns plain dicts/lists so callers can
render them as text or JSON without re-shaping.
"""
from __future__ import annotations

import json
import posixpath

from .store import GraphStore

#: node types find_symbol searches, in ranking-priority order
SYMBOL_TYPES = ("Function", "Method", "Class", "Variable", "Module", "TestCase")

_CALLABLE_TYPES = ("Function", "Method", "TestCase")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _meta(row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _symbol_dict(row) -> dict:
    meta = _meta(row)
    return {
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "type": row["type"],
        "path": row["path"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "language": row["language"],
        "signature": meta.get("signature", ""),
    }


def find_symbol(
    store: GraphStore,
    name: str,
    exact: bool = False,
    limit: int = 20,
    types: tuple[str, ...] = SYMBOL_TYPES,
) -> list[dict]:
    """Find code symbols by name (exact matches ranked first)."""
    name = name.strip()
    if not name:
        return []
    placeholders = ",".join("?" for _ in types)
    type_rank = " ".join(
        f"WHEN type = '{t}' THEN {i}" for i, t in enumerate(types)
    )
    if exact:
        where = "lower(name) = lower(?)"
        args: list = [*types, name]
    else:
        like = f"%{_escape_like(name)}%"
        where = (
            r"(lower(name) LIKE lower(?) ESCAPE '\' "
            r"OR lower(qualified_name) LIKE lower(?) ESCAPE '\')"
        )
        args = [*types, like, like]
    rows = store.conn.execute(
        f"""
        SELECT name, qualified_name, type, path, start_line, end_line,
               language, metadata_json
        FROM nodes
        WHERE type IN ({placeholders}) AND {where}
        ORDER BY CASE WHEN lower(name) = lower(?) THEN 0 ELSE 1 END,
                 CASE {type_rank} ELSE 99 END,
                 name, path, start_line
        LIMIT ?
        """,
        (*args, name, limit),
    ).fetchall()
    return [_symbol_dict(r) for r in rows]


def resolve_file_path(store: GraphStore, filepath: str) -> str | None:
    """Normalize a user-supplied file path to an indexed relative path.

    Tries the path as given (relative to the indexed root), then a unique
    suffix match against the files table.
    """
    candidate = filepath.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    candidate = candidate.rstrip("/")
    root = store.get_meta("root")
    if root and candidate.startswith(root.replace("\\", "/") + "/"):
        candidate = candidate[len(root) + 1:]
    row = store.conn.execute(
        "SELECT path FROM files WHERE status = 'active' AND path = ?", (candidate,)
    ).fetchone()
    if row is not None:
        return row["path"]
    like = f"%/{_escape_like(candidate)}"
    rows = store.conn.execute(
        r"SELECT path FROM files WHERE status = 'active' AND path LIKE ? ESCAPE '\' LIMIT 2",
        (like,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["path"]
    return None


def _module_for_path(store: GraphStore, path: str):
    return store.conn.execute(
        "SELECT * FROM nodes WHERE type = 'Module' AND path = ? LIMIT 1", (path,)
    ).fetchone()


def _symbol_tree(store: GraphStore, path: str, module_id: str | None) -> list[dict]:
    """Nested symbol layout for a file: Module -> Class -> Method, etc."""
    sym_rows = store.conn.execute(
        """
        SELECT id, name, qualified_name, type, start_line, end_line, metadata_json
        FROM nodes
        WHERE path = ? AND type IN ('Class', 'Function', 'Method', 'Variable', 'TestCase')
        ORDER BY start_line, name
        """,
        (path,),
    ).fetchall()
    by_id = {
        r["id"]: {
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "type": r["type"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "signature": _meta(r).get("signature", ""),
            "children": [],
        }
        for r in sym_rows
    }
    parent_of: dict[str, str] = {}
    if by_id:
        placeholders = ",".join("?" for _ in by_id)
        edge_rows = store.conn.execute(
            f"""
            SELECT source_node_id, target_node_id FROM edges
            WHERE path = ? AND type IN ('DEFINES', 'CONTAINS')
              AND target_node_id IN ({placeholders})
            """,
            (path, *by_id),
        ).fetchall()
        for e in edge_rows:
            if e["source_node_id"] in by_id:
                parent_of[e["target_node_id"]] = e["source_node_id"]
    roots: list[dict] = []
    for nid, entry in by_id.items():
        parent = parent_of.get(nid)
        if parent is not None:
            by_id[parent]["children"].append(entry)
        else:
            roots.append(entry)
    return roots


def _imports_of(store: GraphStore, module_id: str) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT DISTINCT n.qualified_name, n.path, e.start_line
        FROM edges e JOIN nodes n ON n.id = e.target_node_id
        WHERE e.type = 'IMPORTS' AND e.source_node_id = ?
        ORDER BY n.qualified_name
        """,
        (module_id,),
    ).fetchall()
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(
            r["qualified_name"],
            {"module": r["qualified_name"], "path": r["path"], "line": r["start_line"]},
        )
    return list(seen.values())


def _imported_by(store: GraphStore, module_id: str) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT DISTINCT n.qualified_name, n.path
        FROM edges e JOIN nodes n ON n.id = e.source_node_id
        WHERE e.type = 'IMPORTS' AND e.target_node_id = ?
        ORDER BY n.path
        """,
        (module_id,),
    ).fetchall()
    return [{"module": r["qualified_name"], "path": r["path"]} for r in rows]


def _calls_out(store: GraphStore, path: str, limit: int) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT s.qualified_name AS caller, t.qualified_name AS callee,
               t.path AS callee_path, e.start_line, e.confidence, e.is_inferred
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'CALLS' AND e.path = ?
        ORDER BY e.start_line
        LIMIT ?
        """,
        (path, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _called_by(store: GraphStore, path: str, limit: int) -> list[dict]:
    rows = store.conn.execute(
        f"""
        SELECT s.qualified_name AS caller, s.path AS caller_path,
               t.qualified_name AS callee, e.start_line, e.confidence, e.is_inferred
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'CALLS' AND t.path = ? AND e.path != ?
              AND t.type IN ({",".join("?" for _ in _CALLABLE_TYPES)})
        ORDER BY s.path, e.start_line
        LIMIT ?
        """,
        (path, path, *_CALLABLE_TYPES, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _env_reads(store: GraphStore, path: str) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT t.name AS var, e.start_line, s.qualified_name AS reader
        FROM edges e
        JOIN nodes t ON t.id = e.target_node_id
        JOIN nodes s ON s.id = e.source_node_id
        WHERE e.type = 'READS_ENV' AND e.path = ?
        ORDER BY e.start_line
        """,
        (path,),
    ).fetchall()
    return [dict(r) for r in rows]


def _related_tests(store: GraphStore, path: str, module_id: str | None) -> list[dict]:
    tests: dict[str, dict] = {}
    if module_id is not None:
        # test files whose module imports this module
        rows = store.conn.execute(
            """
            SELECT tf.path, tf.name
            FROM edges e
            JOIN nodes m ON m.id = e.source_node_id AND m.type = 'Module'
            JOIN nodes tf ON tf.type = 'TestFile' AND tf.path = m.path
            WHERE e.type = 'IMPORTS' AND e.target_node_id = ?
            """,
            (module_id,),
        ).fetchall()
        for r in rows:
            tests.setdefault(r["path"], {"path": r["path"], "via": "imports this module"})
    # name-convention match: test file stem contains this file's stem
    stem = posixpath.splitext(posixpath.basename(path))[0].lower()
    if stem:
        rows = store.conn.execute(
            "SELECT path, name FROM nodes WHERE type = 'TestFile'"
        ).fetchall()
        for r in rows:
            tf_stem = posixpath.splitext(r["name"])[0].lower()
            core = tf_stem.removeprefix("test_").removesuffix("_test")
            core = core.removesuffix(".test").removesuffix(".spec")
            if core and (core in stem or stem in core) and r["path"] != path:
                tests.setdefault(r["path"], {"path": r["path"], "via": "name match"})
    return sorted(tests.values(), key=lambda t: t["path"])


def _doc_mentions(store: GraphStore, path: str) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT DISTINCT s.path, s.name, s.type
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'MENTIONS' AND t.path = ?
        """,
        (path,),
    ).fetchall()
    return [dict(r) for r in rows]


def explain_file(store: GraphStore, filepath: str, call_limit: int = 15) -> dict | None:
    """Assemble a grounded explanation of one indexed file."""
    path = resolve_file_path(store, filepath)
    if path is None:
        return None
    file_row = store.conn.execute(
        "SELECT * FROM nodes WHERE type = 'File' AND path = ? LIMIT 1", (path,)
    ).fetchone()
    module = _module_for_path(store, path)
    module_id = module["id"] if module is not None else None
    module_meta = _meta(module) if module is not None else {}

    counts = {
        r["type"]: r["c"]
        for r in store.conn.execute(
            "SELECT type, COUNT(*) AS c FROM nodes WHERE path = ? GROUP BY type ORDER BY c DESC",
            (path,),
        ).fetchall()
    }
    return {
        "path": path,
        "language": file_row["language"] if file_row is not None else None,
        "line_count": file_row["end_line"] if file_row is not None else None,
        "module": (
            {
                "qualified_name": module["qualified_name"],
                "is_test_file": bool(module_meta.get("is_test_file")),
            }
            if module is not None
            else None
        ),
        "node_counts": counts,
        "symbols": _symbol_tree(store, path, module_id),
        "imports": {
            "internal": _imports_of(store, module_id) if module_id else [],
            "external": module_meta.get("external_imports", []),
        },
        "imported_by": _imported_by(store, module_id) if module_id else [],
        "calls_out": _calls_out(store, path, call_limit),
        "called_by": _called_by(store, path, call_limit),
        "env_vars": _env_reads(store, path),
        "tests": _related_tests(store, path, module_id),
        "docs": _doc_mentions(store, path),
    }

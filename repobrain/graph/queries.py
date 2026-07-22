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
_CODE_TARGET_TYPES = SYMBOL_TYPES + ("Table", "Route", "Endpoint")

_CALLABLE_TYPES = ("Function", "Method", "TestCase")


def trace_config(store: GraphStore, name: str) -> dict:
    """Trace a config/environment name from definitions to runtime reads.

    EnvVar nodes are repository-global, so definitions emitted by dotenv/YAML
    parsers and READS_ENV edges emitted by code parsers meet at one stable node.
    Generic ConfigKey matches are included even when a format-specific adapter
    cannot assert that the key sets an environment variable.
    """
    name = name.strip()
    result = {"name": name, "definitions": [], "usages": []}
    if not name:
        return result
    env = store.conn.execute(
        "SELECT id FROM nodes WHERE type = 'EnvVar' AND lower(name) = lower(?) LIMIT 1",
        (name,),
    ).fetchone()
    if env is not None:
        rows = store.conn.execute(
            """
            SELECT s.name, s.qualified_name, s.type, s.path, s.start_line,
                   e.type AS edge_type, e.metadata_json
            FROM edges e JOIN nodes s ON s.id = e.source_node_id
            WHERE e.target_node_id = ? AND e.type IN ('SETS_ENV', 'READS_ENV')
            ORDER BY CASE e.type WHEN 'SETS_ENV' THEN 0 ELSE 1 END,
                     s.path, e.start_line, s.name
            """,
            (env["id"],),
        ).fetchall()
        for row in rows:
            item = {
                "name": row["name"], "qualified_name": row["qualified_name"],
                "type": row["type"], "path": row["path"],
                "start_line": row["start_line"], "relation": row["edge_type"],
                "metadata": _meta(row),
            }
            result["definitions" if row["edge_type"] == "SETS_ENV" else "usages"].append(item)

    # Generic YAML keys can define application config without an explicit env
    # adapter. Avoid duplicating keys already represented by SETS_ENV.
    rows = store.conn.execute(
        """
        SELECT name, qualified_name, type, path, start_line, metadata_json
        FROM nodes
        WHERE type = 'ConfigKey'
          AND (lower(name) = lower(?) OR lower(qualified_name) = lower(?)
               OR lower(qualified_name) LIKE lower(?))
        ORDER BY path, start_line
        """,
        (name, name, f"%.{name}"),
    ).fetchall()
    seen = {(d["path"], d["start_line"], d["name"]) for d in result["definitions"]}
    for row in rows:
        key = (row["path"], row["start_line"], row["name"])
        if key not in seen:
            result["definitions"].append({
                "name": row["name"], "qualified_name": row["qualified_name"],
                "type": row["type"], "path": row["path"],
                "start_line": row["start_line"], "relation": "DECLARES_CONFIG",
                "metadata": _meta(row),
            })
    return result


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


def resolve_graph_reference(store: GraphStore, value: str) -> dict:
    """Resolve one exact file path or unambiguous symbol reference.

    This is the shared deterministic boundary for memory anchoring. It never
    fuzzy-matches: paths use ``resolve_file_path`` and symbols use the same
    exact name/qualified-name uniqueness rule as Markdown reconciliation.
    """
    raw = value.strip()
    if not raw:
        return {"status": "unresolved", "reference": value,
                "provenance": "empty-reference"}
    path = resolve_file_path(store, raw)
    if path is not None:
        row = store.conn.execute(
            "SELECT id,type,name,qualified_name,path,start_line,end_line "
            "FROM nodes WHERE type='File' AND path=? LIMIT 1", (path,),
        ).fetchone()
        if row is not None:
            normalized = raw.replace("\\", "/").removeprefix("./")
            return {
                "status": "resolved", "reference": value,
                "provenance": "exact-path" if normalized == path else "unique-path-suffix",
                "node": dict(row),
            }
    marks = ",".join("?" for _ in SYMBOL_TYPES)
    rows = store.conn.execute(
        f"SELECT id,type,name,qualified_name,path,start_line,end_line FROM nodes "
        f"WHERE type IN ({marks}) AND (name=? OR qualified_name=?) "
        "ORDER BY path,start_line,id LIMIT 3",
        (*SYMBOL_TYPES, raw, raw),
    ).fetchall()
    unique = {row["id"]: row for row in rows}
    if len(unique) == 1:
        row = next(iter(unique.values()))
        return {
            "status": "resolved", "reference": value,
            "provenance": (
                "exact-qualified-symbol" if raw == row["qualified_name"]
                and raw != row["name"] else "unique-symbol-name"
            ),
            "node": dict(row),
        }
    if len(unique) > 1:
        return {
            "status": "ambiguous", "reference": value,
            "provenance": "ambiguous-exact-symbol",
            "candidates": [dict(row) for row in unique.values()],
        }
    return {"status": "unresolved", "reference": value,
            "provenance": "no-exact-match"}


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


def _instantiates_out(store: GraphStore, path: str, limit: int) -> list[dict]:
    """Classes this file's callables construct (D32 INSTANTIATES)."""
    rows = store.conn.execute(
        """
        SELECT s.qualified_name AS caller, t.qualified_name AS class_,
               t.path AS class_path, e.start_line, e.confidence, e.is_inferred
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'INSTANTIATES' AND e.path = ?
        ORDER BY e.start_line
        LIMIT ?
        """,
        (path, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _instantiated_by(store: GraphStore, path: str, limit: int) -> list[dict]:
    """Callables elsewhere that construct a Class defined in this file."""
    rows = store.conn.execute(
        """
        SELECT s.qualified_name AS caller, s.path AS caller_path,
               t.qualified_name AS class_, e.start_line, e.confidence, e.is_inferred
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'INSTANTIATES' AND t.path = ? AND e.path != ?
        ORDER BY s.path, e.start_line
        LIMIT ?
        """,
        (path, path, limit),
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


def _reference_value(row) -> str:
    return str(_meta(row).get("raw_value") or "")


def _resolve_code_target(store: GraphStore, target: str):
    """Resolve a file path first, then an exact unambiguous symbol."""
    path = resolve_file_path(store, target)
    if path is not None:
        return ("path", path)
    target = target.strip()
    if not target:
        return None
    placeholders = ",".join("?" for _ in _CODE_TARGET_TYPES)
    rows = store.conn.execute(
        f"""
        SELECT id, name, qualified_name, type, path
        FROM nodes
        WHERE type IN ({placeholders})
          AND (name = ? OR qualified_name = ?)
        ORDER BY path, start_line
        """,
        (*_CODE_TARGET_TYPES, target, target),
    ).fetchall()
    unique = {row["id"]: row for row in rows}
    if len(unique) != 1:
        return None
    return ("node", next(iter(unique.values()))["id"])


def docs_for_code(store: GraphStore, target: str, limit: int = 50) -> list[dict]:
    """Return documentation references to an indexed file or unique symbol."""
    resolved = _resolve_code_target(store, target)
    if resolved is None or limit <= 0:
        return []
    mode, value = resolved
    target_clause = "t.path = ?" if mode == "path" else "t.id = ?"
    rows = store.conn.execute(
        f"""
        SELECT s.path AS doc_path, s.name AS section, s.type AS doc_type,
               e.start_line, e.confidence, e.metadata_json,
               t.name AS target_name, t.type AS target_type,
               t.path AS target_path
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'MENTIONS' AND {target_clause}
        ORDER BY e.confidence DESC, s.path, e.start_line, t.type, t.name
        LIMIT ?
        """,
        (value, limit),
    ).fetchall()
    return [
        {
            "doc_path": row["doc_path"],
            "section": row["section"],
            "doc_type": row["doc_type"],
            "start_line": row["start_line"],
            "reference": _reference_value(row),
            "target_name": row["target_name"],
            "target_type": row["target_type"],
            "target_path": row["target_path"],
            "confidence": row["confidence"],
        }
        for row in rows
    ]


def code_for_docs(
    store: GraphStore,
    doc_path: str,
    heading: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return graph targets referenced by a Markdown document or heading."""
    path = resolve_file_path(store, doc_path)
    if path is None or limit <= 0:
        return []
    args: list = [path]
    heading_clause = ""
    if heading is not None:
        heading_clause = "AND s.type = 'MarkdownSection' AND lower(s.name) = lower(?)"
        args.append(heading.strip())
    args.append(limit)
    rows = store.conn.execute(
        f"""
        SELECT s.path AS source_path, s.name AS section, e.start_line,
               e.confidence, e.metadata_json,
               t.name, t.qualified_name, t.type, t.path,
               t.start_line AS target_start_line
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = 'MENTIONS' AND s.path = ? {heading_clause}
        ORDER BY e.confidence DESC, e.start_line, t.path, t.start_line
        LIMIT ?
        """,
        args,
    ).fetchall()
    return [
        {
            "source_path": row["source_path"],
            "section": row["section"],
            "start_line": row["start_line"],
            "reference": _reference_value(row),
            "name": row["name"],
            "qualified_name": row["qualified_name"] or "",
            "type": row["type"],
            "path": row["path"],
            "target_start_line": row["target_start_line"],
            "confidence": row["confidence"],
        }
        for row in rows
    ]


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
        "instantiates": _instantiates_out(store, path, call_limit),
        "instantiated_by": _instantiated_by(store, path, call_limit),
        "env_vars": _env_reads(store, path),
        "tests": _related_tests(store, path, module_id),
        "docs": _doc_mentions(store, path),
    }


def _node_payload(row) -> dict:
    return {
        "id": row["id"], "type": row["type"], "name": row["name"],
        "qualified_name": row["qualified_name"] or "", "path": row["path"] or "",
        "start_line": row["start_line"], "end_line": row["end_line"],
    }


def trace_data_flow(store: GraphStore, start: str, depth: int = 4,
                    direction: str = "both", limit: int = 200) -> dict | None:
    """Trace a route/symbol/file through runtime-oriented graph edges."""
    if direction not in {"in", "out", "both"}:
        raise ValueError("direction must be 'in', 'out', or 'both'")
    row = store.conn.execute(
        "SELECT * FROM nodes WHERE type IN ('Route','Endpoint','Event','Queue') "
        "AND lower(name)=lower(?) ORDER BY type LIMIT 1", (start.strip(),),
    ).fetchone()
    if row is None:
        resolved = _resolve_code_target(store, start)
        if resolved:
            mode, value = resolved
            row = store.conn.execute(
                "SELECT * FROM nodes WHERE " + ("path=?" if mode == "path" else "id=?") +
                " ORDER BY CASE type WHEN 'Module' THEN 0 WHEN 'File' THEN 1 ELSE 2 END LIMIT 1",
                (value,),
            ).fetchone()
    if row is None:
        return None

    edge_types = (
        "HANDLES_ROUTE", "EXPOSES_ENDPOINT", "CALLS", "INSTANTIATES", "IMPORTS",
        "READS", "WRITES", "READS_ENV", "USES_CONFIG", "PUBLISHES_EVENT",
        "CONSUMES_EVENT", "READS_TABLE", "WRITES_TABLE", "DEPENDS_ON",
        "DEFINES", "CONTAINS",
    )
    seen = {row["id"]}
    frontier = [(row["id"], 0)]
    nodes = {row["id"]: _node_payload(row)}
    edges: list[dict] = []
    while frontier and len(edges) < limit:
        current, level = frontier.pop(0)
        if level >= max(0, depth):
            continue
        clauses, args = [], []
        if direction in {"out", "both"}:
            clauses.append("source_node_id=?")
            args.append(current)
            # A callable inherits file/module-level imports and config wiring;
            # walk to its structural parent even during an outward trace.
            clauses.append("(target_node_id=? AND type IN ('DEFINES','CONTAINS'))")
            args.append(current)
        if direction in {"in", "both"}:
            clauses.append("target_node_id=?")
            args.append(current)
        placeholders = ",".join("?" for _ in edge_types)
        rows = store.conn.execute(
            f"SELECT * FROM edges WHERE type IN ({placeholders}) AND ({' OR '.join(clauses)}) "
            "ORDER BY confidence DESC, path, start_line", (*edge_types, *args),
        ).fetchall()
        for edge in rows:
            other = edge["target_node_id"] if edge["source_node_id"] == current else edge["source_node_id"]
            target = store.conn.execute("SELECT * FROM nodes WHERE id=?", (other,)).fetchone()
            if target is None:
                continue
            next_level = level if edge["type"] in {"DEFINES", "CONTAINS"} else level + 1
            record = {
                "type": edge["type"], "source": edge["source_node_id"],
                "target": edge["target_node_id"], "path": edge["path"],
                "start_line": edge["start_line"], "confidence": edge["confidence"],
                "inferred": bool(edge["is_inferred"]), "depth": next_level,
            }
            if record not in edges:
                edges.append(record)
            nodes[other] = _node_payload(target)
            if other not in seen:
                seen.add(other)
                frontier.append((other, next_level))
    return {"start": _node_payload(row), "nodes": list(nodes.values()), "edges": edges}


_CO_CHANGE_NODE_COLUMNS = ("id", "type", "name", "qualified_name", "path",
                           "start_line", "end_line")


def historical_co_change(store: GraphStore, paths: list[str], limit: int = 20,
                         only_external: bool = False) -> dict:
    """Separately labeled historical-evidence bucket for impact answers.

    Co-change edges are correlation mined from recent Git history; they carry
    their supporting commit ids and stay below static-impact confidence, so
    callers must present them apart from observed dependency evidence.
    With ``only_external`` the limit applies after excluding pairs entirely
    inside ``paths``: intra-diff pairs rank highest (they co-change the most)
    and would otherwise starve the unchanged partners callers actually want.
    """
    from ..history import CO_CHANGE_EXPLANATION, history_provenance

    items = []
    if paths:
        marks = ",".join("?" for _ in paths)
        select = ", ".join(
            f"{alias}.{column} AS {alias}_{column}"
            for alias in ("s", "t") for column in _CO_CHANGE_NODE_COLUMNS
        )
        external = (
            f"AND ((s.path IN ({marks})) + (t.path IN ({marks}))) = 1"
            if only_external else ""
        )
        args = [*paths, *paths] + ([*paths, *paths] if only_external else [])
        rows = store.conn.execute(
            f"""
            SELECT {select}, e.confidence, e.metadata_json
            FROM edges e
            JOIN nodes s ON s.id = e.source_node_id
            JOIN nodes t ON t.id = e.target_node_id
            WHERE e.type = 'CO_CHANGED_WITH'
              AND (s.path IN ({marks}) OR t.path IN ({marks}))
              {external}
            ORDER BY e.confidence DESC, s.path, t.path
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        changed = set(paths)
        for row in rows:
            queried_side = "s" if row["s_path"] in changed else "t"
            partner_side = "t" if queried_side == "s" else "s"
            partner = {column: row[f"{partner_side}_{column}"]
                       for column in _CO_CHANGE_NODE_COLUMNS}
            meta = _meta(row)
            items.append({
                "node": _node_payload(partner), "via": "CO_CHANGED_WITH",
                "co_changed_with": row[f"{queried_side}_path"],
                "partner_changed": partner["path"] in changed,
                "support": meta.get("support"), "score": meta.get("score"),
                "confidence": row["confidence"],
                "supporting_commits": meta.get("supporting_commits", []),
                "supporting_commits_truncated":
                    bool(meta.get("supporting_commits_truncated", False)),
                "evidence": "git-history",
            })
    return {
        "items": items,
        "explanation": CO_CHANGE_EXPLANATION,
        "provenance": history_provenance(store),
    }


def impact_analysis(store: GraphStore, target: str, change_type: str = "modify",
                    depth: int = 3, include_history: bool = True,
                    history: dict | None = None) -> dict | None:
    """Estimate change blast radius with confidence buckets and evidence.

    ``history`` is the freshness gate's refresh_history envelope: when the
    caller passes one that is not serveable (stale/unavailable/error), the
    historical bucket is withheld with the reason instead of quietly serving
    evidence extracted at a different HEAD.
    """
    resolved = _resolve_code_target(store, target)
    if resolved is None:
        return None
    mode, value = resolved
    start_rows = store.conn.execute(
        "SELECT * FROM nodes WHERE " + ("path=?" if mode == "path" else "id=?"), (value,),
    ).fetchall()
    if not start_rows:
        return None
    ids = {r["id"] for r in start_rows}
    frontier = [(nid, 0) for nid in ids]
    evidence: dict[str, dict] = {}
    while frontier:
        nid, level = frontier.pop(0)
        if level >= depth:
            continue
        rows = store.conn.execute(
            "SELECT e.source_node_id AS source_id, e.type AS edge_type, e.path AS edge_path, e.start_line AS edge_line, "
            "e.confidence AS edge_confidence, n.* FROM edges e JOIN nodes n ON n.id=e.source_node_id "
            "WHERE e.target_node_id=? AND e.type IN "
            "('IMPORTS','CALLS','INSTANTIATES','MENTIONS','TESTS','COVERS','READS_ENV',"
            "'USES_CONFIG','DEPENDS_ON','HANDLES_ROUTE','READS_TABLE','WRITES_TABLE')",
            (nid,),
        ).fetchall()
        for r in rows:
            source = r["source_id"]
            if source in ids:
                continue
            ids.add(source)
            confidence = float(r["edge_confidence"]) * (0.85 ** level)
            evidence[source] = {
                "node": _node_payload(r), "via": r["edge_type"], "confidence": round(confidence, 3),
                "evidence_path": r["edge_path"], "evidence_line": r["edge_line"],
            }
            frontier.append((source, level + 1))
    items = list(evidence.values())
    high = [x for x in items if x["confidence"] >= 0.85]
    medium = [x for x in items if 0.6 <= x["confidence"] < 0.85]
    low = [x for x in items if x["confidence"] < 0.6]
    tests = [x for x in items if x["node"]["type"] in {"TestFile", "TestCase"} or "/test" in x["node"]["path"]]
    docs = [x for x in items if x["node"]["type"] in {"MarkdownDocument", "MarkdownSection", "ADR"}]
    from ..history import history_serveable, history_status_message

    start_paths = sorted({r["path"] for r in start_rows if r["path"]})
    if not include_history:
        historical = {"items": [], "explanation": "History excluded by caller.",
                      "provenance": None}
    elif history is not None and not history_serveable(history):
        historical = {"items": [], "status": history.get("status"),
                      "explanation": history_status_message(history),
                      "provenance": None}
    else:
        historical = historical_co_change(store, start_paths)
    return {
        "target": target, "change_type": change_type,
        "high_confidence": high, "medium_confidence": medium,
        "low_confidence": low, "recommended_tests": tests,
        "docs_likely_needing_updates": docs,
        "historical_evidence": historical,
        "unknowns": ["Unsupported dynamic calls and runtime-only wiring are not statically observable."],
    }

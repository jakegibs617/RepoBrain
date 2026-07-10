"""Per-repository MCP server exposing RepoBrain's agent-facing API."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import RepoBrainConfig
from .graph.queries import code_for_docs, docs_for_code, explain_file, find_symbol
from .graph.store import GraphStore
from .indexing.indexer import Indexer
from .memory import read_agent_memory, write_agent_memory
from .retrieval.keyword import search


def _safe(value: Any) -> Any:
    """Recursively convert rows, enums, paths, and tuples to JSON values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict) or hasattr(value, "keys"):
        return {str(key): _safe(value[key]) for key in value.keys()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        return _safe(value.to_dict())
    return str(value)


class RepoBrainTools:
    """Transport-independent implementation of MCP tools (also easy to test)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _store(self) -> GraphStore:
        config = RepoBrainConfig.load(self.root)
        db_path = self.root / config.db_path
        if not db_path.exists():
            raise ValueError(f"No RepoBrain database at {db_path}; call index_repo first")
        return GraphStore(db_path)

    def index_repo(self, path: str = ".", incremental: bool = True,
                   include_patterns: list[str] | None = None,
                   exclude_patterns: list[str] | None = None) -> dict:
        target = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        config = RepoBrainConfig.load(target)
        if include_patterns:
            config.include_patterns = include_patterns
        if exclude_patterns:
            config.exclude_patterns = exclude_patterns
        with GraphStore(target / config.db_path) as store:
            stats = Indexer(store, config=config).index(target, incremental=incremental)
        return _safe({"status": "ok", "files_scanned": stats.files_scanned,
                      "files_changed": stats.files_changed, "files_deleted": stats.files_deleted,
                      "nodes_created": stats.nodes_created, "edges_created": stats.edges_created,
                      "warnings": stats.warnings})

    def search_project(self, query: str, limit: int = 10,
                       types: list[str] | None = None) -> dict:
        with self._store() as store:
            if not types:
                results = search(store, query, limit=limit)
            else:
                combined = []
                for type_ in types:
                    combined.extend(search(store, query, limit=limit, node_type=type_))
                results = sorted(combined, key=lambda item: item.score, reverse=True)[:limit]
        return {"status": "ok", "results": [_safe(result.to_dict()) for result in results]}

    def explain_project(self, focus: str = "overall") -> dict:
        with self._store() as store:
            sample = store.conn.execute(
                "SELECT path, type, name FROM nodes WHERE type IN ('Module','MarkdownDocument',"
                "'ConfigFile','Route','TestFile') ORDER BY path LIMIT 100"
            ).fetchall()
            result = {"status": "ok", "focus": focus, "files": store.file_count(),
                      "nodes_by_type": store.counts_by_type("nodes"),
                      "edges_by_type": store.counts_by_type("edges"),
                      "key_nodes": [_safe(row) for row in sample]}
        return _safe(result)

    def explain_file(self, path: str) -> dict:
        with self._store() as store:
            result = explain_file(store, path)
        return {"status": "ok" if result else "not_found", "file": _safe(result)}

    def find_symbol(self, name: str, exact: bool = False, limit: int = 20) -> dict:
        with self._store() as store:
            result = find_symbol(store, name, exact=exact, limit=limit)
        return {"status": "ok", "symbols": _safe(result)}

    def _trace(self, start: str, depth: int, direction: str = "both") -> dict:
        depth = max(0, min(depth, 10))
        with self._store() as store:
            starts = store.conn.execute(
                "SELECT id, type, name, path FROM nodes WHERE lower(name)=lower(?) "
                "OR lower(qualified_name)=lower(?) OR path=? LIMIT 20", (start, start, start)
            ).fetchall()
            frontier = {row["id"] for row in starts}
            seen = set(frontier)
            edges = []
            for _ in range(depth):
                if not frontier:
                    break
                placeholders = ",".join("?" for _ in frontier)
                clauses, params = [], []
                if direction in ("out", "both"):
                    clauses.append(f"source_node_id IN ({placeholders})")
                    params.extend(frontier)
                if direction in ("in", "both"):
                    clauses.append(f"target_node_id IN ({placeholders})")
                    params.extend(frontier)
                found = store.conn.execute(
                    f"SELECT * FROM edges WHERE {' OR '.join(clauses)}", params
                ).fetchall()
                edges.extend(_safe(row) for row in found)
                adjacent = {value for row in found for value in
                            (row["source_node_id"], row["target_node_id"])}
                frontier = adjacent - seen
                seen.update(frontier)
            nodes = []
            if seen:
                placeholders = ",".join("?" for _ in seen)
                nodes = store.conn.execute(
                    f"SELECT id,type,name,qualified_name,path,start_line,end_line FROM nodes "
                    f"WHERE id IN ({placeholders})", list(seen)
                ).fetchall()
        return {"status": "ok", "start": start, "nodes": _safe(nodes), "edges": edges}

    def trace_symbol(self, symbol: str, depth: int = 2) -> dict:
        return self._trace(symbol, depth)

    def trace_config(self, key: str, depth: int = 3) -> dict:
        return self._trace(key, depth)

    def trace_data_flow(self, start: str, depth: int = 4, direction: str = "both") -> dict:
        if direction not in ("in", "out", "both"):
            raise ValueError("direction must be in, out, or both")
        return self._trace(start, depth, direction)

    def impact_analysis(self, target: str, change_type: str = "modify") -> dict:
        trace = self._trace(target, 3, "both")
        nodes = trace["nodes"]
        files = sorted({node["path"] for node in nodes if node.get("path") and node["path"] != target})
        tests = [path for path in files if "test" in path.lower()]
        docs = [path for path in files if path.lower().endswith((".md", ".mdx"))]
        config = [path for path in files if any(part in path.lower() for part in
                                                ("config", ".env", "yaml", "toml"))]
        return {"status": "ok", "target": target, "change_type": change_type,
                "impacted_files": files, "recommended_tests": tests,
                "docs_likely_needing_updates": docs, "impacted_config": config,
                "confidence": 0.8 if trace["edges"] else 0.2,
                "evidence": trace["edges"]}

    def docs_for_code(self, target: str, limit: int = 50) -> dict:
        with self._store() as store:
            result = docs_for_code(store, target, limit=limit)
        return {"status": "ok", "documents": _safe(result)}

    def code_for_docs(self, doc_path: str, heading: str | None = None,
                      limit: int = 50) -> dict:
        with self._store() as store:
            result = code_for_docs(store, doc_path, heading=heading, limit=limit)
        return {"status": "ok", "code": _safe(result)}

    def write_agent_memory(self, summary: str, decisions: list[str] | None = None,
                           assumptions: list[str] | None = None,
                           open_questions: list[str] | None = None,
                           changed_files: list[str] | None = None,
                           next_steps: list[str] | None = None) -> dict:
        return write_agent_memory(self.root, summary, decisions=decisions,
                                  assumptions=assumptions, open_questions=open_questions,
                                  changed_files=changed_files, next_steps=next_steps)

    def read_agent_memory(self, topic: str | None = None, limit: int = 10) -> dict:
        return read_agent_memory(self.root, topic=topic, limit=limit)


def create_server(root: str | Path):
    """Create a FastMCP server, importing the optional SDK only when needed."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("RepoBrain")
    tools = RepoBrainTools(root)
    for name in (
        "index_repo", "search_project", "explain_project", "explain_file", "find_symbol",
        "trace_symbol", "trace_config", "trace_data_flow", "impact_analysis",
        "docs_for_code", "code_for_docs", "write_agent_memory", "read_agent_memory",
    ):
        server.tool(name=name)(getattr(tools, name))
    return server


def run_server(root: str | Path) -> None:
    create_server(root).run(transport="stdio")

"""Per-repository MCP server exposing RepoBrain's agent-facing API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import RepoBrainConfig
from .diagnostics import configure_logging_from_env, log_event
from .briefing import DEFAULT_BUDGET, project_brief
from .change_context import DEFAULT_CHANGE_BUDGET, GitDiffError, change_context
from .graph.queries import (
    CHANGE_TYPES,
    code_for_docs,
    docs_for_code,
    explain_file,
    find_symbol,
    impact_analysis,
    trace_config,
    trace_data_flow,
    trace_symbol,
)
from .graph.store import GraphStore
from .freshness import ensure_fresh
from .history import (
    co_change_report,
    churn_report,
    ownership_report,
    refresh_history,
)
from .indexing.indexer import Indexer
from .memory import read_agent_memory, verify_agent_memory, write_agent_memory
from .reporting import project_overview
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

    def _query(self, callback, *, auto_index: bool = True) -> dict:
        """Run one MCP read behind the shared freshness gate."""
        with self._store() as store:
            freshness = ensure_fresh(self.root, store, auto_index=auto_index)
            if not freshness["can_query"]:
                log_event(
                    "mcp.query.blocked",
                    status=freshness["status"],
                    can_query=False,
                    auto_index=auto_index,
                )
                return {"status": freshness["status"], "freshness": _safe(freshness)}
            store.last_freshness = freshness
            result = callback(store)
        result = _safe(result)
        result["freshness"] = _safe(freshness)
        log_event(
            "mcp.query.completed",
            status=result.get("status", "ok"),
            can_query=True,
            auto_index=auto_index,
        )
        return result

    def index_repo(self, path: str = ".", incremental: bool = True,
                   include_patterns: list[str] | None = None,
                   exclude_patterns: list[str] | None = None) -> dict:
        target = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if target != self.root:
            raise ValueError(
                f"RepoBrain MCP is scoped to {self.root}; refusing to index {target}"
            )
        config = RepoBrainConfig.load(target)
        if include_patterns:
            config.include_patterns = include_patterns
        if exclude_patterns:
            config.exclude_patterns = exclude_patterns
        with GraphStore(target / config.db_path) as store:
            stats = Indexer(store, config=config).index(target, incremental=incremental)
            history = refresh_history(target, store, config=config)
        log_event(
            "mcp.index.completed",
            status="ok",
            incremental=incremental,
            files_scanned=stats.files_scanned,
            files_changed=stats.files_changed,
            files_deleted=stats.files_deleted,
            nodes_created=stats.nodes_created,
            edges_created=stats.edges_created,
        )
        return _safe({"status": "ok", "files_scanned": stats.files_scanned,
                      "files_changed": stats.files_changed, "files_deleted": stats.files_deleted,
                      "nodes_created": stats.nodes_created, "edges_created": stats.edges_created,
                      "warnings": stats.warnings, "history": history})

    def search_project(self, query: str, limit: int = 10,
                       types: list[str] | None = None, auto_index: bool = True) -> dict:
        def run(store):
            if not types:
                results = search(store, query, limit=limit)
            else:
                combined = []
                for type_ in types:
                    combined.extend(search(store, query, limit=limit, node_type=type_))
                results = sorted(combined, key=lambda item: item.score, reverse=True)[:limit]
            return {"status": "ok", "results": [_safe(result.to_dict()) for result in results]}
        return self._query(run, auto_index=auto_index)

    def explain_project(self, focus: str = "overall", auto_index: bool = True) -> dict:
        return self._query(
            lambda store: {"status": "ok", "focus": focus, **project_overview(store)},
            auto_index=auto_index,
        )

    def project_brief(self, budget: int = DEFAULT_BUDGET, auto_index: bool = True) -> dict:
        return self._query(
            lambda store: project_brief(self.root, store, budget=budget, auto_index=False),
            auto_index=auto_index,
        )

    def change_context(self, base: str | None = None, auto_index: bool = True,
                       budget: int = DEFAULT_CHANGE_BUDGET) -> dict:
        """Grounded context for the current diff, budgeted as the CLI is (D48)."""
        try:
            with self._store() as store:
                return _safe(change_context(self.root, store, base=base,
                                            auto_index=auto_index,
                                            include_text=False,
                                            budget=budget))
        except GitDiffError as exc:
            return {"status": "error", "error": str(exc), "changes": []}

    def co_change(self, path: str, limit: int = 20, auto_index: bool = True) -> dict:
        """Files that historically change together with `path` (heuristic evidence)."""
        with self._store() as store:
            return _safe(co_change_report(self.root, store, path, limit=limit,
                                          auto_index=auto_index))

    def churn_hotspots(self, limit: int = 20, auto_index: bool = True) -> dict:
        """Churn hotspots (commit counts, added/deleted lines) from recent history."""
        with self._store() as store:
            return _safe(churn_report(self.root, store, limit=limit,
                                      auto_index=auto_index))

    def ownership(self, path: str | None = None, limit: int = 10,
                  auto_index: bool = True) -> dict:
        """Observed contribution history; never an authorization or CODEOWNERS claim."""
        with self._store() as store:
            return _safe(ownership_report(self.root, store, path, limit=limit,
                                          auto_index=auto_index))

    def explain_file(self, path: str, auto_index: bool = True) -> dict:
        def run(store):
            result = explain_file(store, path)
            return {"status": "ok" if result else "not_found", "file": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def find_symbol(self, name: str, exact: bool = False, limit: int = 20,
                    auto_index: bool = True) -> dict:
        def run(store):
            result = find_symbol(store, name, exact=exact, limit=limit)
            return {"status": "ok", "symbols": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def trace_symbol(self, symbol: str, depth: int = 2, auto_index: bool = True,
                     direction: str = "both") -> dict:
        """Trace graph edges around SYMBOL; depth counts edges (0-10)."""
        def run(store):
            result = trace_symbol(store, symbol, depth=depth, direction=direction)
            return {"status": "ok", **result}
        return self._query(run, auto_index=auto_index)

    def trace_config(self, key: str, depth: int = 3, auto_index: bool = True) -> dict:
        """Find direct config definitions and reads.

        ``depth`` remains accepted for MCP client compatibility, but config
        tracing is a direct lookup rather than a recursive graph traversal.
        """
        def run(store):
            result = trace_config(store, key)
            return {"status": "ok", **_safe(result)}
        return self._query(run, auto_index=auto_index)

    def trace_data_flow(self, start: str, depth: int = 4, direction: str = "both",
                        auto_index: bool = True) -> dict:
        if direction not in ("in", "out", "both"):
            raise ValueError("direction must be in, out, or both")
        def run(store):
            result = trace_data_flow(store, start, depth=depth, direction=direction)
            return {"status": "ok" if result else "not_found", "flow": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def impact_analysis(self, target: str, change_type: str = "modify",
                        auto_index: bool = True) -> dict:
        """Estimate impact for a modify/modified, added, deleted, or renamed scenario."""
        if change_type not in CHANGE_TYPES:
            choices = ", ".join(CHANGE_TYPES)
            raise ValueError(f"change_type must be one of: {choices}")
        def run(store):
            history = (getattr(store, "last_freshness", None) or {}).get("history")
            result = impact_analysis(store, target, change_type=change_type,
                                     history=history)
            return {"status": "ok" if result else "not_found", "impact": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def docs_for_code(self, target: str, limit: int = 50, auto_index: bool = True) -> dict:
        def run(store):
            result = docs_for_code(store, target, limit=limit)
            return {"status": "ok", "documents": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def code_for_docs(self, doc_path: str, heading: str | None = None,
                      limit: int = 50, auto_index: bool = True) -> dict:
        def run(store):
            result = code_for_docs(store, doc_path, heading=heading, limit=limit)
            return {"status": "ok", "code": _safe(result)}
        return self._query(run, auto_index=auto_index)

    def write_agent_memory(self, summary: str, decisions: list[str] | None = None,
                           assumptions: list[str] | None = None,
                           open_questions: list[str] | None = None,
                           changed_files: list[str] | None = None,
                           next_steps: list[str] | None = None) -> dict:
        return write_agent_memory(self.root, summary, decisions=decisions,
                                  assumptions=assumptions, open_questions=open_questions,
                                  changed_files=changed_files, next_steps=next_steps)

    def read_agent_memory(self, topic: str | None = None, limit: int = 10,
                          auto_index: bool = True) -> dict:
        return self._query(
            lambda store: read_agent_memory(
                self.root, topic=topic, limit=limit, store=store,
            ),
            auto_index=auto_index,
        )

    def verify_agent_memory(self, limit: int = 1000,
                            auto_index: bool = True) -> dict:
        return self._query(
            lambda store: verify_agent_memory(self.root, store, limit=limit),
            auto_index=auto_index,
        )


def create_server(root: str | Path):
    """Create a FastMCP server, importing the optional SDK only when needed."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("RepoBrain")
    tools = RepoBrainTools(root)
    for name in (
        "index_repo", "search_project", "explain_project", "project_brief", "change_context", "explain_file", "find_symbol",
        "trace_symbol", "trace_config", "trace_data_flow", "impact_analysis",
        "co_change", "churn_hotspots", "ownership",
        "docs_for_code", "code_for_docs", "write_agent_memory", "read_agent_memory",
        "verify_agent_memory",
    ):
        server.tool(name=name)(getattr(tools, name))
    return server


def run_server(root: str | Path) -> None:
    configure_logging_from_env()
    log_event("mcp.server.start", status="ok", transport="stdio")
    create_server(root).run(transport="stdio")

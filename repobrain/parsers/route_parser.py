"""Deterministic route extraction for common Python and JS web patterns."""
from __future__ import annotations

import re

from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType, node_id
from .base import ParseResult, Parser


_PY_ROUTE = re.compile(
    r'@(?:\w+\.)?(?:route|get|post|put|patch|delete)\(\s*["\'](?P<path>[^"\']+)["\'](?P<args>[^)]*)\)'
    r'\s*\n\s*(?:async\s+)?def\s+(?P<handler>\w+)',
    re.MULTILINE,
)
_JS_ROUTE = re.compile(
    r'\b(?:router|app)\.(?P<method>get|post|put|patch|delete)\(\s*["\'](?P<path>[^"\']+)["\']'
    r'\s*,(?P<body>.*?)(?=\n\s*(?:router|app)\.(?:get|post|put|patch|delete)\(|\Z)',
    re.DOTALL | re.IGNORECASE,
)


class RouteParser(Parser):
    name = "route_parser"

    def begin_run(self, known_paths) -> None:
        self._dirty = False

    def can_parse(self, path: str, language: str | None) -> bool:
        return language in {"python", "javascript", "typescript"}

    def parse(self, path: str, content: str) -> ParseResult:
        self._dirty = True
        result = ParseResult()
        if path.endswith(".py"):
            matches = self._python_matches(content)
        else:
            matches = self._javascript_matches(content)
        for method, route_path, handler, line in matches:
            name = f"{method} {route_path}"
            route = Node(
                type=NodeType.ROUTE, name=name, qualified_name=name, path=path,
                start_line=line, end_line=line, language="http",
                metadata={"method": method, "route": route_path, "handler": handler},
                extractor=self.name,
            )
            result.nodes.append(route)
            result.fts_rows.append(FtsRow(path, name, name, route.id))
            # CodeParser uses the same path + symbol name identity. Orphan cleanup
            # safely drops this edge if a heuristic handler is not a real symbol.
        return result

    def finish_run(self, store) -> list[Edge]:
        """Resolve handlers after CodeParser symbols have been persisted."""
        if not getattr(self, "_dirty", False):
            return []
        store.delete_edges(EdgeType.HANDLES_ROUTE, extractor=self.name)
        edges = []
        routes = store.conn.execute(
            "SELECT * FROM nodes WHERE type='Route' AND extractor=?", (self.name,)
        ).fetchall()
        for route in routes:
            meta = __import__("json").loads(route["metadata_json"] or "{}")
            handler = meta.get("handler")
            if handler == "__module__":
                target = store.conn.execute(
                    "SELECT id FROM nodes WHERE type='Module' AND path=? LIMIT 1", (route["path"],)
                ).fetchone()
            else:
                target = store.conn.execute(
                    "SELECT id FROM nodes WHERE type IN ('Function','Method') AND path=? AND name=? LIMIT 2",
                    (route["path"], handler),
                ).fetchone()
            if target:
                edges.append(Edge(
                    type=EdgeType.HANDLES_ROUTE, source_node_id=route["id"],
                    target_node_id=target["id"], path=route["path"],
                    start_line=route["start_line"], confidence=0.9, extractor=self.name,
                ))
        return edges

    @staticmethod
    def _python_matches(content: str):
        for match in _PY_ROUTE.finditer(content):
            args = match.group("args")
            method_match = re.search(r'methods\s*=\s*\[\s*["\'](\w+)', args)
            method = method_match.group(1).upper() if method_match else "GET"
            yield method, match.group("path"), match.group("handler"), content.count("\n", 0, match.start()) + 1

    @staticmethod
    def _javascript_matches(content: str):
        for match in _JS_ROUTE.finditer(content):
            body = match.group("body")
            callback = re.search(r'(?:async\s*)?\([^)]*\)\s*=>\s*\{', body)
            # Module-level callbacks are represented by the Module node; this
            # preserves observed CALLS edges attributed there by CodeParser.
            handler = path_module_name = ""
            if callback:
                path_module_name = "__module__"
            else:
                named = re.search(r'\b([A-Za-z_$][\w$]*)\s*[,)]', body)
                handler = named.group(1) if named else ""
            line = content.count("\n", 0, match.start()) + 1
            if path_module_name:
                # Signal module resolution to parse() with the qualified module name.
                handler = path_module_name
            yield match.group("method").upper(), match.group("path"), handler, line

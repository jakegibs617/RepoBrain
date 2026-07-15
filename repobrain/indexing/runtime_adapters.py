"""Deterministic reconciliation for framework and ORM syntax facts.

Parsers persist source-local facts in Route and Module metadata.  Adapters
resolve those facts only after every changed file has been stored, then replace
the derived graph facts they own.  They never import or execute target code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from ..graph.schema import Edge, EdgeType, Node, NodeType
from ..graph.store import GraphStore

RUNTIME_ADAPTER_VERSION = "1"


@dataclass
class AdapterResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def extend(self, other: "AdapterResult") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)


class RuntimeAdapter(Protocol):
    """Narrow boundary from persisted syntax facts to graph facts."""

    extractor: str

    def reconcile(self, store: GraphStore) -> AdapterResult: ...


class RuntimeAdapterReconciler:
    """Replace each adapter-owned fact family inside the index transaction."""

    def __init__(self, adapters: tuple[RuntimeAdapter, ...] | None = None) -> None:
        self.adapters = (
            (RouteHandlerAdapter(), SqlAlchemyAdapter())
            if adapters is None else adapters
        )

    def reconcile(self, store: GraphStore) -> AdapterResult:
        result = AdapterResult()
        for adapter in self.adapters:
            store.delete_facts_by_extractor(adapter.extractor)
            result.extend(adapter.reconcile(store))
        return result


def _metadata(row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _module_metadata(store: GraphStore, path: str) -> dict:
    row = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' AND path=? LIMIT 1",
        (path,),
    ).fetchone()
    return _metadata(row) if row is not None else {}


def _import_binding(module_meta: dict, local: str) -> dict | None:
    matches = [
        binding for binding in module_meta.get("import_bindings", [])
        if binding.get("local") == local and binding.get("kind") == "symbol"
    ]
    return matches[0] if len(matches) == 1 else None


def _exact_callable(
    store: GraphStore, path: str, name: str, start_line: int | None = None,
):
    line_clause = " AND start_line=?" if start_line is not None else ""
    args = (path, name, start_line) if start_line is not None else (path, name)
    rows = store.conn.execute(
        "SELECT * FROM nodes WHERE type IN ('Function','Method','TestCase') "
        f"AND path=? AND name=?{line_clause} LIMIT 2", args,
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


class RouteHandlerAdapter:
    """Resolve Flask-style and Express route syntax to precise handlers."""

    extractor = "framework-route-adapter"

    def reconcile(self, store: GraphStore) -> AdapterResult:
        result = AdapterResult()
        routes = store.conn.execute(
            "SELECT * FROM nodes WHERE type='Route' AND extractor='route_parser' "
            "ORDER BY path, start_line, name"
        ).fetchall()
        for route in routes:
            meta = _metadata(route)
            if meta.get("ambiguous"):
                continue
            handler = None
            if meta.get("handler_kind") == "inline":
                handler = self._inline_handler(store, route, meta, result)
            elif meta.get("handler"):
                handler = self._named_handler(store, route, meta)
            if handler is None:
                continue
            result.edges.append(Edge(
                type=EdgeType.HANDLES_ROUTE,
                source_node_id=route["id"], target_node_id=handler["id"],
                path=route["path"], start_line=route["start_line"],
                end_line=route["end_line"], confidence=0.9,
                extractor=self.extractor,
                metadata={
                    "framework": meta.get("framework"),
                    "resolution": meta.get("handler_kind"),
                    "source_grounded": True,
                },
            ))
        return result

    def _named_handler(self, store: GraphStore, route, meta: dict):
        name = meta["handler"]
        local = _exact_callable(
            store, route["path"], name, meta.get("handler_start_line"),
        )
        if local is not None:
            return local
        binding = _import_binding(_module_metadata(store, route["path"]), name)
        if binding is None:
            return None
        return _exact_callable(store, binding["target_path"], binding["symbol"])

    def _inline_handler(
        self, store: GraphStore, route, meta: dict, result: AdapterResult,
    ) -> dict:
        qname = meta["handler_qname"]
        node = Node(
            type=NodeType.FUNCTION,
            name=f"{route['name']} callback",
            qualified_name=qname,
            path=route["path"],
            start_line=meta["callback_start_line"],
            end_line=meta["callback_end_line"],
            language=meta.get("language"),
            confidence=0.9,
            extractor=self.extractor,
            metadata={
                "framework": meta.get("framework"),
                "route": route["name"],
                "synthetic_identity": "literal-route-and-source-span",
            },
        )
        result.nodes.append(node)

        module = store.conn.execute(
            "SELECT id FROM nodes WHERE type='Module' AND path=? LIMIT 1",
            (route["path"],),
        ).fetchone()
        if module is not None:
            calls = store.conn.execute(
                "SELECT * FROM edges WHERE type='CALLS' AND extractor='code_treesitter' "
                "AND source_node_id=? AND path=? AND start_line BETWEEN ? AND ?",
                (module["id"], route["path"], meta["callback_start_line"],
                 meta["callback_end_line"]),
            ).fetchall()
            for call in calls:
                call_meta = _metadata(call)
                call_meta.update({
                    "resolution": "framework-inline-callback",
                    "original_source": module["id"],
                })
                result.edges.append(Edge(
                    type=EdgeType.CALLS,
                    source_node_id=node.id,
                    target_node_id=call["target_node_id"],
                    path=call["path"], start_line=call["start_line"],
                    end_line=call["end_line"], confidence=call["confidence"],
                    extractor=self.extractor,
                    is_inferred=bool(call["is_inferred"]),
                    inference_reason=call["inference_reason"],
                    metadata=call_meta,
                ))
        return {"id": node.id}


class SqlAlchemyAdapter:
    """Resolve exact ``__tablename__`` and model-operation syntax facts."""

    extractor = "sqlalchemy-adapter"

    def reconcile(self, store: GraphStore) -> AdapterResult:
        result = AdapterResult()
        model_tables: dict[str, list[Node]] = {}
        modules = store.conn.execute(
            "SELECT * FROM nodes WHERE type='Module' ORDER BY path"
        ).fetchall()

        for module in modules:
            meta = _metadata(module)
            for fact in meta.get("orm_models", []):
                model = self._class(store, module["path"], fact["class"])
                if model is None:
                    continue
                table = Node(
                    type=NodeType.TABLE,
                    name=fact["table"],
                    qualified_name=f"{model['qualified_name']}.__table__:{fact['table']}",
                    path=module["path"], start_line=fact["line"],
                    end_line=fact["line"], language="sql",
                    confidence=1.0, extractor=self.extractor,
                    metadata={
                        "framework": "sqlalchemy",
                        "model": model["qualified_name"],
                        "table_literal": fact["table"],
                        "evidence": "__tablename__ literal",
                    },
                )
                result.nodes.append(table)
                model_tables.setdefault(model["id"], []).append(table)
                result.edges.append(Edge(
                    type=EdgeType.DEPENDS_ON,
                    source_node_id=model["id"], target_node_id=table.id,
                    path=module["path"], start_line=fact["line"],
                    end_line=fact["line"], confidence=1.0,
                    extractor=self.extractor,
                    metadata={"relation": "maps-table", "source_grounded": True},
                ))

        for module in modules:
            meta = _metadata(module)
            for fact in meta.get("orm_operations", []):
                model = self._resolve_model(store, module["path"], meta,
                                            fact["model_binding"])
                tables = model_tables.get(model["id"], []) if model is not None else []
                source = store.conn.execute(
                    "SELECT id FROM nodes WHERE id=? LIMIT 1", (fact["source_id"],),
                ).fetchone()
                if source is None or len(tables) != 1:
                    continue
                edge_type = (
                    EdgeType.READS_TABLE if fact["operation"] == "read"
                    else EdgeType.WRITES_TABLE
                )
                result.edges.append(Edge(
                    type=edge_type,
                    source_node_id=source["id"], target_node_id=tables[0].id,
                    path=module["path"], start_line=fact["line"],
                    end_line=fact["line"], confidence=0.85,
                    extractor=self.extractor, is_inferred=True,
                    inference_reason="sqlalchemy-convention",
                    metadata={
                        "model": model["qualified_name"],
                        "table": tables[0].name,
                        "pattern": fact["pattern"],
                        "evidence": "exact-model-binding-and-table-literal",
                    },
                ))
        return result

    @staticmethod
    def _class(store: GraphStore, path: str, name: str):
        rows = store.conn.execute(
            "SELECT * FROM nodes WHERE type='Class' AND path=? AND name=? LIMIT 2",
            (path, name),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def _resolve_model(self, store: GraphStore, path: str, meta: dict, binding: str):
        local = self._class(store, path, binding)
        if local is not None:
            return local
        imported = _import_binding(meta, binding)
        if imported is None:
            return None
        return self._class(store, imported["target_path"], imported["symbol"])

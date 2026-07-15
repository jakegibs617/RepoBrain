"""Source-local syntax facts for Flask-style and Express routes."""
from __future__ import annotations

import ast
import posixpath

from ..graph.schema import FtsRow, Node, NodeType
from .base import ParseResult, Parser

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


class RouteParser(Parser):
    """Persist route declarations; cross-file handler resolution is separate."""

    name = "route_parser"

    def can_parse(self, path: str, language: str | None) -> bool:
        return language in {"python", "javascript", "typescript"}

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        try:
            matches = (
                self._python_matches(path, content) if path.endswith(".py")
                else self._javascript_matches(path, content)
            )
        except (SyntaxError, ValueError) as exc:
            result.warnings.append(f"{path}: route syntax extraction failed: {exc}")
            return result
        for fact in matches:
            name = f"{fact['method']} {fact['route']}"
            route = Node(
                type=NodeType.ROUTE, name=name, qualified_name=name, path=path,
                start_line=fact["line"], end_line=fact.get("end_line", fact["line"]),
                language="http", metadata=fact, extractor=self.name,
            )
            result.nodes.append(route)
            result.fts_rows.append(FtsRow(path, name, name, route.id))
        return result

    @staticmethod
    def _literal(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _python_matches(self, path: str, content: str) -> list[dict]:
        tree = ast.parse(content)
        facts = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in fn.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if not isinstance(decorator.func.value, (ast.Name, ast.Attribute)):
                    continue  # dynamic receiver such as get_app().route(...)
                attr = decorator.func.attr.lower()
                if attr not in _HTTP_METHODS | {"route"} or not decorator.args:
                    continue
                route = self._literal(decorator.args[0])
                if route is None:
                    continue
                methods = [attr.upper()] if attr != "route" else ["GET"]
                if attr == "route":
                    for keyword in decorator.keywords:
                        if keyword.arg != "methods":
                            continue
                        if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                            methods = []
                            break
                        literals = [self._literal(item) for item in keyword.value.elts]
                        if literals and all(literals):
                            methods = [method.upper() for method in literals]
                        else:
                            methods = []
                        break
                for method in methods:
                    facts.append({
                        "framework": "flask", "language": "python",
                        "method": method, "route": route, "handler": fn.name,
                        "handler_kind": "decorated-function", "line": decorator.lineno,
                        "handler_start_line": fn.lineno,
                        "end_line": getattr(fn, "end_lineno", fn.lineno),
                    })
        return facts

    @staticmethod
    def _ts_text(node) -> str:
        return node.text.decode("utf-8", "replace")

    def _javascript_matches(self, path: str, content: str) -> list[dict]:
        from tree_sitter import Query, QueryCursor
        from tree_sitter_language_pack import get_language, get_parser

        grammar = (
            "tsx" if path.endswith(".tsx")
            else "typescript" if path.endswith(".ts")
            else "javascript"
        )
        tree = get_parser(grammar).parse(content.encode("utf-8"))
        query = Query(get_language(grammar), "(call_expression) @call")
        calls = QueryCursor(query).captures(tree.root_node).get("call", [])
        facts = []
        module = posixpath.splitext(path)[0]
        for call in sorted(calls, key=lambda node: node.start_byte):
            fn = call.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            receiver = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            if receiver is None or prop is None or receiver.type != "identifier":
                continue
            receiver_name = self._ts_text(receiver)
            method = self._ts_text(prop).lower()
            if receiver_name not in {"app", "router"} or method not in _HTTP_METHODS:
                continue
            args = call.child_by_field_name("arguments")
            values = list(args.named_children) if args is not None else []
            if len(values) < 2 or values[0].type != "string":
                continue
            route = self._ts_text(values[0]).strip("'\"`")
            handlers = values[1:]
            fact = {
                "framework": "express", "language": grammar,
                "method": method.upper(), "route": route,
                "line": call.start_point[0] + 1, "end_line": call.end_point[0] + 1,
            }
            if len(handlers) != 1:
                fact.update({"ambiguous": True, "handler_kind": "multiple-callbacks"})
            elif handlers[0].type == "identifier":
                fact.update({
                    "handler": self._ts_text(handlers[0]),
                    "handler_kind": "named-callback",
                })
            elif handlers[0].type in {"arrow_function", "function_expression", "function"}:
                start = handlers[0].start_point[0] + 1
                end = handlers[0].end_point[0] + 1
                fact.update({
                    "handler_kind": "inline",
                    "handler_qname": f"{module}#route:{method.upper()} {route}@{start}",
                    "callback_start_line": start, "callback_end_line": end,
                })
            else:
                fact.update({"ambiguous": True, "handler_kind": "dynamic-callback"})
            facts.append(fact)
        return facts

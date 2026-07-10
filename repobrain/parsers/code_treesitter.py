"""Tree-sitter code symbol parser (Milestone 3).

Extracts Module/Function/Class/Method/Variable nodes plus DEFINES/CONTAINS/
IMPORTS/CALLS/READS_ENV edges from source files. First-class languages are
Python, JavaScript, TypeScript, PHP, and Bash; Go, Java, and Ruby get the same
generic symbol extraction (defs + basic calls, imports as metadata).

Conventions (see DECISIONS.md D14–D18):
- DEFINES = symbol definition (File→Module, Module→Function/Class/Variable,
  Class→Method); CONTAINS = structural nesting (function inside function).
- Internal imports become Module IMPORTS Module edges resolved against the
  set of scanned repo files; unresolvable/external imports are recorded in
  the module node's ``external_imports`` metadata, never as dangling nodes.
- CALLS precision over recall: same-file / self.method() / import-qualified
  resolutions get confidence 0.9 (observed); cross-file name-only matches are
  added in a post-index pass with is_inferred=1, confidence 0.7,
  inference_reason="name-match", and only when the name is globally unique.
- EnvVar nodes are repo-global: id keyed on ("EnvVar", name, "") so reads of
  the same variable from many files converge on one node; each observation is
  its own READS_ENV edge carrying file/line provenance.

Per-language Query objects are compiled once (lru_cache) and reused across
files. Any per-file failure degrades to a warning; the file still gets its
generic File node from GenericFileParser.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache

from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType, node_id
from .base import Parser, ParseResult

EXTRACTOR_NAME = "code_treesitter"

#: scanner language -> tree-sitter grammar name (tsx handled in grammar_for)
GRAMMAR_BY_LANGUAGE = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "php": "php",
    "bash": "bash",
    "go": "go",
    "java": "java",
    "ruby": "ruby",
}

_JS_QUERY = """
(function_declaration) @def.function
(generator_function_declaration) @def.function
(class_declaration) @def.class
(method_definition) @def.method
(import_statement) @import.esm
(call_expression) @call
(member_expression) @member
(subscript_expression) @subscript
(program (lexical_declaration (variable_declarator) @var.decl))
(program (variable_declaration (variable_declarator) @var.decl))
(program (export_statement (lexical_declaration (variable_declarator) @var.decl)))
(program (export_statement (variable_declaration (variable_declarator) @var.decl)))
"""

_TS_QUERY = _JS_QUERY + """
(interface_declaration) @def.class
(enum_declaration) @def.class
(abstract_class_declaration) @def.class
"""

_QUERY_SOURCES = {
    "python": """
(function_definition) @def.function
(class_definition) @def.class
(import_statement) @import.plain
(import_from_statement) @import.from
(call) @call
(subscript) @subscript
(module (expression_statement (assignment) @var.assign))
(module (assignment) @var.assign)
""",
    "javascript": _JS_QUERY,
    "typescript": _TS_QUERY,
    "tsx": _TS_QUERY,
    "php": """
(function_definition) @def.function
(class_declaration) @def.class
(interface_declaration) @def.class
(trait_declaration) @def.class
(method_declaration) @def.method
(namespace_use_declaration) @import.use
(require_expression) @import.req
(require_once_expression) @import.req
(include_expression) @import.req
(include_once_expression) @import.req
(function_call_expression) @call
(member_call_expression) @call.member
(program (expression_statement (assignment_expression) @var.assign))
(const_declaration) @var.const
""",
    "bash": """
(function_definition) @def.function
(program (variable_assignment) @var.assign)
(command) @call
""",
    "go": """
(function_declaration) @def.function
(method_declaration) @def.method
(type_declaration (type_spec) @def.type)
(import_spec) @import.go
(call_expression) @call
(source_file (const_declaration (const_spec) @var.spec))
(source_file (var_declaration (var_spec) @var.spec))
""",
    "java": """
(class_declaration) @def.class
(interface_declaration) @def.class
(enum_declaration) @def.class
(method_declaration) @def.method
(constructor_declaration) @def.method
(import_declaration) @import.java
(method_invocation) @call
""",
    "ruby": """
(method) @def.function
(singleton_method) @def.function
(class) @def.class
(module) @def.class
(call) @call
(program (assignment) @var.assign)
""",
}

_TEST_JS_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_TEST_DIRS = {"tests", "test", "__tests__", "spec"}

_JS_EXTENSIONS = ["", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]

SAME_FILE_CALL_CONFIDENCE = 0.9
INFERRED_CALL_CONFIDENCE = 0.7


def is_test_file(path: str) -> bool:
    base = posixpath.basename(path)
    stem = posixpath.splitext(base)[0]
    if base.startswith("test_") or stem.endswith("_test"):
        return True
    if _TEST_JS_RE.search(base):
        return True
    return any(seg in _TEST_DIRS for seg in path.split("/")[:-1])


def grammar_for(path: str, language: str) -> str:
    if path.endswith(".tsx"):
        return "tsx"
    return GRAMMAR_BY_LANGUAGE[language]


def python_module_qname(path: str) -> str:
    parts = path[: -len(".py")].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path


def default_module_qname(path: str) -> str:
    root, _ext = posixpath.splitext(path)
    return root or path


@lru_cache(maxsize=None)
def _ts_parser(grammar: str):
    from tree_sitter_language_pack import get_parser

    return get_parser(grammar)


@lru_cache(maxsize=None)
def _ts_query(grammar: str):
    from tree_sitter import Query
    from tree_sitter_language_pack import get_language

    return Query(get_language(grammar), _QUERY_SOURCES[grammar])


def _run_query(query, root) -> dict[str, list]:
    from tree_sitter import QueryCursor

    return QueryCursor(query).captures(root)


@dataclass
class _PendingCall:
    caller_id: str
    caller_qname: str
    callee_name: str
    path: str
    line: int


class CodeParser(Parser):
    """Parser interface entry point; per-file work happens in extractors."""

    name = EXTRACTOR_NAME

    def __init__(self) -> None:
        self._known_files: frozenset[str] = frozenset()
        self._pending_calls: list[_PendingCall] = []

    # -- indexer hooks -------------------------------------------------------

    def begin_run(self, known_files: set[str]) -> None:
        """Called by the indexer with every scanned repo path (for import
        resolution) before any file is parsed."""
        self._known_files = frozenset(known_files)
        self._pending_calls = []

    def finish_run(self, store) -> list[Edge]:
        """Cross-file name-only CALLS resolution (after nodes are upserted).

        A pending call becomes an edge only when exactly one Function/Method
        node in the whole graph carries that name (precision over recall).
        """
        edges: list[Edge] = []
        by_name: dict[str, list[_PendingCall]] = {}
        for pc in self._pending_calls:
            by_name.setdefault(pc.callee_name, []).append(pc)
        for name, pcs in by_name.items():
            rows = store.conn.execute(
                "SELECT id, path FROM nodes WHERE type IN ('Function', 'Method') "
                "AND name = ? LIMIT 3",
                (name,),
            ).fetchall()
            for pc in pcs:
                candidates = [
                    r for r in rows if r["path"] != pc.path and r["id"] != pc.caller_id
                ]
                if len(candidates) != 1:
                    continue
                edges.append(
                    Edge(
                        type=EdgeType.CALLS,
                        source_node_id=pc.caller_id,
                        target_node_id=candidates[0]["id"],
                        path=pc.path,
                        start_line=pc.line,
                        end_line=pc.line,
                        confidence=INFERRED_CALL_CONFIDENCE,
                        extractor=EXTRACTOR_NAME,
                        is_inferred=True,
                        inference_reason="name-match",
                        metadata={"callee": name, "resolution": "name-match"},
                    )
                )
        self._pending_calls = []
        return edges

    # -- Parser interface ----------------------------------------------------

    def can_parse(self, path: str, language: str | None) -> bool:
        return language in GRAMMAR_BY_LANGUAGE

    def parse(self, path: str, content: str) -> ParseResult:
        from ..indexing.scanner import detect_language

        result = ParseResult()
        language = detect_language(path)
        if language not in GRAMMAR_BY_LANGUAGE:
            return result
        grammar = grammar_for(path, language)
        try:
            tree = _ts_parser(grammar).parse(content.encode("utf-8"))
            captures = _run_query(_ts_query(grammar), tree.root_node)
        except Exception as exc:  # grammar load/parse failure: degrade
            result.warnings.append(f"{path}: tree-sitter parse failed: {exc}")
            return result
        if tree.root_node.has_error:
            result.warnings.append(
                f"{path}: syntax errors; extracting what parsed cleanly"
            )
        extractor = _EXTRACTORS[language](self, path, language, content, result)
        try:
            extractor.run(captures)
        except Exception as exc:  # an extractor bug must not sink the run
            result.warnings.append(f"{path}: symbol extraction failed: {exc}")
        return result


# ---------------------------------------------------------------------------
# extractors
# ---------------------------------------------------------------------------


class _Extractor:
    """Language-family extraction pipeline over one file's query captures."""

    CLASS_SCOPES: frozenset[str] = frozenset()
    FUNC_SCOPES: frozenset[str] = frozenset()
    #: anonymous callable containers that break the scope walk but carry no name
    ANON_SCOPES: frozenset[str] = frozenset()

    def __init__(
        self, parser: CodeParser, path: str, language: str, content: str,
        result: ParseResult,
    ) -> None:
        self.parser = parser
        self.path = path
        self.language = language
        self.lines = content.splitlines()
        self.result = result
        self.known = parser._known_files
        self.is_test = is_test_file(path)
        self.module_qname = self._module_qname()

        self.def_pairs: list[tuple[object, Node]] = []  # (ts node, graph node)
        self.ts_defs: dict[int, Node] = {}  # ts node id -> graph node
        self.func_by_name: dict[str, Node] = {}  # module-level callables
        self.classes_by_name: dict[str, Node] = {}
        self.methods: dict[tuple[str, str], Node] = {}  # (class qname, name)
        self.symbol_aliases: dict[str, tuple[str, str, str]] = {}  # local -> (mod qname, mod path, symbol)
        self.module_aliases: dict[str, tuple[str, str]] = {}  # prefix -> (mod qname, mod path)
        self.external_imports: set[str] = set()
        self.env_consumed: set[int] = set()
        self.seen_vars: set[str] = set()
        self.testcases: list[Node] = []
        self.module_node: Node | None = None

    # -- pipeline -------------------------------------------------------------

    def run(self, captures: dict[str, list]) -> None:
        self._emit_module()
        self._register_defs(captures)
        self._register_tests(captures)
        self._link_defs()
        self._extract_variables(captures)
        self._extract_imports(captures)
        self._extract_env(captures)
        self._extract_calls(captures)
        self._emit_testfile()
        self._finish_module_metadata()

    # -- generic helpers ------------------------------------------------------

    def _module_qname(self) -> str:
        return default_module_qname(self.path)

    def _text(self, node) -> str:
        return node.text.decode("utf-8", "replace")

    def _span(self, node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def _signature(self, node) -> str:
        line = node.start_point[0]
        if 0 <= line < len(self.lines):
            return self.lines[line].strip()[:200]
        return ""

    def _def_name(self, node) -> str | None:
        name_node = node.child_by_field_name("name")
        return self._text(name_node) if name_node is not None else None

    def _named_scopes(self, node) -> list[str]:
        """Names of enclosing class/function scopes, outermost first."""
        names: list[str] = []
        p = node.parent
        while p is not None:
            if p.type in self.CLASS_SCOPES or p.type in self.FUNC_SCOPES:
                name = self._def_name(p)
                if name:
                    names.append(name)
            p = p.parent
        names.reverse()
        return names

    def _nearest_scope_kind(self, node) -> str | None:
        p = node.parent
        while p is not None:
            if p.type in self.CLASS_SCOPES:
                return "class"
            if p.type in self.FUNC_SCOPES:
                return "function"
            p = p.parent
        return None

    def _enclosing_callable(self, node) -> Node | None:
        """Nearest enclosing Function/Method/TestCase graph node, if any."""
        p = node.parent
        while p is not None:
            found = self.ts_defs.get(p.id)
            if found is not None and found.type in (
                NodeType.FUNCTION, NodeType.METHOD, NodeType.TEST_CASE,
            ):
                return found
            p = p.parent
        return None

    def _enclosing_class_qname(self, node) -> str | None:
        p = node.parent
        while p is not None:
            if p.type in self.CLASS_SCOPES:
                name = self._def_name(p)
                if not name:
                    return None
                scopes = self._named_scopes(p)
                return ".".join([self.module_qname, *scopes, name])
            p = p.parent
        return None

    def _string_value(self, node) -> str | None:
        """Literal text of a string node (its content children), else None."""
        if node is None:
            return None
        if node.type not in ("string", "interpreted_string_literal",
                             "raw_string_literal", "string_literal"):
            return None
        parts = [
            self._text(c) for c in node.children
            if c.type in ("string_content", "string_fragment",
                          "interpreted_string_literal_content")
        ]
        if parts:
            return "".join(parts)
        text = self._text(node)
        return text.strip("'\"`") or None

    def _first_string_arg(self, call_node, args_field: str = "arguments") -> str | None:
        args = call_node.child_by_field_name(args_field)
        if args is None:
            return None
        for child in args.named_children:
            inner = child
            if child.type == "argument" and child.named_children:  # php wraps args
                inner = child.named_children[0]
            value = self._string_value(inner)
            if value is not None:
                return value
            return None  # first arg is not a literal string: give up
        return None

    # -- module / defs ----------------------------------------------------------

    def _emit_module(self) -> None:
        line_count = len(self.lines)
        self.module_node = Node(
            type=NodeType.MODULE,
            name=self.module_qname.replace("/", ".").rsplit(".", 1)[-1],
            qualified_name=self.module_qname,
            path=self.path,
            start_line=1 if line_count else None,
            end_line=line_count or None,
            language=self.language,
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(self.module_node)
        file_node_id = node_id(NodeType.FILE, self.path, self.path)
        self.result.edges.append(
            Edge(
                type=EdgeType.DEFINES,
                source_node_id=file_node_id,
                target_node_id=self.module_node.id,
                path=self.path,
                start_line=1 if line_count else None,
                end_line=line_count or None,
                extractor=EXTRACTOR_NAME,
            )
        )
        self.result.fts_rows.append(
            FtsRow(
                path=self.path,
                name=self.module_node.name,
                content=self.module_qname,
                node_id=self.module_node.id,
            )
        )

    #: capture name -> def kind
    _DEF_CAPTURES = {
        "def.function": "function",
        "def.method": "method",
        "def.class": "class",
        "def.type": "class",
    }

    def _register_defs(self, captures: dict[str, list]) -> None:
        for capture, kind in self._DEF_CAPTURES.items():
            for ts in sorted(captures.get(capture, []), key=lambda n: n.start_byte):
                self._register_one_def(ts, kind)

    def _register_one_def(self, ts, kind: str) -> None:
        name = self._def_name(ts)
        if not name:
            return
        scopes = self._named_scopes(ts)
        qname = ".".join([self.module_qname, *scopes, name])
        if kind == "class":
            ntype = NodeType.CLASS
        elif kind == "method":
            ntype = NodeType.METHOD
        else:
            ntype = (
                NodeType.METHOD
                if self._nearest_scope_kind(ts) == "class"
                else NodeType.FUNCTION
            )
        if (
            self.is_test
            and ntype in (NodeType.FUNCTION, NodeType.METHOD)
            and name.lower().startswith("test")
        ):
            ntype = NodeType.TEST_CASE
        start, end = self._span(ts)
        node = Node(
            type=ntype,
            name=name,
            qualified_name=qname,
            path=self.path,
            start_line=start,
            end_line=end,
            language=self.language,
            metadata={"signature": self._signature(ts)},
            extractor=EXTRACTOR_NAME,
        )
        if kind == "class":
            node.metadata["kind"] = self._class_kind(ts)
        self.result.nodes.append(node)
        self.def_pairs.append((ts, node))
        self.ts_defs[ts.id] = node
        if ntype == NodeType.CLASS:
            self.classes_by_name.setdefault(name, node)
        elif not scopes:
            self.func_by_name.setdefault(name, node)
        if ntype in (NodeType.METHOD, NodeType.TEST_CASE) and scopes:
            self.methods[(qname.rsplit(".", 1)[0], name)] = node
        if ntype == NodeType.TEST_CASE:
            self.testcases.append(node)
        self.result.fts_rows.append(
            FtsRow(
                path=self.path,
                name=name,
                content=f"{qname}\n{node.metadata['signature']}",
                node_id=node.id,
            )
        )

    def _class_kind(self, ts) -> str:
        return {
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "trait_declaration": "trait",
            "type_spec": "type",
            "module": "module",
        }.get(ts.type, "class")

    def _link_defs(self) -> None:
        for ts, node in self.def_pairs:
            container: Node | None = None
            p = ts.parent
            while p is not None:
                container = self.ts_defs.get(p.id)
                if container is not None:
                    break
                p = p.parent
            if container is None:
                source, edge_type = self.module_node, EdgeType.DEFINES
            elif container.type == NodeType.CLASS:
                source, edge_type = container, EdgeType.DEFINES
            else:  # nested inside another callable: structural nesting
                source, edge_type = container, EdgeType.CONTAINS
            self.result.edges.append(
                Edge(
                    type=edge_type,
                    source_node_id=source.id,
                    target_node_id=node.id,
                    path=self.path,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    extractor=EXTRACTOR_NAME,
                )
            )

    def _add_variable(self, name: str, ts) -> Node | None:
        if not name or name in self.seen_vars:
            return None
        self.seen_vars.add(name)
        start, end = self._span(ts)
        node = Node(
            type=NodeType.VARIABLE,
            name=name,
            qualified_name=f"{self.module_qname}.{name}",
            path=self.path,
            start_line=start,
            end_line=end,
            language=self.language,
            metadata={"signature": self._signature(ts)},
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(node)
        self.result.edges.append(
            Edge(
                type=EdgeType.DEFINES,
                source_node_id=self.module_node.id,
                target_node_id=node.id,
                path=self.path,
                start_line=start,
                end_line=end,
                extractor=EXTRACTOR_NAME,
            )
        )
        self.result.fts_rows.append(
            FtsRow(
                path=self.path, name=name,
                content=f"{node.qualified_name}\n{node.metadata['signature']}",
                node_id=node.id,
            )
        )
        return node

    # -- imports ---------------------------------------------------------------

    def _add_import_edge(self, target_qname: str, target_path: str, line: int) -> None:
        self.result.edges.append(
            Edge(
                type=EdgeType.IMPORTS,
                source_node_id=self.module_node.id,
                target_node_id=node_id(NodeType.MODULE, target_qname, target_path),
                path=self.path,
                start_line=line,
                end_line=line,
                metadata={"module": target_qname, "target_path": target_path},
                extractor=EXTRACTOR_NAME,
            )
        )

    # -- calls -------------------------------------------------------------------

    def _call_source(self, ts) -> Node:
        """Enclosing callable, else the module node (module-level call)."""
        return self._enclosing_callable(ts) or self.module_node

    def _add_call_edge(
        self, source: Node, target_id: str, line: int, resolution: str,
        callee: str, confidence: float = SAME_FILE_CALL_CONFIDENCE,
    ) -> None:
        self.result.edges.append(
            Edge(
                type=EdgeType.CALLS,
                source_node_id=source.id,
                target_node_id=target_id,
                path=self.path,
                start_line=line,
                end_line=line,
                confidence=confidence,
                extractor=EXTRACTOR_NAME,
                metadata={"callee": callee, "resolution": resolution},
            )
        )

    def _resolve_plain_call(self, name: str, ts) -> None:
        """Shared bare-name call resolution ladder."""
        source = self._call_source(ts)
        line = ts.start_point[0] + 1
        if name in self.func_by_name:
            target = self.func_by_name[name]
            self._add_call_edge(source, target.id, line, "same-file", name)
            return
        if name in self.classes_by_name:
            return  # instantiation, not a function call (out of M3 scope)
        if name in self.symbol_aliases:
            mod_qname, mod_path, symbol = self.symbol_aliases[name]
            target_id = node_id(NodeType.FUNCTION, f"{mod_qname}.{symbol}", mod_path)
            self._add_call_edge(source, target_id, line, "import", name)
            return
        self.parser._pending_calls.append(
            _PendingCall(
                caller_id=source.id,
                caller_qname=source.qualified_name,
                callee_name=name,
                path=self.path,
                line=line,
            )
        )

    def _resolve_self_call(self, name: str, ts) -> None:
        """self.method() / this.method(): resolve within the enclosing class."""
        class_qname = self._enclosing_class_qname(ts)
        if class_qname is None:
            return
        target = self.methods.get((class_qname, name))
        if target is None:
            return  # unknown in this class (maybe inherited): skip, precision
        source = self._call_source(ts)
        self._add_call_edge(
            source, target.id, ts.start_point[0] + 1, "same-class", name
        )

    def _resolve_module_attr_call(self, prefix: str, name: str, ts) -> None:
        """alias.func() where alias is an imported internal module."""
        if prefix not in self.module_aliases:
            return
        mod_qname, mod_path = self.module_aliases[prefix]
        source = self._call_source(ts)
        target_id = node_id(NodeType.FUNCTION, f"{mod_qname}.{name}", mod_path)
        self._add_call_edge(source, target_id, ts.start_point[0] + 1, "import", name)

    # -- env -----------------------------------------------------------------

    def _add_env_read(self, var: str, ts) -> None:
        line = ts.start_point[0] + 1
        env_node = Node(
            type=NodeType.ENV_VAR,
            name=var,
            qualified_name=var,
            path="",  # repo-global identity (D17)
            language=None,
            metadata={"observation": {"path": self.path, "line": line}},
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(env_node)
        source = self._call_source(ts)
        self.result.edges.append(
            Edge(
                type=EdgeType.READS_ENV,
                source_node_id=source.id,
                target_node_id=env_node.id,
                path=self.path,
                start_line=line,
                end_line=line,
                extractor=EXTRACTOR_NAME,
                metadata={"var": var},
            )
        )

    # -- test files --------------------------------------------------------------

    def _emit_testfile(self) -> None:
        if not self.is_test:
            return
        line_count = len(self.lines)
        tf = Node(
            type=NodeType.TEST_FILE,
            name=posixpath.basename(self.path),
            qualified_name=self.path,
            path=self.path,
            start_line=1 if line_count else None,
            end_line=line_count or None,
            language=self.language,
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(tf)
        for tc in self.testcases:
            self.result.edges.append(
                Edge(
                    type=EdgeType.CONTAINS,
                    source_node_id=tf.id,
                    target_node_id=tc.id,
                    path=self.path,
                    start_line=tc.start_line,
                    end_line=tc.end_line,
                    extractor=EXTRACTOR_NAME,
                )
            )

    def _finish_module_metadata(self) -> None:
        self.module_node.metadata.update(
            {
                "external_imports": sorted(self.external_imports),
                "is_test_file": self.is_test,
            }
        )

    # -- per-language hooks (defaults: no-ops) ---------------------------------

    def _register_tests(self, captures: dict[str, list]) -> None:
        pass

    def _extract_variables(self, captures: dict[str, list]) -> None:
        pass

    def _extract_imports(self, captures: dict[str, list]) -> None:
        pass

    def _extract_env(self, captures: dict[str, list]) -> None:
        pass

    def _extract_calls(self, captures: dict[str, list]) -> None:
        pass


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


class _PythonExtractor(_Extractor):
    CLASS_SCOPES = frozenset({"class_definition"})
    FUNC_SCOPES = frozenset({"function_definition"})
    ANON_SCOPES = frozenset({"lambda"})

    def _module_qname(self) -> str:
        return python_module_qname(self.path)

    def _package_parts(self) -> list[str]:
        parts = self.module_qname.split(".")
        if not self.path.endswith("/__init__.py") and self.path != "__init__.py":
            parts = parts[:-1]
        return parts

    def _resolve_module(self, dotted: str) -> tuple[str, str] | None:
        base = dotted.replace(".", "/")
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in self.known:
                return dotted, candidate
        return None

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for assign in captures.get("var.assign", []):
            left = assign.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                self._add_variable(self._text(left), assign)

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for stmt in captures.get("import.plain", []):
            line = stmt.start_point[0] + 1
            for child in stmt.named_children:
                if child.type == "dotted_name":
                    self._import_module(self._text(child), self._text(child), line)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        alias = self._text(alias_node) if alias_node is not None else None
                        self._import_module(self._text(name_node),
                                            alias or self._text(name_node), line)
        for stmt in captures.get("import.from", []):
            self._import_from(stmt)

    def _import_module(self, dotted: str, bind_as: str, line: int) -> None:
        resolved = self._resolve_module(dotted)
        if resolved is None:
            self.external_imports.add(dotted)
            return
        qname, path = resolved
        self._add_import_edge(qname, path, line)
        # `import a.b` binds prefix "a.b" for attribute calls; `import a.b as z`
        # binds "z".
        self.module_aliases[bind_as] = (qname, path)

    def _relative_base(self, rel_node) -> str | None:
        dots = 0
        suffix = ""
        for child in rel_node.children:
            if child.type == "import_prefix":
                dots = len(self._text(child))
            elif child.type == "dotted_name":
                suffix = self._text(child)
        pkg = self._package_parts()
        cut = len(pkg) - (dots - 1)
        if dots == 0 or cut < 0:
            return None
        base_parts = pkg[:cut] if dots > 1 else pkg
        dotted = ".".join(base_parts)
        if suffix:
            dotted = f"{dotted}.{suffix}" if dotted else suffix
        return dotted or None

    def _import_from(self, stmt) -> None:
        line = stmt.start_point[0] + 1
        module_node_ts = stmt.child_by_field_name("module_name")
        if module_node_ts is None:
            return
        if module_node_ts.type == "relative_import":
            module_text = self._relative_base(module_node_ts)
            display = self._text(module_node_ts)
        else:
            module_text = self._text(module_node_ts)
            display = module_text
        if module_text is None:
            self.external_imports.add(display)
            return
        names: list[tuple[str, str]] = []  # (imported name, local binding)
        for child in stmt.children_by_field_name("name"):
            if child.type == "dotted_name":
                names.append((self._text(child), self._text(child)))
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                if name_node is not None:
                    local = self._text(alias_node) if alias_node is not None else self._text(name_node)
                    names.append((self._text(name_node), local))
        base_resolved = self._resolve_module(module_text)
        imported_any = False
        for imported, local in names:
            sub = self._resolve_module(f"{module_text}.{imported}")
            if sub is not None:
                qname, path = sub
                self._add_import_edge(qname, path, line)
                self.module_aliases[local] = (qname, path)
                imported_any = True
            elif base_resolved is not None:
                qname, path = base_resolved
                self.symbol_aliases[local] = (qname, path, imported)
        if base_resolved is not None and not imported_any:
            qname, path = base_resolved
            self._add_import_edge(qname, path, line)
        elif base_resolved is None:
            self.external_imports.add(display)

    _ENV_CALLS = {"os.getenv", "getenv"}
    _ENV_GET_CALLS = {"os.environ.get", "environ.get"}
    _ENV_SUBSCRIPTS = {"os.environ", "environ"}

    def _extract_env(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            full = self._text(fn)
            if full in self._ENV_CALLS or full in self._ENV_GET_CALLS:
                self.env_consumed.add(call.id)
                var = self._first_string_arg(call)
                if var:
                    self._add_env_read(var, call)
        for sub in captures.get("subscript", []):
            obj = sub.child_by_field_name("value")
            idx = sub.child_by_field_name("subscript")
            if obj is not None and self._text(obj) in self._ENV_SUBSCRIPTS:
                var = self._string_value(idx) if idx is not None else None
                if var:
                    self._add_env_read(var, sub)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            if call.id in self.env_consumed:
                continue
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                self._resolve_plain_call(self._text(fn), call)
            elif fn.type == "attribute":
                full = self._text(fn)
                if "." not in full:
                    continue
                prefix, name = full.rsplit(".", 1)
                if prefix == "self":
                    self._resolve_self_call(name, call)
                else:
                    self._resolve_module_attr_call(prefix, name, call)


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------

_JS_FUNC_VALUES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)


class _JsExtractor(_Extractor):
    CLASS_SCOPES = frozenset({"class_declaration", "abstract_class_declaration"})
    FUNC_SCOPES = frozenset(
        {"function_declaration", "generator_function_declaration", "method_definition"}
    )
    ANON_SCOPES = frozenset(_JS_FUNC_VALUES)

    def _resolve_import(self, spec: str) -> tuple[str, str] | None:
        if not spec.startswith("."):
            return None
        cur_dir = posixpath.dirname(self.path)
        base = posixpath.normpath(posixpath.join(cur_dir, spec))
        if base.startswith(".."):
            return None
        for ext in _JS_EXTENSIONS:
            if base + ext in self.known:
                path = base + ext
                return default_module_qname(path), path
        for ext in _JS_EXTENSIONS[1:]:
            candidate = f"{base}/index{ext}"
            if candidate in self.known:
                return default_module_qname(candidate), candidate
        return None

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for decl in captures.get("var.decl", []):
            name_node = decl.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue  # destructuring patterns handled by require imports
            value = decl.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNC_VALUES:
                # `const f = () => ...` at module level is a Function
                self._register_declared_function(self._text(name_node), decl, value)
                continue
            if (
                value is not None
                and value.type == "call_expression"
                and self._callee_text(value) == "require"
            ):
                continue  # import, not a variable (handled in _extract_imports)
            self._add_variable(self._text(name_node), decl)

    def _register_declared_function(self, name: str, decl, value) -> None:
        qname = f"{self.module_qname}.{name}"
        start, end = self._span(decl)
        ntype = NodeType.FUNCTION
        if self.is_test and name.lower().startswith("test"):
            ntype = NodeType.TEST_CASE
        node = Node(
            type=ntype,
            name=name,
            qualified_name=qname,
            path=self.path,
            start_line=start,
            end_line=end,
            language=self.language,
            metadata={"signature": self._signature(decl)},
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(node)
        self.ts_defs[value.id] = node
        self.func_by_name.setdefault(name, node)
        if ntype == NodeType.TEST_CASE:
            self.testcases.append(node)
        self.result.edges.append(
            Edge(
                type=EdgeType.DEFINES,
                source_node_id=self.module_node.id,
                target_node_id=node.id,
                path=self.path,
                start_line=start,
                end_line=end,
                extractor=EXTRACTOR_NAME,
            )
        )
        self.result.fts_rows.append(
            FtsRow(path=self.path, name=name,
                   content=f"{qname}\n{node.metadata['signature']}", node_id=node.id)
        )

    def _callee_text(self, call) -> str | None:
        fn = call.child_by_field_name("function")
        return self._text(fn) if fn is not None else None

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for stmt in captures.get("import.esm", []):
            self._esm_import(stmt)
        for call in captures.get("call", []):
            if self._callee_text(call) == "require":
                self._require_import(call)

    def _esm_import(self, stmt) -> None:
        line = stmt.start_point[0] + 1
        source = self._string_value(stmt.child_by_field_name("source"))
        if source is None:
            return
        resolved = self._resolve_import(source)
        if resolved is None:
            self.external_imports.add(source)
            return
        qname, path = resolved
        self._add_import_edge(qname, path, line)
        for clause in stmt.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":  # default import
                    self.module_aliases[self._text(part)] = (qname, path)
                elif part.type == "namespace_import":
                    for ident in part.named_children:
                        if ident.type == "identifier":
                            self.module_aliases[self._text(ident)] = (qname, path)
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier":
                            continue
                        name_node = spec.child_by_field_name("name")
                        alias_node = spec.child_by_field_name("alias")
                        if name_node is None:
                            continue
                        local = (
                            self._text(alias_node)
                            if alias_node is not None
                            else self._text(name_node)
                        )
                        self.symbol_aliases[local] = (qname, path, self._text(name_node))

    def _require_import(self, call) -> None:
        line = call.start_point[0] + 1
        source = self._first_string_arg(call)
        if source is None:
            return
        resolved = self._resolve_import(source)
        if resolved is None:
            self.external_imports.add(source)
            return
        qname, path = resolved
        self._add_import_edge(qname, path, line)
        parent = call.parent
        if parent is not None and parent.type == "variable_declarator":
            name_node = parent.child_by_field_name("name")
            if name_node is None:
                return
            if name_node.type == "identifier":
                self.module_aliases[self._text(name_node)] = (qname, path)
            elif name_node.type == "object_pattern":
                for prop in name_node.named_children:
                    if prop.type == "shorthand_property_identifier_pattern":
                        local = self._text(prop)
                        self.symbol_aliases[local] = (qname, path, local)
                    elif prop.type == "pair_pattern":
                        key = prop.child_by_field_name("key")
                        value = prop.child_by_field_name("value")
                        if key is not None and value is not None and value.type == "identifier":
                            self.symbol_aliases[self._text(value)] = (
                                qname, path, self._text(key),
                            )

    def _extract_env(self, captures: dict[str, list]) -> None:
        for member in captures.get("member", []):
            obj = member.child_by_field_name("object")
            prop = member.child_by_field_name("property")
            if obj is None or prop is None:
                continue
            if self._text(obj) == "process.env":
                self._add_env_read(self._text(prop), member)
        for sub in captures.get("subscript", []):
            obj = sub.child_by_field_name("object")
            idx = sub.child_by_field_name("index")
            if obj is not None and self._text(obj) == "process.env":
                var = self._string_value(idx) if idx is not None else None
                if var:
                    self._add_env_read(var, sub)

    _TEST_REGISTRARS = frozenset({"it", "test"})

    def _register_tests(self, captures: dict[str, list]) -> None:
        if not self.is_test:
            return
        seen: dict[str, int] = {}
        for call in sorted(captures.get("call", []), key=lambda n: n.start_byte):
            callee = self._callee_text(call)
            if callee not in self._TEST_REGISTRARS:
                continue
            title = self._first_string_arg(call)
            if not title:
                continue
            qname = f"{self.module_qname}#test:{title}"
            seen[qname] = seen.get(qname, 0) + 1
            if seen[qname] > 1:
                qname = f"{qname}@{seen[qname]}"
            start, end = self._span(call)
            node = Node(
                type=NodeType.TEST_CASE,
                name=title,
                qualified_name=qname,
                path=self.path,
                start_line=start,
                end_line=end,
                language=self.language,
                metadata={"registrar": callee},
                extractor=EXTRACTOR_NAME,
            )
            self.result.nodes.append(node)
            self.testcases.append(node)
            self.result.edges.append(
                Edge(
                    type=EdgeType.DEFINES,
                    source_node_id=self.module_node.id,
                    target_node_id=node.id,
                    path=self.path,
                    start_line=start,
                    end_line=end,
                    extractor=EXTRACTOR_NAME,
                )
            )
            self.result.fts_rows.append(
                FtsRow(path=self.path, name=title, content=qname, node_id=node.id)
            )
            # attribute calls inside the test callback to the TestCase
            args = call.child_by_field_name("arguments")
            if args is not None:
                for arg in args.named_children:
                    if arg.type in _JS_FUNC_VALUES:
                        self.ts_defs[arg.id] = node

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                name = self._text(fn)
                if name == "require" or name in self._TEST_REGISTRARS or name == "describe":
                    continue
                self._resolve_plain_call(name, call)
            elif fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = fn.child_by_field_name("property")
                if obj is None or prop is None:
                    continue
                name = self._text(prop)
                if obj.type == "this":
                    self._resolve_self_call(name, call)
                elif obj.type == "identifier":
                    self._resolve_module_attr_call(self._text(obj), name, call)


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------


class _PhpExtractor(_Extractor):
    CLASS_SCOPES = frozenset(
        {"class_declaration", "interface_declaration", "trait_declaration"}
    )
    FUNC_SCOPES = frozenset({"function_definition", "method_declaration"})
    ANON_SCOPES = frozenset({"anonymous_function_creation_expression", "arrow_function"})

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for assign in captures.get("var.assign", []):
            left = assign.child_by_field_name("left")
            if left is not None and left.type == "variable_name":
                self._add_variable(self._text(left).lstrip("$"), assign)
        for const in captures.get("var.const", []):
            for element in const.named_children:
                if element.type != "const_element":
                    continue
                for child in element.named_children:
                    if child.type == "name":
                        self._add_variable(self._text(child), element)
                        break

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for use in captures.get("import.use", []):
            self.external_imports.add(
                self._text(use).removeprefix("use").strip(" ;")
            )
        for req in captures.get("import.req", []):
            line = req.start_point[0] + 1
            target = None
            for child in req.named_children:
                target = self._string_value(child)
                if target:
                    break
            if not target:
                self.external_imports.add(self._text(req)[:120])
                continue
            base = posixpath.normpath(
                posixpath.join(posixpath.dirname(self.path), target)
            )
            if base in self.known:
                self._add_import_edge(default_module_qname(base), base, line)
            else:
                self.external_imports.add(target)

    def _extract_env(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            fn = call.child_by_field_name("function")
            if fn is not None and self._text(fn) == "getenv":
                self.env_consumed.add(call.id)
                var = self._first_string_arg(call)
                if var:
                    self._add_env_read(var, call)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            if call.id in self.env_consumed:
                continue
            fn = call.child_by_field_name("function")
            if fn is not None and fn.type == "name":
                self._resolve_plain_call(self._text(fn), call)
        for call in captures.get("call.member", []):
            obj = call.child_by_field_name("object")
            name_node = call.child_by_field_name("name")
            if obj is None or name_node is None:
                continue
            if self._text(obj) == "$this":
                self._resolve_self_call(self._text(name_node), call)


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


class _BashExtractor(_Extractor):
    FUNC_SCOPES = frozenset({"function_definition"})

    def _register_one_def(self, ts, kind: str) -> None:
        super()._register_one_def(ts, kind)
        # bash functions are globally callable regardless of nesting
        node = self.ts_defs.get(ts.id)
        if node is not None:
            self.func_by_name.setdefault(node.name, node)

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for assign in captures.get("var.assign", []):
            name_node = assign.child_by_field_name("name")
            if name_node is not None:
                self._add_variable(self._text(name_node), assign)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for cmd in captures.get("call", []):
            name_node = cmd.child_by_field_name("name")
            if name_node is None:
                continue
            name = self._text(name_node)
            target = self.func_by_name.get(name)
            if target is None:
                continue  # external command: skip (precision over recall)
            source = self._call_source(cmd)
            self._add_call_edge(
                source, target.id, cmd.start_point[0] + 1, "same-file", name
            )


# ---------------------------------------------------------------------------
# Go / Java / Ruby (generic wiring)
# ---------------------------------------------------------------------------


class _GoExtractor(_Extractor):
    CLASS_SCOPES = frozenset()  # go methods carry their receiver, not nesting
    FUNC_SCOPES = frozenset({"function_declaration", "method_declaration"})
    ANON_SCOPES = frozenset({"func_literal"})

    def _register_one_def(self, ts, kind: str) -> None:
        if ts.type == "method_declaration":
            self._register_go_method(ts)
        else:
            super()._register_one_def(ts, kind)

    def _register_go_method(self, ts) -> None:
        name = self._def_name(ts)
        if not name:
            return
        receiver = ts.child_by_field_name("receiver")
        recv_type = ""
        if receiver is not None:
            for param in receiver.named_children:
                type_node = param.child_by_field_name("type")
                if type_node is not None:
                    recv_type = self._text(type_node).lstrip("*")
                    break
        qname = ".".join(filter(None, [self.module_qname, recv_type, name]))
        start, end = self._span(ts)
        node = Node(
            type=NodeType.METHOD,
            name=name,
            qualified_name=qname,
            path=self.path,
            start_line=start,
            end_line=end,
            language=self.language,
            metadata={"signature": self._signature(ts), "receiver": recv_type},
            extractor=EXTRACTOR_NAME,
        )
        self.result.nodes.append(node)
        self.ts_defs[ts.id] = node
        if recv_type:
            self.methods[(f"{self.module_qname}.{recv_type}", name)] = node
        # not in def_pairs, so link directly: Class DEFINES if the receiver
        # type is declared in this file, else Module DEFINES
        container = self.classes_by_name.get(recv_type)
        source = container if container is not None else self.module_node
        self.result.edges.append(
            Edge(
                type=EdgeType.DEFINES,
                source_node_id=source.id,
                target_node_id=node.id,
                path=self.path,
                start_line=start,
                end_line=end,
                extractor=EXTRACTOR_NAME,
            )
        )
        self.result.fts_rows.append(
            FtsRow(path=self.path, name=name,
                   content=f"{qname}\n{node.metadata['signature']}", node_id=node.id)
        )

    def _register_defs(self, captures: dict[str, list]) -> None:
        # classes (type specs) first so method receivers can link to them
        for ts in sorted(captures.get("def.type", []), key=lambda n: n.start_byte):
            self._register_one_def(ts, "class")
        for ts in sorted(captures.get("def.function", []), key=lambda n: n.start_byte):
            self._register_one_def(ts, "function")
        for ts in sorted(captures.get("def.method", []), key=lambda n: n.start_byte):
            self._register_one_def(ts, "method")

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for spec in captures.get("var.spec", []):
            for name_node in spec.children_by_field_name("name"):
                self._add_variable(self._text(name_node), spec)

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for spec in captures.get("import.go", []):
            path_node = spec.child_by_field_name("path")
            value = self._string_value(path_node) if path_node is not None else None
            if value:
                self.external_imports.add(value)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            fn = call.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                self._resolve_plain_call(self._text(fn), call)


class _JavaExtractor(_Extractor):
    CLASS_SCOPES = frozenset(
        {"class_declaration", "interface_declaration", "enum_declaration"}
    )
    FUNC_SCOPES = frozenset({"method_declaration", "constructor_declaration"})
    ANON_SCOPES = frozenset({"lambda_expression"})

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for imp in captures.get("import.java", []):
            text = self._text(imp).removeprefix("import").strip(" ;")
            if text:
                self.external_imports.add(text)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            name_node = call.child_by_field_name("name")
            obj = call.child_by_field_name("object")
            if name_node is None:
                continue
            if obj is None or obj.type == "this":
                # bare / this-qualified invocation: resolve within the class
                self._resolve_self_call(self._text(name_node), call)


class _RubyExtractor(_Extractor):
    CLASS_SCOPES = frozenset({"class", "module"})
    FUNC_SCOPES = frozenset({"method", "singleton_method"})
    ANON_SCOPES = frozenset({"block", "do_block", "lambda"})

    def _extract_variables(self, captures: dict[str, list]) -> None:
        for assign in captures.get("var.assign", []):
            left = assign.child_by_field_name("left")
            if left is not None and left.type == "constant":
                self._add_variable(self._text(left), assign)

    def _extract_imports(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            method = call.child_by_field_name("method")
            if method is None:
                continue
            name = self._text(method)
            if name not in ("require", "require_relative"):
                continue
            self.env_consumed.add(call.id)  # keep out of the calls pass
            target = self._first_string_arg(call)
            if not target:
                continue
            if name == "require_relative":
                base = posixpath.normpath(
                    posixpath.join(posixpath.dirname(self.path), target)
                )
                candidate = base if base.endswith(".rb") else f"{base}.rb"
                if candidate in self.known:
                    self._add_import_edge(
                        default_module_qname(candidate), candidate,
                        call.start_point[0] + 1,
                    )
                    continue
            self.external_imports.add(target)

    def _extract_calls(self, captures: dict[str, list]) -> None:
        for call in captures.get("call", []):
            if call.id in self.env_consumed:
                continue
            if call.child_by_field_name("receiver") is not None:
                continue  # dynamic receiver: skip
            method = call.child_by_field_name("method")
            if method is None or method.type != "identifier":
                continue
            name = self._text(method)
            class_qname = self._enclosing_class_qname(call)
            if class_qname is not None and (class_qname, name) in self.methods:
                self._resolve_self_call(name, call)
            else:
                self._resolve_plain_call(name, call)


_EXTRACTORS: dict[str, type[_Extractor]] = {
    "python": _PythonExtractor,
    "javascript": _JsExtractor,
    "typescript": _JsExtractor,
    "php": _PhpExtractor,
    "bash": _BashExtractor,
    "go": _GoExtractor,
    "java": _JavaExtractor,
    "ruby": _RubyExtractor,
}

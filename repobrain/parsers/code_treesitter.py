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
  Go resolves against the module path declared in ``go.mod`` at the indexed
  root (a bounded, one-time text read done once in ``begin_run``, not a Go
  toolchain invocation); Java resolves fully-qualified imports against a
  conventional ``src/main/java``/``src/test/java`` source root detected from
  the scanned file set. See D15/D19 in DECISIONS.md for the precise rules
  and their documented limits.
- CALLS precision over recall: same-file / self.method() / import-qualified
  resolutions get confidence 0.9 (observed); cross-file name-only matches are
  added in a post-index pass with is_inferred=1, confidence 0.7,
  inference_reason="name-match", and only when the name is globally unique.
- INSTANTIATES (D32) mirrors CALLS' confidence ladder exactly for a bare
  call-shaped node whose callee resolves to a Class instead of a
  Function/Method: same tiers, same is_inferred/confidence/inference_reason
  values, same finish_run batching. CALLS always wins a same-name tie
  deterministically (checked first at every tier), so a name never produces
  both edge types. Import-qualified resolution (`symbol_aliases`/
  `module_aliases`) is a deliberate refinement over D16's old "compute the
  id, let the orphan sweep clean up a wrong guess" approach: with a second
  candidate type in play, a target that's genuinely both a Function and a
  Class of the same name in the imported module would leave *both*
  speculative edges existing, violating "never double-emit". Those calls are
  queued (`_PendingImportCall`) and resolved by one batched existence check
  in `finish_run` instead, so CALLS/INSTANTIATES for the import-qualified
  case are chosen deterministically rather than blindly emitted. Go is
  excluded (`_GoExtractor.SUPPORTS_INSTANTIATES = False`): its
  type-conversion syntax `T(x)` parses as the same `call_expression` shape
  as a real call, which would otherwise be misread as a constructor call.
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
from pathlib import Path

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


_GO_MODULE_RE = re.compile(r"^module\s+(\S+)")
_GO_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _read_go_module_prefix(root: str | Path | None) -> str | None:
    """Read the ``module`` directive from ``go.mod`` at the indexed root.

    A single bounded text read done once per index run (analogous to the
    indexer's own ``git rev-parse`` call), not a Go toolchain invocation and
    not a filesystem probe during per-file parsing. Only ``go.mod`` located
    exactly at the indexed root is honored (D19): a module whose ``go.mod``
    lives in an ancestor directory outside the indexed root is not resolved,
    matching this codebase's precision-over-recall stance rather than
    guessing at a module boundary. Block comments (``/* ... */``) are
    stripped before scanning so a commented-out ``module`` line can never be
    mistaken for the real directive.
    """
    if root is None:
        return None
    go_mod = Path(root) / "go.mod"
    try:
        text = go_mod.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = _GO_BLOCK_COMMENT_RE.sub("", text)
    for line in text.splitlines():
        match = _GO_MODULE_RE.match(line.strip())
        if match:
            prefix = match.group(1).split("//", 1)[0].strip()
            return prefix or None
    return None


_JAVA_SOURCE_SEGMENTS = {"main": ("src", "main", "java"), "test": ("src", "test", "java")}


def _detect_java_source_roots(known_files: frozenset[str]) -> dict[str, str | None]:
    """Detect the conventional Maven/Gradle ``src/main|test/java`` root(s).

    Purely path-based over the scanned file set (no filesystem/content
    access), mirroring how Python/JS resolve against ``known_files`` alone.
    Matching is done on whole path segments (``"src/main/java".split("/")``
    as a contiguous subsequence of ``path.split("/")``), not a raw substring
    search — a directory that merely *ends* in ``...src`` immediately
    followed by ``main/java/`` (e.g. ``thirdparty-src/main/java/``) must not
    be mistaken for a real ``src/main/java`` root.
    If a marker appears under more than one distinct prefix (e.g. a
    multi-module monorepo with two ``src/main/java`` trees), that marker's
    root is left ``None`` (ambiguous) rather than guessed — imports stay
    external metadata in that case, per this codebase's precision-over-recall
    stance (D19). The package-declaration-derived fallback mentioned as an
    option for non-conventional layouts is intentionally not implemented: it
    would require reading file content during ``begin_run`` (before any file
    is parsed), which this codebase's parsers don't do.
    """
    candidates: dict[str, set[str]] = {"main": set(), "test": set()}
    for f in known_files:
        if not f.endswith(".java"):
            continue
        parts = f.split("/")
        for kind, segments in _JAVA_SOURCE_SEGMENTS.items():
            n = len(segments)
            for i in range(len(parts) - n + 1):
                if tuple(parts[i : i + n]) == segments:
                    candidates[kind].add("/".join(parts[: i + n]) + "/")
                    break  # first occurrence in this file is enough
    return {
        kind: next(iter(roots)) if len(roots) == 1 else None
        for kind, roots in candidates.items()
    }


def _index_files_by_dir(
    known_files: frozenset[str], suffix: str, exclude_suffix: str | None = None,
) -> dict[str, list[str]]:
    """Group scanned paths by directory for O(1) package-directory lookups.

    Go/Java "resolve every file in this exact directory" import resolution
    (D19) would otherwise re-scan the full ``known_files`` set per import
    statement; this precomputes the grouping once in ``begin_run``, the same
    place ``_java_source_roots``/``_go_module_prefix`` are computed, so
    per-import resolution is a dict lookup regardless of corpus size (in the
    spirit of D30's scale-hardening precedent against repeated full scans).
    """
    index: dict[str, list[str]] = {}
    for f in known_files:
        if not f.endswith(suffix):
            continue
        if exclude_suffix is not None and f.endswith(exclude_suffix):
            continue
        index.setdefault(posixpath.dirname(f), []).append(f)
    return index


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
    #: whether this callee name may also resolve to a Class (INSTANTIATES,
    #: D32). False for languages whose call-shaped grammar node can carry a
    #: non-instantiation meaning that would otherwise be misread as one (Go's
    #: `T(x)` type conversion parses as the same `call_expression` shape as
    #: a real call) -- see `_Extractor.SUPPORTS_INSTANTIATES`.
    allow_instantiate: bool = True


@dataclass
class _PendingImportCall:
    """An import-qualified call/constructor whose resolution needs a batched
    existence check (D32), rather than the blind speculative id construction
    D16 uses for the CALLS-only case.

    D16's import-qualified CALLS deliberately never checks existence: the
    target id is computed and, if wrong, the existing orphan-edge sweep
    removes the dangling edge in the same transaction. That trick stops
    being safe once a *second* candidate type (Class) enters the picture,
    because both a Function-shaped and a Class-shaped target id could each
    correspond to a real node (e.g. the imported module legitimately defines
    both a function and a class of the same name) -- blindly emitting both
    would violate the "never double-emit" rule, since neither one would
    dangle for the orphan sweep to clean up. Deferring to one batched
    ``id IN (...)`` lookup in `finish_run` (same batching pattern as the
    name-match pass) resolves this deterministically: CALLS wins whenever
    the Function/Method target exists, matching the same-file precedence
    rule exactly, and it costs one extra batched lookup for the run's
    import-qualified calls -- not a new per-call query.
    """

    caller_id: str
    callee_name: str
    path: str
    line: int
    func_target_id: str
    #: None when the caller's language doesn't support INSTANTIATES (D32).
    class_target_id: str | None


class CodeParser(Parser):
    """Parser interface entry point; per-file work happens in extractors."""

    name = EXTRACTOR_NAME

    def __init__(self) -> None:
        self._known_files: frozenset[str] = frozenset()
        self._pending_calls: list[_PendingCall] = []
        self._pending_import_calls: list[_PendingImportCall] = []
        self._go_module_prefix: str | None = None
        self._java_source_roots: dict[str, str | None] = {"main": None, "test": None}
        self._go_dir_index: dict[str, list[str]] = {}
        self._java_dir_index: dict[str, list[str]] = {}

    # -- indexer hooks -------------------------------------------------------

    def begin_run(
        self, known_files: set[str], root: str | Path | None = None,
    ) -> None:
        """Called by the indexer with every scanned repo path (for import
        resolution) before any file is parsed. ``root`` is the indexed repo
        root; it is used only for the bounded ``go.mod`` read described in
        ``_read_go_module_prefix`` (optional — omitting it just means Go
        imports stay external metadata, as before this milestone)."""
        self._known_files = frozenset(known_files)
        self._pending_calls = []
        self._pending_import_calls = []
        self._go_module_prefix = _read_go_module_prefix(root)
        self._java_source_roots = _detect_java_source_roots(self._known_files)
        # Precomputed once per run so per-import resolution (D19) is a dict
        # lookup, not a scan of the full known-files set per import spec.
        self._go_dir_index = _index_files_by_dir(
            self._known_files, ".go", exclude_suffix="_test.go",
        )
        self._java_dir_index = _index_files_by_dir(self._known_files, ".java")

    def finish_run(self, store) -> list[Edge]:
        """Cross-file name-only CALLS/INSTANTIATES resolution (after nodes
        are upserted).

        A pending call becomes a CALLS edge when exactly one Function/Method
        node in the whole graph carries that name (precision over recall).
        Failing that, it becomes an INSTANTIATES edge (D32) under the exact
        same uniqueness rule but over Class nodes instead -- and only when
        the pending call's own language allows constructor resolution
        (`allow_instantiate`; see `_Extractor.SUPPORTS_INSTANTIATES`). CALLS
        is always tried first and wins deterministically: a name that
        happens to match both a Function/Method and a Class only ever
        produces the CALLS edge, never both (spec: don't double-emit).

        Candidate lookup is batched into chunked ``name IN (...)`` queries
        instead of one query per distinct callee name: a large run can queue
        thousands of distinct bare-call names, and one round trip per name is
        an avoidable N+1 pattern the SQL index doesn't fix. Chunking keeps
        each statement's parameter count bounded regardless of corpus size.
        """
        edges: list[Edge] = []
        by_name: dict[str, list[_PendingCall]] = {}
        for pc in self._pending_calls:
            by_name.setdefault(pc.callee_name, []).append(pc)
        names = list(by_name)
        func_matches: dict[str, list] = {name: [] for name in names}
        class_matches: dict[str, list] = {name: [] for name in names}
        for chunk in (names[i : i + 400] for i in range(0, len(names), 400)):
            placeholders = ",".join("?" for _ in chunk)
            rows = store.conn.execute(
                "SELECT id, path, name, type FROM nodes "
                "WHERE type IN ('Function', 'Method', 'Class') "
                f"AND name IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                bucket = class_matches if row["type"] == "Class" else func_matches
                bucket[row["name"]].append(row)

        def _path_counts(rows: list) -> dict[str, int]:
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["path"]] = counts.get(r["path"], 0) + 1
            return counts

        # A node's id embeds its path (D2), and a pending call's caller
        # always lives in `pc.path`, so `r["id"] == pc.caller_id` can only
        # ever hold when `r["path"] == pc.path` already does -- excluding by
        # path alone is equivalent to the old combined filter. Precomputing
        # per-path counts turns "is there exactly one candidate outside my
        # own file" into an O(1) arithmetic check per pending call. This
        # matters for a name that is common across a large repo (`run`,
        # `__init__`, ...): without it, every pending call sharing that name
        # would re-scan every match for that name, an O(matches * calls)
        # blowup for exactly the popular names most likely to appear at
        # scale.
        for name, pcs in by_name.items():
            func_rows = func_matches[name]
            class_rows = class_matches[name]
            func_total = len(func_rows)
            func_path_counts = _path_counts(func_rows)
            class_total = len(class_rows)
            class_path_counts = _path_counts(class_rows)
            for pc in pcs:
                if func_total and func_total - func_path_counts.get(pc.path, 0) == 1:
                    candidates = [r for r in func_rows if r["path"] != pc.path]
                    if len(candidates) != 1:
                        continue  # defensive; unreachable given the count above
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
                    continue  # CALLS wins: never also emit INSTANTIATES (D32)
                if not pc.allow_instantiate or not class_total:
                    continue
                if class_total - class_path_counts.get(pc.path, 0) != 1:
                    continue
                candidates = [r for r in class_rows if r["path"] != pc.path]
                if len(candidates) != 1:
                    continue  # defensive; unreachable given the count above
                edges.append(
                    Edge(
                        type=EdgeType.INSTANTIATES,
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
        edges.extend(self._resolve_pending_import_calls(store))
        self._pending_import_calls = []
        return edges

    def _resolve_pending_import_calls(self, store) -> list[Edge]:
        """Batched existence check for import-qualified CALLS/INSTANTIATES
        candidates (D32; see `_PendingImportCall`).

        One or two chunked ``id IN (...)`` lookups regardless of how many
        import-qualified calls this run queued -- the same batching pattern
        `finish_run`'s name-match pass already uses, just keyed by id
        instead of name. CALLS wins deterministically whenever the
        Function/Method target exists, so a name ambiguous between a
        same-named Function and Class in the target module never produces
        both edges.
        """
        pics = self._pending_import_calls
        if not pics:
            return []
        all_ids: set[str] = set()
        for pic in pics:
            all_ids.add(pic.func_target_id)
            if pic.class_target_id is not None:
                all_ids.add(pic.class_target_id)
        id_list = list(all_ids)
        existing_types: dict[str, str] = {}
        for chunk in (id_list[i : i + 400] for i in range(0, len(id_list), 400)):
            placeholders = ",".join("?" for _ in chunk)
            rows = store.conn.execute(
                f"SELECT id, type FROM nodes WHERE id IN ({placeholders})", chunk,
            ).fetchall()
            for row in rows:
                existing_types[row["id"]] = row["type"]
        edges: list[Edge] = []
        for pic in pics:
            if existing_types.get(pic.func_target_id) in ("Function", "Method"):
                edges.append(
                    Edge(
                        type=EdgeType.CALLS,
                        source_node_id=pic.caller_id,
                        target_node_id=pic.func_target_id,
                        path=pic.path,
                        start_line=pic.line,
                        end_line=pic.line,
                        confidence=SAME_FILE_CALL_CONFIDENCE,
                        extractor=EXTRACTOR_NAME,
                        metadata={"callee": pic.callee_name, "resolution": "import"},
                    )
                )
                continue  # CALLS wins: never also emit INSTANTIATES (D32)
            if pic.class_target_id is not None and existing_types.get(pic.class_target_id) == "Class":
                edges.append(
                    Edge(
                        type=EdgeType.INSTANTIATES,
                        source_node_id=pic.caller_id,
                        target_node_id=pic.class_target_id,
                        path=pic.path,
                        start_line=pic.line,
                        end_line=pic.line,
                        confidence=SAME_FILE_CALL_CONFIDENCE,
                        extractor=EXTRACTOR_NAME,
                        metadata={"callee": pic.callee_name, "resolution": "import"},
                    )
                )
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
    #: whether a bare call-shaped node whose callee name matches a Class may
    #: become an INSTANTIATES edge (D32). True by default: for every
    #: language wired to `_resolve_plain_call`/`_resolve_module_attr_call`,
    #: a call-shaped tree-sitter node whose callee resolves to a Class is
    #: unambiguously a constructor invocation. Go is the one exception (see
    #: `_GoExtractor`): its grammar parses a type conversion like `T(x)` as
    #: the identical `call_expression` shape, so a same-named Class match
    #: there would misread a conversion as an instantiation.
    SUPPORTS_INSTANTIATES: bool = True

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
        self.orm_models: list[dict] = []
        self.orm_operations: list[dict] = []

    # -- pipeline -------------------------------------------------------------

    def run(self, captures: dict[str, list]) -> None:
        self._emit_module()
        self._register_defs(captures)
        self._register_tests(captures)
        self._link_defs()
        self._extract_variables(captures)
        self._extract_imports(captures)
        self._extract_runtime_facts(captures)
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
        edge_type: str = EdgeType.CALLS,
    ) -> None:
        self.result.edges.append(
            Edge(
                type=edge_type,
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
        """Shared bare-name call/constructor resolution ladder (D16/D32).

        Mirrors CALLS' confidence ladder for INSTANTIATES: a name is always
        checked against callables (``func_by_name``) before classes
        (``classes_by_name``), so a same-file function deterministically
        wins over a same-named class -- there is never a double-emit for a
        name that is locally ambiguous between the two.
        """
        source = self._call_source(ts)
        line = ts.start_point[0] + 1
        if name in self.func_by_name:
            target = self.func_by_name[name]
            self._add_call_edge(source, target.id, line, "same-file", name)
            return
        if name in self.classes_by_name:
            if self.SUPPORTS_INSTANTIATES:
                target = self.classes_by_name[name]
                self._add_call_edge(
                    source, target.id, line, "same-file", name,
                    edge_type=EdgeType.INSTANTIATES,
                )
            return
        if name in self.symbol_aliases:
            mod_qname, mod_path, symbol = self.symbol_aliases[name]
            func_target_id = node_id(NodeType.FUNCTION, f"{mod_qname}.{symbol}", mod_path)
            class_target_id = (
                node_id(NodeType.CLASS, f"{mod_qname}.{symbol}", mod_path)
                if self.SUPPORTS_INSTANTIATES else None
            )
            self.parser._pending_import_calls.append(
                _PendingImportCall(
                    caller_id=source.id, callee_name=name, path=self.path,
                    line=line, func_target_id=func_target_id,
                    class_target_id=class_target_id,
                )
            )
            return
        self.parser._pending_calls.append(
            _PendingCall(
                caller_id=source.id,
                caller_qname=source.qualified_name,
                callee_name=name,
                path=self.path,
                line=line,
                allow_instantiate=self.SUPPORTS_INSTANTIATES,
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
        """alias.func() / alias.ClassName() where alias is an imported
        internal module (D32: resolved via the same batched existence check
        as the `symbol_aliases` case -- see `_PendingImportCall`)."""
        if prefix not in self.module_aliases:
            return
        mod_qname, mod_path = self.module_aliases[prefix]
        source = self._call_source(ts)
        line = ts.start_point[0] + 1
        func_target_id = node_id(NodeType.FUNCTION, f"{mod_qname}.{name}", mod_path)
        class_target_id = (
            node_id(NodeType.CLASS, f"{mod_qname}.{name}", mod_path)
            if self.SUPPORTS_INSTANTIATES else None
        )
        self.parser._pending_import_calls.append(
            _PendingImportCall(
                caller_id=source.id, callee_name=name, path=self.path,
                line=line, func_target_id=func_target_id,
                class_target_id=class_target_id,
            )
        )

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
        bindings = [
            {"local": local, "kind": "symbol", "module": value[0],
             "target_path": value[1], "symbol": value[2]}
            for local, value in sorted(self.symbol_aliases.items())
        ]
        bindings.extend(
            {"local": local, "kind": "module", "module": value[0],
             "target_path": value[1]}
            for local, value in sorted(self.module_aliases.items())
        )
        self.module_node.metadata.update(
            {
                "external_imports": sorted(self.external_imports),
                "import_bindings": bindings,
                "is_test_file": self.is_test,
                "orm_models": self.orm_models,
                "orm_operations": self.orm_operations,
            }
        )

    # -- per-language hooks (defaults: no-ops) ---------------------------------

    def _register_tests(self, captures: dict[str, list]) -> None:
        pass

    def _extract_variables(self, captures: dict[str, list]) -> None:
        pass

    def _extract_imports(self, captures: dict[str, list]) -> None:
        pass

    def _extract_runtime_facts(self, captures: dict[str, list]) -> None:
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

    def _extract_runtime_facts(self, captures: dict[str, list]) -> None:
        for class_ts in captures.get("def.class", []):
            class_name = self._def_name(class_ts)
            body = class_ts.child_by_field_name("body")
            if not class_name or body is None:
                continue
            for statement in body.named_children:
                if statement.type != "expression_statement":
                    assignment = statement
                elif statement.named_children:
                    assignment = statement.named_children[0]
                else:
                    continue
                if assignment.type != "assignment":
                    continue
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                if left is None or self._text(left) != "__tablename__":
                    continue
                table = self._string_value(right)
                if table:
                    self.orm_models.append({
                        "framework": "sqlalchemy", "class": class_name,
                        "table": table, "line": assignment.start_point[0] + 1,
                    })

        for call in captures.get("call", []):
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            callee = self._text(fn)
            args = call.child_by_field_name("arguments")
            named_args = list(args.named_children) if args is not None else []
            model = None
            operation = None
            pattern = None
            parts = callee.split(".")
            if len(parts) >= 3 and parts[1] == "query":
                model, operation, pattern = parts[0], "read", "Model.query.*"
            elif callee == "select" and named_args and named_args[0].type == "identifier":
                model, operation, pattern = self._text(named_args[0]), "read", "select(Model)"
            elif callee.endswith(".get") and "session" in parts[:-1] and named_args:
                if named_args[0].type == "identifier":
                    model, operation, pattern = self._text(named_args[0]), "read", "session.get(Model, ...)"
            elif callee.endswith((".add", ".merge")) and "session" in parts[:-1] and named_args:
                candidate = named_args[0]
                if candidate.type == "call":
                    constructor = candidate.child_by_field_name("function")
                    if constructor is not None and constructor.type == "identifier":
                        model = self._text(constructor)
                        operation = "write"
                        pattern = f"session.{parts[-1]}(Model(...))"
            if not model or not operation:
                continue
            source = self._call_source(call)
            self.orm_operations.append({
                "framework": "sqlalchemy", "operation": operation,
                "model_binding": model, "source_id": source.id,
                "source_qname": source.qualified_name,
                "line": call.start_point[0] + 1, "pattern": pattern,
            })

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
    # Go has no `new ClassName()` call-expression constructor syntax --
    # struct literals (`Foo{}` / `&Foo{}`) are a different tree-sitter shape
    # entirely and are never captured as `@call`. Meanwhile Go's type
    # conversion syntax `T(x)` (T a named type) *does* parse as an ordinary
    # `call_expression`, identical in shape to a real call. Without this
    # flag, a type conversion whose type name happens to be a known Class
    # would be misread as an INSTANTIATES edge -- a real false positive,
    # not a hypothetical one, so Go stays out of scope for INSTANTIATES.
    SUPPORTS_INSTANTIATES = False

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
            if not value:
                continue
            line = spec.start_point[0] + 1
            targets = self._resolve_go_import(value)
            if targets:
                for qname, path in targets:
                    self._add_import_edge(qname, path, line)
            else:
                self.external_imports.add(value)

    def _resolve_go_import(self, import_path: str) -> list[tuple[str, str]] | None:
        """Resolve a Go import path to every non-test ``.go`` file in the
        corresponding package directory (D19).

        Go imports name a package (a directory), not a single file, and a
        package commonly spans multiple files; rather than guess which file
        the caller means, an internal import resolves to *every* file-Module
        in that exact directory (no recursion — Go packages are one
        directory each). ``_test.go`` files are excluded as targets because
        an external package cannot import another package's test files.
        Returns ``None`` (stays external metadata) when the module prefix is
        unknown or the import falls outside it — never a guessed target.
        """
        module = self.parser._go_module_prefix
        if not module:
            return None
        if import_path == module:
            rel_dir = ""
        elif import_path.startswith(f"{module}/"):
            rel_dir = import_path[len(module) + 1 :]
        else:
            return None
        matches = [
            (default_module_qname(f), f)
            for f in self.parser._go_dir_index.get(rel_dir, [])
        ]
        return matches or None

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
            if not text:
                continue
            line = imp.start_point[0] + 1
            dotted = text
            if dotted.startswith("static "):
                # `import static pkg.Class.member;` — the resolvable target
                # is the class, not the individual static member.
                dotted = dotted[len("static "):].strip()
                dotted = dotted.rsplit(".", 1)[0] if "." in dotted else dotted
            wildcard = dotted.endswith(".*")
            if wildcard:
                dotted = dotted[:-2]
            targets = self._resolve_java_import(dotted, wildcard)
            if targets:
                for qname, path in targets:
                    self._add_import_edge(qname, path, line)
            else:
                self.external_imports.add(text)

    def _resolve_java_import(
        self, dotted: str, wildcard: bool,
    ) -> list[tuple[str, str]] | None:
        """Resolve a fully-qualified Java import against the detected
        conventional source root(s) (D19).

        `com.example.pkg.ClassName` maps to
        `<source-root>/com/example/pkg/ClassName.java`. A wildcard
        (`import com.example.pkg.*;`) mirrors Go's multi-file package
        handling: it resolves to every `.java` file directly in that package
        directory (non-recursive) rather than guessing a single class.
        Checks the `main` root before `test` (imports of test-only classes
        from production code are not a real Java scenario, but a test file
        importing another test-tree class still resolves).
        """
        roots = self.parser._java_source_roots
        for kind in ("main", "test"):
            root = roots.get(kind)
            if not root:
                continue
            if wildcard:
                pkg_dir = f"{root}{dotted.replace('.', '/')}".rstrip("/")
                matches = [
                    (default_module_qname(f), f)
                    for f in self.parser._java_dir_index.get(pkg_dir, [])
                ]
                if matches:
                    return matches
            else:
                candidate = f"{root}{dotted.replace('.', '/')}.java"
                if candidate in self.known:
                    return [(default_module_qname(candidate), candidate)]
        return None

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

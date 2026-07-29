"""Parser interface and registry."""
from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType


_RAW_FTS_EXCLUDED_LANGUAGES = {"dockerfile", "json", "toml", "yaml"}

#: Bump by hand when extraction changes *outside* this package — most of all
#: :func:`repobrain.indexing.scanner.detect_language`, which decides which
#: parsers run at all. Changes to the parsers themselves need no bump:
#: :meth:`ParserRegistry.fingerprint` hashes registry composition and the
#: parser sources, so both are detected without anyone remembering.
EXTRACTOR_VERSION = "1"


def parser_source_digest() -> str:
    """Digest the bytes of every parser module in this package.

    Read fresh on every call rather than cached: ``fingerprint()`` runs on
    every freshness check, including a statusline polling ``freshness`` on a
    timer, and a cache would hold a stale answer across exactly the edit this
    exists to notice. Measured at 0.27 ms over the current ~147 KB of sources.

    Names are hashed alongside the bytes so that renaming a parser module is a
    change even when the corpus of bytes happens to be identical.
    """
    directory = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(directory.glob("*.py"), key=lambda item: item.name):
        digest.update(source.name.encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()


@dataclass
class ParseResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fts_rows: list[FtsRow] = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.warnings.extend(other.warnings)
        self.fts_rows.extend(other.fts_rows)


class Parser:
    """Base parser. Subclasses set `name` and implement both methods."""

    name = "base"

    def can_parse(self, path: str, language: str | None) -> bool:
        raise NotImplementedError

    def parse(self, path: str, content: str) -> ParseResult:
        raise NotImplementedError


class GenericFileParser(Parser):
    """Creates a File node, Directory nodes + CONTAINS edges, and an FTS
    content row for every text file."""

    name = "generic_file_parser"

    def can_parse(self, path: str, language: str | None) -> bool:
        return True

    def parse(self, path: str, content: str) -> ParseResult:
        from ..indexing.scanner import detect_language

        result = ParseResult()
        line_count = len(content.splitlines())
        language = detect_language(path)
        file_node = Node(
            type=NodeType.FILE,
            name=posixpath.basename(path),
            qualified_name=path,
            path=path,
            start_line=1 if line_count else None,
            end_line=line_count or None,
            language=language,
            extractor=self.name,
        )
        result.nodes.append(file_node)
        # Structured configuration parsers emit value-free facts. Persisting
        # their raw file content here would bypass that boundary and retain
        # credentials commonly embedded in JSON, TOML, ARG, or ENV values.
        if language not in _RAW_FTS_EXCLUDED_LANGUAGES:
            result.fts_rows.append(
                FtsRow(
                    path=path,
                    name=file_node.name,
                    content=content,
                    node_id=file_node.id,
                )
            )

        # Directory chain: a/b/c.py -> Directory(a), Directory(a/b),
        # a CONTAINS a/b (provenance path = child dir), a/b CONTAINS file.
        parts = path.split("/")[:-1]
        prev_dir: Node | None = None
        for i in range(len(parts)):
            dir_path = "/".join(parts[: i + 1])
            dir_node = Node(
                type=NodeType.DIRECTORY,
                name=parts[i],
                qualified_name=dir_path,
                path=dir_path,
                extractor=self.name,
            )
            result.nodes.append(dir_node)
            if prev_dir is not None:
                result.edges.append(
                    Edge(
                        type=EdgeType.CONTAINS,
                        source_node_id=prev_dir.id,
                        target_node_id=dir_node.id,
                        path=dir_path,
                        extractor=self.name,
                    )
                )
            prev_dir = dir_node
        if prev_dir is not None:
            result.edges.append(
                Edge(
                    type=EdgeType.CONTAINS,
                    source_node_id=prev_dir.id,
                    target_node_id=file_node.id,
                    path=path,
                    extractor=self.name,
                )
            )
        return result


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: list[Parser] = []

    def register(self, parser: Parser) -> None:
        self._parsers.append(parser)

    def parsers_for(self, path: str, language: str | None) -> list[Parser]:
        return [p for p in self._parsers if p.can_parse(path, language)]

    def all(self) -> list[Parser]:
        return list(self._parsers)

    def fingerprint(self) -> str:
        """Identify what this registry would extract, for staleness comparison.

        A stored graph is only as current as the extractor that produced it.
        File stat and content hashes answer "did the tree move", which says
        nothing after RepoBrain itself changes: the bytes are identical and the
        facts derivable from them are not. Comparing this value against the one
        recorded at index time is how that becomes visible.

        Composition covers a parser being added or removed. The source digest
        covers the case composition cannot see and that a hand-maintained
        constant cannot be trusted to catch — a parser keeping its name while
        changing its output.
        """
        parts = [
            EXTRACTOR_VERSION,
            parser_source_digest(),
            *sorted(parser.name for parser in self._parsers),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def default_registry() -> ParserRegistry:
    from .code_treesitter import CodeParser
    from .config_parser import EnvFileParser
    from .dockerfile_parser import DockerfileParser
    from .markdown_parser import MarkdownParser
    from .structured_config_parser import JsonParser, TomlParser
    from .yaml_parser import YamlParser
    from .route_parser import RouteParser

    registry = ParserRegistry()
    registry.register(GenericFileParser())
    registry.register(EnvFileParser())
    registry.register(JsonParser())
    registry.register(TomlParser())
    registry.register(DockerfileParser())
    registry.register(YamlParser())
    registry.register(MarkdownParser())
    registry.register(CodeParser())
    registry.register(RouteParser())
    return registry

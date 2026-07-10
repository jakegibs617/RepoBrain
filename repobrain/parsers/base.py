"""Parser interface and registry."""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType


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
        result.fts_rows.append(
            FtsRow(path=path, name=file_node.name, content=content, node_id=file_node.id)
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


def default_registry() -> ParserRegistry:
    from .code_treesitter import CodeParser
    from .config_parser import EnvFileParser
    from .markdown_parser import MarkdownParser
    from .yaml_parser import YamlParser
    from .route_parser import RouteParser

    registry = ParserRegistry()
    registry.register(GenericFileParser())
    registry.register(EnvFileParser())
    registry.register(YamlParser())
    registry.register(MarkdownParser())
    registry.register(CodeParser())
    registry.register(RouteParser())
    return registry

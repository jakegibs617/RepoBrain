"""Parsers for dotenv-style environment definition files."""
from __future__ import annotations

import posixpath
import re

from .base import ParseResult, Parser
from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType


_ENV_FILE_RE = re.compile(r"(^|/)(\.env(?:\.[^/]+)?|[^/]*\.env(?:\.[^/]+)?)$")
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class EnvFileParser(Parser):
    """Extract definitions from .env, .env.example, and similarly named files."""

    name = "env_file_parser"

    def can_parse(self, path: str, language: str | None) -> bool:
        return bool(_ENV_FILE_RE.search(path))

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        config = Node(
            type=NodeType.CONFIG_FILE,
            name=posixpath.basename(path),
            qualified_name=path,
            path=path,
            start_line=1 if content else None,
            end_line=len(content.splitlines()) or None,
            language="config",
            extractor=self.name,
        )
        result.nodes.append(config)
        result.fts_rows.append(FtsRow(path=path, name=config.name, content=content, node_id=config.id))
        for line_no, line in enumerate(content.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _ASSIGNMENT_RE.match(line)
            if not match:
                result.warnings.append(f"{path}:{line_no}: ignored invalid environment assignment")
                continue
            name, raw_value = match.groups()
            value = raw_value.strip()
            key = Node(
                type=NodeType.CONFIG_KEY,
                name=name,
                qualified_name=name,
                path=path,
                start_line=line_no,
                end_line=line_no,
                metadata={"value": value, "format": "dotenv"},
                extractor=self.name,
            )
            env = Node(type=NodeType.ENV_VAR, name=name, qualified_name=name, path="", extractor=self.name)
            result.nodes.extend((key, env))
            result.edges.extend((
                Edge(type=EdgeType.DECLARES_CONFIG, source_node_id=config.id, target_node_id=key.id,
                     path=path, start_line=line_no, extractor=self.name),
                Edge(type=EdgeType.SETS_ENV, source_node_id=key.id, target_node_id=env.id,
                     path=path, start_line=line_no, metadata={"value": value}, extractor=self.name),
            ))
        return result

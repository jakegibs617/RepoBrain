"""Grounded Dockerfile instruction and base-image extraction."""
from __future__ import annotations

import posixpath
import re

from .base import ParseResult, Parser
from ..graph.schema import Edge, EdgeType, Node, NodeType


_INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)\s+(.*?)\s*$", re.DOTALL)
_FROM = re.compile(
    r"^(?:(--platform=\S+)\s+)?(\S+)(?:\s+[Aa][Ss]\s+([A-Za-z0-9_.-]+))?$"
)
_EXPOSE_PORT = re.compile(r"^(?:[1-9][0-9]{0,4})(?:/(?:tcp|udp))?$", re.IGNORECASE)


def _logical_lines(content: str):
    buffered: list[str] = []
    start_line = 0
    for line_number, raw in enumerate(content.splitlines(), 1):
        stripped = raw.strip()
        if not buffered and (not stripped or stripped.startswith("#")):
            continue
        if not buffered:
            start_line = line_number
        continued = raw.rstrip().endswith("\\")
        buffered.append(raw.rstrip()[:-1] if continued else raw)
        if not continued:
            yield start_line, " ".join(part.strip() for part in buffered)
            buffered = []
    if buffered:
        yield start_line, " ".join(part.strip() for part in buffered)


class DockerfileParser(Parser):
    name = "dockerfile_parser"

    def can_parse(self, path: str, language: str | None) -> bool:
        if language is not None:
            return language == "dockerfile"
        from ..indexing.scanner import is_dockerfile_name

        return is_dockerfile_name(posixpath.basename(path))

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        config = Node(
            type=NodeType.CONFIG_FILE,
            name=posixpath.basename(path),
            qualified_name=path,
            path=path,
            start_line=1 if content else None,
            end_line=len(content.splitlines()) or None,
            language="dockerfile",
            metadata={"format": "dockerfile"},
            extractor=self.name,
        )
        result.nodes.append(config)

        occurrence: dict[str, int] = {}
        for line_number, logical in _logical_lines(content):
            match = _INSTRUCTION.match(logical)
            if not match:
                result.warnings.append(
                    f"{path}:{line_number}: ignored invalid Dockerfile instruction"
                )
                continue
            instruction, arguments = match.groups()
            instruction = instruction.upper()
            occurrence[instruction] = occurrence.get(instruction, 0) + 1
            metadata: dict[str, object] = {"instruction": instruction}
            # Retain only narrowly typed, non-secret fields. Raw Dockerfile
            # arguments (especially RUN, ENV, and ARG) may contain credentials.
            if instruction == "EXPOSE":
                ports = [
                    value
                    for value in arguments.split()
                    if _EXPOSE_PORT.fullmatch(value)
                    and int(value.split("/", 1)[0]) <= 65535
                ]
                if ports:
                    metadata["ports"] = ports
            key = Node(
                type=NodeType.CONFIG_KEY,
                name=instruction,
                qualified_name=f"{path}:{instruction}:{occurrence[instruction]}",
                path=path,
                start_line=line_number,
                end_line=line_number,
                language="dockerfile",
                metadata=metadata,
                extractor=self.name,
            )
            result.nodes.append(key)
            result.edges.append(
                Edge(
                    type=EdgeType.DECLARES_CONFIG,
                    source_node_id=config.id,
                    target_node_id=key.id,
                    path=path,
                    start_line=line_number,
                    extractor=self.name,
                )
            )

            if instruction != "FROM":
                continue
            from_match = _FROM.match(arguments)
            if not from_match:
                result.warnings.append(
                    f"{path}:{line_number}: could not parse FROM instruction"
                )
                continue
            platform, image_name, stage = from_match.groups()
            key.metadata.update(
                item
                for item in (("platform", platform), ("stage", stage))
                if item[1] is not None
            )
            if image_name.startswith("$"):
                continue
            image = Node(
                type=NodeType.DOCKER_IMAGE,
                name=image_name,
                qualified_name=image_name,
                path="",
                extractor=self.name,
            )
            result.nodes.append(image)
            result.edges.append(
                Edge(
                    type=EdgeType.USES_IMAGE,
                    source_node_id=key.id,
                    target_node_id=image.id,
                    path=path,
                    start_line=line_number,
                    extractor=self.name,
                )
            )
        return result

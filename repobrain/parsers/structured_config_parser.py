"""Conservative, value-free extraction for JSON and TOML configuration."""
from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Mapping

from .base import ParseResult, Parser
from ..graph.schema import Edge, EdgeType, Node, NodeType


KeyPath = tuple[str, ...]
_TOML_TABLE = re.compile(r"^\s*\[\[?([^\]]+?)\]?\]\s*(?:#.*)?$")
_TOML_KEY = re.compile(
    r"""^\s*((?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)(?:\s*\.\s*(?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+))*)\s*="""
)


def _walk_keys(value: object, path: KeyPath = ()) -> Iterator[tuple[KeyPath, object]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = (*path, str(key))
            yield child_path, child
            yield from _walk_keys(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_keys(child, (*path, str(index)))


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _json_lines(content: str) -> dict[str, deque[int]]:
    lines: dict[str, deque[int]] = defaultdict(deque)
    key_pattern = re.compile(r'("(?:[^"\\]|\\.)*")\s*:')
    for line_number, line in enumerate(content.splitlines(), 1):
        for match in key_pattern.finditer(line):
            try:
                key = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(key, str):
                lines[key].append(line_number)
    return lines


def _toml_part(text: str) -> str:
    text = text.strip()
    if text.startswith('"'):
        return json.loads(text)
    if text.startswith("'"):
        return text[1:-1]
    return text


def _split_toml_path(text: str) -> KeyPath:
    parts = re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+', text)
    return tuple(_toml_part(part) for part in parts)


def _toml_lines(content: str) -> dict[KeyPath, int]:
    current: KeyPath = ()
    lines: dict[KeyPath, int] = {}
    for line_number, line in enumerate(content.splitlines(), 1):
        table = _TOML_TABLE.match(line)
        if table:
            current = _split_toml_path(table.group(1))
            if current:
                lines.setdefault(current, line_number)
            continue
        assignment = _TOML_KEY.match(line)
        if assignment:
            key_path = (*current, *_split_toml_path(assignment.group(1)))
            if key_path:
                lines.setdefault(key_path, line_number)
    return lines


class _StructuredConfigParser(Parser):
    language = ""
    suffix = ""

    def load(self, content: str) -> object:
        raise NotImplementedError

    def line_lookup(self, content: str) -> Callable[[KeyPath], int | None]:
        raise NotImplementedError

    def can_parse(self, path: str, language: str | None) -> bool:
        return language == self.language or posixpath.splitext(path)[1].lower() == self.suffix

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        config = Node(
            type=NodeType.CONFIG_FILE,
            name=posixpath.basename(path),
            qualified_name=path,
            path=path,
            start_line=1 if content else None,
            end_line=len(content.splitlines()) or None,
            language=self.language,
            metadata={"format": self.language},
            extractor=self.name,
        )
        result.nodes.append(config)
        try:
            data = self.load(content)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError):
            # Parser exception messages can echo source fragments. Keep the
            # warning useful without turning warnings_json into a value sink.
            result.warnings.append(f"{path}: invalid {self.language.upper()}")
            return result

        locate = self.line_lookup(content)
        made: dict[KeyPath, Node] = {}
        for key_path, value in _walk_keys(data):
            line = locate(key_path)
            key = Node(
                type=NodeType.CONFIG_KEY,
                name=key_path[-1],
                qualified_name=".".join(key_path),
                path=path,
                start_line=line,
                end_line=line,
                language=self.language,
                metadata={"value_type": _value_type(value)},
                extractor=self.name,
            )
            result.nodes.append(key)
            made[key_path] = key
            parent_path = key_path[:-1]
            while parent_path and parent_path not in made:
                parent_path = parent_path[:-1]
            parent = made.get(parent_path, config)
            result.edges.append(
                Edge(
                    type=(
                        EdgeType.DECLARES_CONFIG
                        if parent is config
                        else EdgeType.CONTAINS
                    ),
                    source_node_id=parent.id,
                    target_node_id=key.id,
                    path=path,
                    start_line=line,
                    extractor=self.name,
                )
            )
        return result


class JsonParser(_StructuredConfigParser):
    name = "json_parser"
    language = "json"
    suffix = ".json"

    def load(self, content: str) -> object:
        return json.loads(content)

    def line_lookup(self, content: str) -> Callable[[KeyPath], int | None]:
        occurrences = _json_lines(content)

        def locate(path: KeyPath) -> int | None:
            matches = occurrences[path[-1]]
            return matches.popleft() if matches else None

        return locate


class TomlParser(_StructuredConfigParser):
    name = "toml_parser"
    language = "toml"
    suffix = ".toml"

    def load(self, content: str) -> object:
        return tomllib.loads(content)

    def line_lookup(self, content: str) -> Callable[[KeyPath], int | None]:
        lines = _toml_lines(content)
        return lines.get

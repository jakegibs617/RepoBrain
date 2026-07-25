"""Markdown parser built on markdown-it-py.

Produces one MarkdownDocument node per file and a MarkdownSection node per
heading (section spans follow the heading hierarchy, nested via CONTAINS
edges). Extracts links and fenced code blocks into section/document metadata,
and TODO/FIXME list items as Task nodes. Section content is emitted as FTS
rows so full-text search can hit individual sections.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from markdown_it.token import Token

from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType
from .base import Parser, ParseResult

_TASK_RE = re.compile(r"\b(TODO|FIXME)\b")
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s*")


@dataclass
class _Section:
    level: int
    title: str
    start_line: int  # 1-based inclusive
    end_line: int = 0
    parent: "_Section | None" = None
    node: Node | None = None
    links: list[dict] = field(default_factory=list)
    code_blocks: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)


class MarkdownParser(Parser):
    name = "markdown_parser"

    def __init__(self) -> None:
        self.md = MarkdownIt("commonmark").enable("table")

    def can_parse(self, path: str, language: str | None) -> bool:
        return language == "markdown" or path.lower().endswith((".md", ".markdown"))

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        lines = content.splitlines()
        try:
            tokens = self.md.parse(content)
        except Exception as exc:  # markdown-it rarely throws; degrade gracefully
            result.warnings.append(f"{path}: markdown parse failed: {exc}")
            return result

        sections = self._collect_sections(tokens, total_lines=len(lines))
        links = self._collect_links(tokens)
        inline_code = self._collect_inline_code(tokens)
        fences = self._collect_code_blocks(tokens)
        tasks = self._collect_tasks(tokens)

        title = sections[0].title if sections and sections[0].level == 1 else None
        doc_node = Node(
            type=NodeType.MARKDOWN_DOCUMENT,
            name=posixpath.basename(path),
            qualified_name=path,
            path=path,
            start_line=1 if lines else None,
            end_line=len(lines) or None,
            language="markdown",
            metadata={
                "title": title,
                "links": links,
                "references": [
                    *({"kind": "link", "value": link["href"], "line": link["line"]} for link in links),
                    *inline_code,
                ],
                "code_blocks": fences,
                "heading_count": len(sections),
            },
            extractor=self.name,
        )
        result.nodes.append(doc_node)

        self._build_section_nodes(
            sections, links, inline_code, fences, path, lines, doc_node, result
        )
        self._build_task_nodes(tasks, sections, path, doc_node, result)
        return result

    # -- extraction helpers --------------------------------------------------

    def _collect_sections(self, tokens: list[Token], total_lines: int) -> list["_Section"]:
        sections: list[_Section] = []
        for i, tok in enumerate(tokens):
            if tok.type != "heading_open" or tok.map is None:
                continue
            level = int(tok.tag[1])
            title = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                title = tokens[i + 1].content.strip()
            sections.append(_Section(level=level, title=title, start_line=tok.map[0] + 1))
        # section span ends where the next heading of the same or higher level starts
        for idx, sec in enumerate(sections):
            sec.end_line = total_lines
            for nxt in sections[idx + 1:]:
                if nxt.level <= sec.level:
                    sec.end_line = nxt.start_line - 1
                    break
        # parent = nearest previous heading with a lower level
        stack: list[_Section] = []
        for sec in sections:
            while stack and stack[-1].level >= sec.level:
                stack.pop()
            sec.parent = stack[-1] if stack else None
            stack.append(sec)
        return sections

    def _collect_links(self, tokens: list[Token]) -> list[dict]:
        links: list[dict] = []
        for tok in tokens:
            if tok.type != "inline" or not tok.children:
                continue
            line = (tok.map[0] + 1) if tok.map else None
            current: dict | None = None
            for child in tok.children:
                if child.type == "link_open":
                    current = {"href": child.attrGet("href"), "text": "", "line": line}
                elif child.type == "link_close" and current is not None:
                    links.append(current)
                    current = None
                elif current is not None and child.type in ("text", "code_inline"):
                    current["text"] += child.content
        return links

    def _collect_code_blocks(self, tokens: list[Token]) -> list[dict]:
        blocks: list[dict] = []
        for tok in tokens:
            if tok.type == "fence" and tok.map is not None:
                blocks.append(
                    {
                        "language": (tok.info or "").strip() or None,
                        "start_line": tok.map[0] + 1,
                        "end_line": tok.map[1],
                        "content": tok.content,
                    }
                )
        return blocks

    def _collect_inline_code(self, tokens: list[Token]) -> list[dict]:
        """Collect backticked reference candidates without interpreting them.

        Resolution needs the complete graph and therefore happens after all
        files have been parsed and stored.
        """
        references: list[dict] = []
        for tok in tokens:
            if tok.type != "inline" or not tok.children:
                continue
            line = (tok.map[0] + 1) if tok.map else None
            for child in tok.children:
                if child.type == "code_inline" and child.content.strip():
                    references.append(
                        {"kind": "inline_code", "value": child.content.strip(), "line": line}
                    )
        return references

    def _collect_tasks(self, tokens: list[Token]) -> list[dict]:
        tasks: list[dict] = []
        for i, tok in enumerate(tokens):
            if tok.type != "list_item_open" or tok.map is None:
                continue
            text = ""
            for nxt in tokens[i + 1:]:
                if nxt.type == "inline":
                    text = nxt.content.strip()
                    break
                if nxt.type == "list_item_close":
                    break
            text = _CHECKBOX_RE.sub("", text)
            if _TASK_RE.search(text):
                tasks.append(
                    {"text": text, "start_line": tok.map[0] + 1, "end_line": tok.map[1]}
                )
        return tasks

    # -- node/edge construction -----------------------------------------------

    def _build_section_nodes(
        self,
        sections: list["_Section"],
        links: list[dict],
        inline_code: list[dict],
        fences: list[dict],
        path: str,
        lines: list[str],
        doc_node: Node,
        result: ParseResult,
    ) -> None:
        def deepest_section(line: int | None) -> _Section | None:
            best: _Section | None = None
            if line is None:
                return None
            for sec in sections:
                if sec.start_line <= line <= sec.end_line:
                    if best is None or sec.level > best.level or sec.start_line > best.start_line:
                        best = sec
            return best

        for link in links:
            sec = deepest_section(link.get("line"))
            if sec is not None:
                sec.links.append(link)
                sec.references.append(
                    {"kind": "link", "value": link["href"], "line": link["line"]}
                )
        for reference in inline_code:
            sec = deepest_section(reference.get("line"))
            if sec is not None:
                sec.references.append(reference)
        for fence in fences:
            sec = deepest_section(fence.get("start_line"))
            if sec is not None:
                sec.code_blocks.append(
                    {k: v for k, v in fence.items() if k != "content"}
                )

        seen_qnames: dict[str, int] = {}
        for sec in sections:
            chain: list[str] = []
            cursor: _Section | None = sec
            while cursor is not None:
                chain.append(cursor.title)
                cursor = cursor.parent
            qname = f"{path}#{'/'.join(reversed(chain))}"
            seen_qnames[qname] = seen_qnames.get(qname, 0) + 1
            if seen_qnames[qname] > 1:
                qname = f"{qname}@{seen_qnames[qname]}"
            sec.node = Node(
                type=NodeType.MARKDOWN_SECTION,
                name=sec.title,
                qualified_name=qname,
                path=path,
                start_line=sec.start_line,
                end_line=sec.end_line,
                language="markdown",
                metadata={
                    "level": sec.level,
                    "links": sec.links,
                    "code_blocks": sec.code_blocks,
                    "references": sec.references,
                },
                extractor=self.name,
            )
            result.nodes.append(sec.node)
            parent_node = sec.parent.node if sec.parent is not None else doc_node
            assert parent_node is not None
            result.edges.append(
                Edge(
                    type=EdgeType.CONTAINS,
                    source_node_id=parent_node.id,
                    target_node_id=sec.node.id,
                    path=path,
                    start_line=sec.start_line,
                    end_line=sec.end_line,
                    extractor=self.name,
                )
            )
            section_text = "\n".join(lines[sec.start_line - 1: sec.end_line])
            result.fts_rows.append(
                FtsRow(path=path, name=sec.title, content=section_text, node_id=sec.node.id)
            )

    def _build_task_nodes(
        self,
        tasks: list[dict],
        sections: list["_Section"],
        path: str,
        doc_node: Node,
        result: ParseResult,
    ) -> None:
        for task in tasks:
            line = task["start_line"]
            container: Node = doc_node
            best: _Section | None = None
            for sec in sections:
                if sec.start_line <= line <= sec.end_line:
                    if best is None or sec.level > best.level or sec.start_line > best.start_line:
                        best = sec
            if best is not None and best.node is not None:
                container = best.node
            task_node = Node(
                type=NodeType.TASK,
                name=task["text"][:120],
                qualified_name=f"{path}#task:{line}",
                path=path,
                start_line=line,
                end_line=task["end_line"],
                language="markdown",
                metadata={"raw": task["text"]},
                extractor=self.name,
            )
            result.nodes.append(task_node)
            result.edges.append(
                Edge(
                    type=EdgeType.CONTAINS,
                    source_node_id=container.id,
                    target_node_id=task_node.id,
                    path=path,
                    start_line=line,
                    end_line=task["end_line"],
                    extractor=self.name,
                )
            )

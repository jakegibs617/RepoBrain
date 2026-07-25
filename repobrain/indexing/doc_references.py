"""Resolve structured Markdown references against the current graph.

Parsing remains local and syntactic. This reconciliation pass runs after node
upserts, when the complete graph is available, and owns the MENTIONS edges it
derives. Rebuilding its edge family makes incremental indexing converge when
either a document or its target changes.
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from ..graph.schema import Edge, EdgeType
from ..graph.store import GraphStore


_SYMBOL_TYPES = (
    "Module", "Function", "Method", "Class", "Variable", "TestCase",
    "Route", "Endpoint", "CLICommand", "EnvVar", "ConfigKey",
)


@dataclass(frozen=True)
class _ResolvedReference:
    target_id: str
    target_name: str
    normalized: str
    confidence: float
    inferred: bool = False
    reason: str | None = None


class MarkdownMentionReconciler:
    """Connect Markdown reference candidates to unambiguous graph nodes."""

    name = "markdown_mention_reconciler"

    def reconcile(self, store: GraphStore) -> list[Edge]:
        store.delete_edges(EdgeType.MENTIONS, extractor=self.name)
        files = self._file_index(store)
        symbols = self._symbol_index(store)
        edges: list[Edge] = []

        documents = store.conn.execute(
            "SELECT id, path, metadata_json FROM nodes "
            "WHERE type = 'MarkdownDocument' ORDER BY path"
        ).fetchall()
        sections_by_path = self._sections_by_path(store)

        for document in documents:
            metadata = self._metadata(document)
            for reference in metadata.get("references", []):
                raw = str(reference.get("value") or "").strip()
                line = reference.get("line")
                kind = str(reference.get("kind") or "")
                if not raw:
                    continue
                resolved = self._resolve(
                    raw, kind, document["path"], files, symbols
                )
                if resolved is None:
                    continue
                source_id = self._source_for_line(
                    document["id"], sections_by_path.get(document["path"], []), line
                )
                edges.append(
                    Edge(
                        type=EdgeType.MENTIONS,
                        source_node_id=source_id,
                        target_node_id=resolved.target_id,
                        path=document["path"],
                        start_line=line,
                        end_line=line,
                        metadata={
                            "kind": kind,
                            "raw_value": raw,
                            "normalized_value": resolved.normalized,
                            "target_name": resolved.target_name,
                        },
                        confidence=resolved.confidence,
                        extractor=self.name,
                        is_inferred=resolved.inferred,
                        inference_reason=resolved.reason,
                    )
                )
        return edges

    @staticmethod
    def _metadata(row) -> dict:
        try:
            return json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _file_index(store: GraphStore) -> dict[str, object]:
        rows = store.conn.execute(
            "SELECT id, name, path FROM nodes WHERE type = 'File' ORDER BY path"
        ).fetchall()
        return {row["path"]: row for row in rows}

    @staticmethod
    def _symbol_index(store: GraphStore) -> dict[str, list[object]]:
        placeholders = ",".join("?" for _ in _SYMBOL_TYPES)
        rows = store.conn.execute(
            f"SELECT id, name, qualified_name, path FROM nodes "
            f"WHERE type IN ({placeholders}) ORDER BY path, start_line",
            _SYMBOL_TYPES,
        ).fetchall()
        index: dict[str, list[object]] = {}
        for row in rows:
            if row["qualified_name"]:
                index.setdefault(row["qualified_name"], []).append(row)
            index.setdefault(row["name"], []).append(row)
        return index

    @staticmethod
    def _sections_by_path(store: GraphStore) -> dict[str, list[Any]]:
        rows = store.conn.execute(
            "SELECT id, path, start_line, end_line FROM nodes "
            "WHERE type = 'MarkdownSection' ORDER BY path, start_line"
        ).fetchall()
        result: dict[str, list[Any]] = {}
        for row in rows:
            result.setdefault(row["path"], []).append(row)
        return result

    @staticmethod
    def _source_for_line(document_id: str, sections: list[Any], line: int | None) -> str:
        if line is None:
            return document_id
        candidates = [
            section for section in sections
            if section["start_line"] <= line <= section["end_line"]
        ]
        if not candidates:
            return document_id
        return max(candidates, key=lambda section: section["start_line"])["id"]

    def _resolve(
        self,
        raw: str,
        kind: str,
        doc_path: str,
        files: dict[str, Any],
        symbols: dict[str, list[Any]],
    ) -> _ResolvedReference | None:
        path = self._normalize_path(raw, doc_path)
        if path is not None and path in files:
            row = files[path]
            return _ResolvedReference(
                row["id"], row["name"], path, 1.0 if kind == "link" else 0.95
            )

        # Links are path evidence only. Their human-readable text is not a
        # reliable code symbol, and URLs must never fall through to matching.
        if kind == "link":
            return None

        matches = symbols.get(raw, [])
        unique = {row["id"]: row for row in matches}
        if len(unique) != 1:
            return None
        row = next(iter(unique.values()))
        qualified = raw == row["qualified_name"] and raw != row["name"]
        return _ResolvedReference(
            row["id"],
            row["name"],
            raw,
            0.9 if qualified else 0.75,
            inferred=not qualified,
            reason=None if qualified else "unique-symbol-name",
        )

    @staticmethod
    def _normalize_path(raw: str, doc_path: str) -> str | None:
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or raw.startswith("#"):
            return None
        candidate = unquote(parsed.path).replace("\\", "/").strip()
        if not candidate or any(char.isspace() for char in candidate):
            return None
        if candidate.startswith("/"):
            normalized = posixpath.normpath(candidate.lstrip("/"))
        else:
            normalized = posixpath.normpath(
                posixpath.join(posixpath.dirname(doc_path), candidate)
            )
        if normalized == ".." or normalized.startswith("../"):
            return None
        return normalized.removeprefix("./")

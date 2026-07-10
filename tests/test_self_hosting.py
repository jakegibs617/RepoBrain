"""Dogfood RepoBrain against its own source tree.

This is intentionally a broad integration test. Fixture tests prove individual
features; this test catches scanner, parser, reconciliation, and query failures
that only appear when they collaborate on a real repository.
"""
from pathlib import Path

from repobrain.graph.queries import code_for_docs, docs_for_code, explain_file, find_symbol
from repobrain.briefing import project_brief
from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.retrieval.keyword import search


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repobrain_understands_its_own_repository(tmp_path):
    with GraphStore(tmp_path / "self-index.sqlite") as store:
        indexer = Indexer(store)
        stats = indexer.index(PROJECT_ROOT, incremental=False)

        assert stats.files_scanned >= 50
        assert stats.nodes_created >= 500
        assert stats.edges_created >= 500
        assert stats.warnings == []

        symbols = find_symbol(store, "MarkdownMentionReconciler", exact=True)
        assert len(symbols) == 1
        assert symbols[0]["path"] == "repobrain/indexing/doc_references.py"

        hits = search(store, "MarkdownMentionReconciler", limit=3)
        assert hits and hits[0].path == "repobrain/indexing/doc_references.py"

        explanation = explain_file(store, "repobrain/indexing/doc_references.py")
        assert explanation is not None
        assert explanation["module"]["qualified_name"] == (
            "repobrain.indexing.doc_references"
        )
        assert any(
            item["module"] == "repobrain.graph.store"
            for item in explanation["imports"]["internal"]
        )
        assert any(doc["path"] == "AGENT_HANDOFF.md" for doc in explanation["docs"])

        docs = docs_for_code(store, "repobrain/indexing/doc_references.py")
        assert any(item["doc_path"] == "DECISIONS.md" for item in docs)

        code = code_for_docs(
            store, "AGENT_HANDOFF.md", heading="Current Architecture Understanding"
        )
        assert any(item["path"] == "repobrain/indexing/indexer.py" for item in code)

        brief = project_brief(PROJECT_ROOT, store, budget=1200)
        assert "local-first" in brief["text"].lower()
        assert "coding agents" in brief["text"].lower()
        assert "repobrain.cli [Module]" in brief["text"]

        unchanged = indexer.index(PROJECT_ROOT)
        assert unchanged.files_changed == 0
        assert unchanged.nodes_created == 0
        assert unchanged.edges_created == 0

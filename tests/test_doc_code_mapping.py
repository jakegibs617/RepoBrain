"""Acceptance tests for Markdown-to-code purpose mapping (Milestone 4).

These tests deliberately exercise the public query boundary rather than the
matching implementation.  A caller should be able to navigate in either
direction while retaining the Markdown section and literal reference that
justify each match.
"""
from pathlib import Path

from repobrain.graph.queries import code_for_docs, docs_for_code, explain_file


def test_docs_for_code_finds_file_reference_with_section_provenance(
    indexer, store, small_app
):
    indexer.index(small_app)

    results = docs_for_code(store, "app/services/user_service.py")

    match = next(r for r in results if r["doc_path"] == "README.md")
    assert match["section"] == "Architecture"
    assert match["doc_type"] == "MarkdownSection"
    assert match["reference"] == "app/services/user_service.py"
    assert match["target_name"] == "user_service.py"
    assert match["target_type"] == "File"
    assert match["target_path"] == "app/services/user_service.py"
    assert match["start_line"] > 0
    assert 0 < match["confidence"] <= 1


def test_docs_for_code_accepts_unique_symbol_and_normalized_file_path(
    indexer, store, small_app
):
    indexer.index(small_app)

    symbol_results = docs_for_code(store, "create_user")
    symbol_match = next(
        r
        for r in symbol_results
        if r["doc_path"] == "README.md" and r["target_type"] == "Function"
    )
    assert symbol_match["section"] == "Architecture"
    assert symbol_match["reference"] == "create_user"
    assert symbol_match["target_name"] == "create_user"
    assert symbol_match["target_path"] == "app/services/user_service.py"

    normalized = docs_for_code(store, "./app/services/user_service.py")
    assert normalized == docs_for_code(store, "app/services/user_service.py")


def test_code_for_docs_scopes_results_to_requested_heading(indexer, store, small_app):
    indexer.index(small_app)

    results = code_for_docs(store, "README.md", heading="Architecture")

    paths = {r["path"] for r in results}
    assert {
        "app/api/routes.py",
        "app/handlers/user_handler.py",
        "app/services/user_service.py",
        "app/repositories/user_repository.py",
    } <= paths
    assert "app/db/config.py" not in paths
    assert all(r["source_path"] == "README.md" for r in results)
    assert all(r["section"] == "Architecture" for r in results)
    assert all(r["reference"] and r["target_start_line"] > 0 for r in results)
    assert all(0 < r["confidence"] <= 1 for r in results)

    service = next(
        r
        for r in results
        if r["type"] == "File" and r["path"] == "app/services/user_service.py"
    )
    assert service["name"] == "user_service.py"
    assert service["qualified_name"]


def test_explain_file_includes_referencing_markdown_section(indexer, store, small_app):
    indexer.index(small_app)

    info = explain_file(store, "app/db/config.py")

    doc = next(d for d in info["docs"] if d["path"] == "README.md")
    assert doc["name"] == "Database"
    assert doc["type"] == "MarkdownSection"


def test_removed_doc_reference_removes_reverse_mapping(indexer, store, small_app):
    indexer.index(small_app)
    assert any(
        r["doc_path"] == "README.md"
        for r in docs_for_code(store, "app/repositories/user_repository.py")
    )

    readme = Path(small_app) / "README.md"
    readme.write_text(
        readme.read_text().replace(
            "`app/repositories/user_repository.py`", "the repository layer"
        )
    )
    stats = indexer.index(small_app)

    assert stats.files_changed == 1
    assert not any(
        r["doc_path"] == "README.md"
        for r in docs_for_code(store, "app/repositories/user_repository.py")
    )


def test_nonexistent_code_like_reference_does_not_create_a_mention(
    indexer, store, small_app
):
    readme = Path(small_app) / "README.md"
    readme.write_text(
        readme.read_text()
        + "\nA future design may add `app/services/audit_service.py`.\n"
    )

    indexer.index(small_app)

    assert not any(
        r["reference"] == "app/services/audit_service.py"
        for r in code_for_docs(store, "README.md")
    )


def test_unchanged_doc_gains_mapping_when_target_is_added(indexer, store, small_app):
    readme = Path(small_app) / "README.md"
    readme.write_text(readme.read_text() + "\nSee `app/services/audit_service.py`.\n")
    indexer.index(small_app)
    assert not any(
        r["reference"] == "app/services/audit_service.py"
        for r in code_for_docs(store, "README.md")
    )

    target = Path(small_app) / "app/services/audit_service.py"
    target.write_text("def record_event():\n    pass\n")
    stats = indexer.index(small_app)

    assert stats.files_changed == 1
    assert any(
        r["reference"] == "app/services/audit_service.py"
        for r in code_for_docs(store, "README.md")
    )


def test_ambiguous_symbol_name_drops_inferred_mapping(indexer, store, small_app):
    indexer.index(small_app)
    assert any(
        r["reference"] == "create_user" and r["type"] == "Function"
        for r in code_for_docs(store, "README.md")
    )

    duplicate = Path(small_app) / "app/services/legacy.py"
    duplicate.write_text("def create_user(payload):\n    return payload\n")
    indexer.index(small_app)

    assert not any(
        r["reference"] == "create_user" and r["type"] == "Function"
        for r in code_for_docs(store, "README.md")
    )

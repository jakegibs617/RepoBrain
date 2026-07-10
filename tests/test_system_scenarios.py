"""Complex, stateful product scenarios spanning multiple capabilities."""
from pathlib import Path

from repobrain.graph.queries import code_for_docs, docs_for_code


def _symbol_mentions(store, name: str) -> list[dict]:
    return [
        item for item in code_for_docs(store, "README.md", heading="Architecture")
        if item["reference"] == name and item["type"] == "Function"
    ]


def test_documented_symbol_lifecycle_converges_across_incremental_runs(
    indexer, store, small_app
):
    """A relationship must follow reality through ambiguity and a rename."""
    initial = indexer.index(small_app)
    assert initial.warnings == []
    assert len(_symbol_mentions(store, "create_user")) == 1

    # A second definition makes the bare symbol ambiguous. The unchanged
    # README must lose its inferred symbol relationship while retaining its
    # explicit file relationship.
    duplicate = Path(small_app) / "app/legacy.py"
    duplicate.write_text("def create_user(payload):\n    return payload\n")
    ambiguous = indexer.index(small_app)
    assert ambiguous.files_changed == 1
    assert _symbol_mentions(store, "create_user") == []
    assert docs_for_code(store, "app/services/user_service.py")

    # Removing the collision makes the symbol unique again. The README is
    # still unchanged, proving reconciliation responds to target-side changes.
    duplicate.unlink()
    unique_again = indexer.index(small_app)
    assert unique_again.files_deleted == 1
    assert len(_symbol_mentions(store, "create_user")) == 1

    # Rename the implementation first. Stale symbol edges must disappear even
    # though the documentation has not caught up yet.
    service = Path(small_app) / "app/services/user_service.py"
    service.write_text(service.read_text().replace("def create_user(", "def create_account("))
    renamed = indexer.index(small_app)
    assert renamed.files_changed == 1
    assert _symbol_mentions(store, "create_user") == []

    # Update the documentation in a later commit and verify the new grounded
    # relationship appears without a full rebuild.
    readme = Path(small_app) / "README.md"
    readme.write_text(readme.read_text().replace("`create_user`", "`create_account`"))
    documented = indexer.index(small_app)
    assert documented.files_changed == 1
    new_mentions = _symbol_mentions(store, "create_account")
    assert len(new_mentions) == 1
    assert new_mentions[0]["path"] == "app/services/user_service.py"
    assert new_mentions[0]["confidence"] < 1.0

    # The graph must contain no dangling edges at the end of the sequence.
    dangling = store.conn.execute(
        """
        SELECT COUNT(*) AS count FROM edges e
        LEFT JOIN nodes s ON s.id = e.source_node_id
        LEFT JOIN nodes t ON t.id = e.target_node_id
        WHERE s.id IS NULL OR t.id IS NULL
        """
    ).fetchone()["count"]
    assert dangling == 0

    unchanged = indexer.index(small_app)
    assert unchanged.files_changed == 0
    assert unchanged.nodes_created == 0
    assert unchanged.edges_created == 0

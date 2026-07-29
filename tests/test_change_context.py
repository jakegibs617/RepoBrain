import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from repobrain.change_context import GitDiffError, capture_git_changes, change_context
from repobrain.cli import main
from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.mcp_server import RepoBrainTools


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "repobrain@example.test")
    _git(root, "config", "user.name", "RepoBrain Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    _git(root, "branch", "-M", "main")


def _indexed(root: Path) -> GraphStore:
    store = GraphStore(root / ".repobrain" / "repobrain.sqlite")
    Indexer(store).index(root)
    return store


def _change_create_user(root: Path) -> None:
    path = root / "app" / "services" / "user_service.py"
    path.write_text(path.read_text().replace(
        "    return _repo.insert(payload)\n",
        "    created = _repo.insert(payload)\n    return created\n",
    ))


def test_capture_working_tree_combines_staged_unstaged_and_untracked(small_app):
    _init_repo(small_app)
    readme = small_app / "README.md"
    readme.write_text(readme.read_text() + "\nstaged note\n")
    _git(small_app, "add", "README.md")
    _change_create_user(small_app)
    service = small_app / "app" / "services" / "user_service.py"
    _git(small_app, "add", "app/services/user_service.py")
    service.write_text(service.read_text() + "\n# unstaged too\n")
    (small_app / "untracked.py").write_text("value = True\n")

    captured = capture_git_changes(small_app)
    by_path = {item["new_path"] or item["old_path"]: item for item in captured["changes"]}

    assert captured["mode"] == "working"
    assert {"README.md", "app/services/user_service.py", "untracked.py"} == set(by_path)
    assert by_path["untracked.py"]["untracked"] is True
    assert by_path["app/services/user_service.py"]["line_ranges"]["new"]
    assert not any(path.startswith(".repobrain/") for path in by_path)


def test_changed_lines_map_to_symbol_impact_tests_and_stale_docs(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        result = change_context(small_app, store)

    service = next(item for item in result["changes"]
                   if item["new_path"] == "app/services/user_service.py")
    qnames = {item["qualified_name"] for item in service["symbols"]}
    assert "app.services.user_service.create_user" in qnames
    assert "app.services.user_service.get_user" not in qnames
    assert any(item["node"]["path"] == "app/handlers/user_handler.py"
               for bucket in result["impact"].values() for item in bucket)
    assert any(item["node"]["path"] == "tests/test_users.py" for item in result["tests_to_run"])
    doc = next(item for item in result["docs_to_review"] if item["doc_path"] == "README.md")
    assert doc["section"] == "Architecture"
    assert doc["provenance"].startswith("README.md:")
    assert 0 < doc["confidence"] <= 1
    assert "unchanged document" in doc["why"]
    assert result["freshness"]["status"] == "reindexed"


def test_sqlalchemy_table_change_uses_shared_change_context_impact(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        model = small_app / "app" / "models" / "user.py"
        model.write_text(model.read_text().replace('"users"', '"accounts"'))
        result = change_context(small_app, store)

    change = next(item for item in result["changes"]
                  if item["new_path"] == "app/models/user.py")
    assert any(item["type"] == "Table" and item["name"] == "accounts"
               for item in change["symbols"])
    impacted = [item for bucket in result["impact"].values() for item in bucket]
    assert {item["node"]["name"] for item in impacted} >= {"load_by_id", "persist"}
    assert {proof["via"] for item in impacted
            for proof in item["evidence"]} >= {"READS_TABLE", "WRITES_TABLE"}


def test_changed_document_is_not_flagged_as_stale(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        readme = small_app / "README.md"
        readme.write_text(readme.read_text() + "\nReviewed with service change.\n")
        result = change_context(small_app, store)
    assert not any(item["doc_path"] == "README.md" for item in result["docs_to_review"])


def test_branch_base_diff_uses_merge_base_and_preserves_worktree(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _git(small_app, "checkout", "-qb", "feature")
        _change_create_user(small_app)
        _git(small_app, "add", "app/services/user_service.py")
        _git(small_app, "commit", "-qm", "change service")
        before = _git(small_app, "status", "--porcelain=v1")
        result = change_context(small_app, store, base="main")
        after = _git(small_app, "status", "--porcelain=v1")

    assert result["mode"] == "branch"
    assert result["base"] == "main"
    assert [item["new_path"] for item in result["changes"]] == [
        "app/services/user_service.py"
    ]
    assert before == after


def test_rename_and_delete_keep_old_paths_and_deleted_symbol_evidence(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _git(small_app, "mv", "app/db/config.py", "app/db/settings.py")
        _git(small_app, "rm", "-q", "app/services/user_service.py")
        result = change_context(small_app, store)

    renamed = next(item for item in result["changes"] if item["status"] == "renamed")
    deleted = next(item for item in result["changes"] if item["status"] == "deleted")
    assert renamed["old_path"] == "app/db/config.py"
    assert renamed["new_path"] == "app/db/settings.py"
    assert renamed["file_node"]["path"] == "app/db/settings.py"
    assert deleted["old_path"] == "app/services/user_service.py"
    assert deleted["new_path"] is None
    assert any(item["name"] == "create_user" for item in deleted["symbols"])
    assert all(item["provenance"].startswith("git:") for item in deleted["symbols"])
    assert any("deleted path app/services/user_service.py" in item for item in result["unknowns"])
    old_targets = {item["target"]["path"] for item in result["docs_to_review"]}
    assert "app/db/config.py" in old_targets
    assert "app/services/user_service.py" in old_targets
    assert all(item["inference_reason"] == "historical-exact-path-reference"
               for item in result["docs_to_review"] if item["target"]["path"] in old_targets)


def test_binary_and_unparsed_changes_are_exposed_honestly(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        (small_app / "asset.bin").write_bytes(b"\x00\x01\x02")
        result = change_context(small_app, store)
    binary = next(item for item in result["changes"] if item["new_path"] == "asset.bin")
    assert binary["binary"] is True
    assert binary["mapping_status"] == "binary_unparsed"
    assert binary["symbols"] == []


def test_untracked_text_addition_maps_its_full_symbol_range(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        (small_app / "app" / "services" / "audit.py").write_text(
            "def record_audit(event):\n    return event\n"
        )
        result = change_context(small_app, store)
    added = next(item for item in result["changes"] if item["new_path"].endswith("audit.py"))
    assert added["status"] == "added"
    assert added["untracked"] is True
    assert added["line_ranges"]["new"] == [{"start": 1, "end": 2}]
    assert [item["qualified_name"] for item in added["symbols"]] == [
        "app.services.audit.record_audit"
    ]


def test_multiple_changed_targets_deduplicate_impact_but_keep_reasons(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        repo = small_app / "app" / "repositories" / "user_repository.py"
        repo.write_text(repo.read_text() + "\n# repository change\n")
        result = change_context(small_app, store)
    records = [item for bucket in result["impact"].values() for item in bucket]
    keys = [(item["node"]["id"], evidence["via"], evidence["path"], evidence["line"])
            for item in records for evidence in item["evidence"]]
    assert len(keys) == len(set(keys))
    assert any(len(item["reason_ids"]) > 1 for item in records)
    # Every id resolves; the table is the only place a reason is spelled out.
    for item in records:
        assert all(result["reasons"][index] for index in item["reason_ids"])


def _repo_with_a_shared_dependent(root: Path) -> None:
    """Two modules a downstream module imports, so one node has two edges in."""
    (root / "alpha.py").write_text("def alpha():\n    return 1\n")
    (root / "beta.py").write_text("def beta():\n    return 2\n")
    (root / "downstream.py").write_text(
        "from alpha import alpha\nfrom beta import beta\n\n\n"
        "def combined():\n    return alpha() + beta()\n"
    )


def test_one_node_reached_by_two_edges_is_one_item_carrying_both(tmp_path):
    """A node is a fact about the blast radius; an edge is why it is in it.

    Emitting the same node once per incoming edge made the payload grow with
    the graph's edge count rather than with the size of the impacted set —
    958 rows for 497 nodes on this repository's own two-commit diff.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _repo_with_a_shared_dependent(root)
    _init_repo(root)
    with _indexed(root) as store:
        for name in ("alpha.py", "beta.py"):
            target = root / name
            target.write_text(target.read_text() + "\n# changed\n")
        result = change_context(root, store)

    records = [item for bucket in result["impact"].values() for item in bucket]
    ids = [item["node"]["id"] for item in records]
    assert len(ids) == len(set(ids)), "a node must appear at most once in the impact set"
    downstream = next(item for item in records
                      if item["node"]["path"] == "downstream.py"
                      and item["node"]["type"] == "Module")
    assert len(downstream["evidence"]) == 2
    assert {evidence["line"] for evidence in downstream["evidence"]} == {1, 2}
    assert downstream["confidence"] == max(
        evidence["confidence"] for evidence in downstream["evidence"]
    )


def test_reasons_are_spelled_out_once_however_many_items_cite_them(tmp_path):
    """The dominant cost was attribution, not facts.

    3,175 reason dicts across 958 impact items and 599 across 32 test items —
    441,930 and 82,878 characters respectively, for a set of reasons no larger
    than the diff itself.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _repo_with_a_shared_dependent(root)
    _init_repo(root)
    with _indexed(root) as store:
        for name in ("alpha.py", "beta.py"):
            target = root / name
            target.write_text(target.read_text() + "\n# changed\n")
        result = change_context(root, store)

    serialized = [json.dumps(reason, sort_keys=True) for reason in result["reasons"]]
    assert len(serialized) == len(set(serialized)), "the table must not repeat a reason"
    cited = {index for bucket in result["impact"].values()
             for item in bucket for index in item["reason_ids"]}
    cited |= {index for item in result["tests_to_run"] for index in item["reason_ids"]}
    assert cited, "items must cite the table rather than inline their reasons"
    assert cited <= set(range(len(result["reasons"])))


def test_json_output_omits_the_rendered_text_it_would_duplicate(small_app):
    """`text` is the same payload again in prose — 157,802 characters of it on
    the diff that motivated this. A caller parsing JSON has the structure."""
    _init_repo(small_app)
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    _change_create_user(small_app)

    machine = CliRunner().invoke(
        main, ["change-context", "--path", str(small_app), "--json"],
    )
    assert machine.exit_code == 0, machine.output
    assert "text" not in json.loads(machine.output)
    assert "text" not in tools.change_context()

    human = CliRunner().invoke(main, ["change-context", "--path", str(small_app)])
    assert human.exit_code == 0, human.output
    assert "RepoBrain change context" in human.output
    assert "Tests to run" in human.output


def test_ambiguous_names_do_not_cross_map_changed_line_symbols(small_app):
    _init_repo(small_app)
    legacy = small_app / "app" / "services" / "legacy.py"
    legacy.write_text("def create_user(payload):\n    return payload\n")
    _git(small_app, "add", "app/services/legacy.py")
    _git(small_app, "commit", "-qm", "add ambiguous legacy symbol")
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        result = change_context(small_app, store)
    service = next(item for item in result["changes"]
                   if item["new_path"] == "app/services/user_service.py")
    assert {item["qualified_name"] for item in service["symbols"]} == {
        "app.services.user_service.create_user"
    }


def test_cli_and_mcp_change_context_have_matching_grounded_sections(small_app):
    _init_repo(small_app)
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    _change_create_user(small_app)
    cli = CliRunner().invoke(
        main, ["change-context", "--path", str(small_app), "--json"],
    )
    assert cli.exit_code == 0, cli.output
    cli_data = json.loads(cli.output)
    mcp_data = tools.change_context()
    assert [item["new_path"] for item in cli_data["changes"]] == [
        item["new_path"] for item in mcp_data["changes"]
    ]
    assert {(item["doc_path"], item["section"]) for item in cli_data["docs_to_review"]} == {
        (item["doc_path"], item["section"]) for item in mcp_data["docs_to_review"]
    }
    assert cli_data["reasons"] == mcp_data["reasons"]
    human = CliRunner().invoke(main, ["change-context", "--path", str(small_app)])
    assert human.exit_code == 0, human.output
    assert "Tests to run" in human.output
    assert "Docs to review" in human.output


def test_freshness_failure_returns_no_change_facts(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        for number in range(11):
            (small_app / f"new-{number}.py").write_text(f"value = {number}\n")
        result = change_context(small_app, store)
    assert result["status"] == "blocked"
    assert result["freshness"]["reason"] == "threshold_exceeded"
    assert "changes" not in result
    assert "impact" not in result


def test_invalid_base_and_non_git_repo_fail_without_mutation(small_app, tmp_path):
    _init_repo(small_app)
    before = _git(small_app, "status", "--porcelain=v1")
    with pytest.raises(GitDiffError):
        capture_git_changes(small_app, base="does-not-exist")
    assert _git(small_app, "status", "--porcelain=v1") == before

    with GraphStore(tmp_path / "db.sqlite") as store:
        with pytest.raises(GitDiffError):
            change_context(tmp_path, store)


def test_self_hosting_branch_change_context_is_grounded(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    with GraphStore(tmp_path / "self.sqlite") as store:
        Indexer(store).index(project_root, incremental=False)
        result = change_context(project_root, store, base="HEAD~1")
    assert result["status"] == "ok"
    assert result["mode"] == "branch"
    assert all(item["provenance"] for change in result["changes"]
               for item in change["symbols"])

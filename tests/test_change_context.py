import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from repobrain.change_context import (
    DEFAULT_CHANGE_BUDGET,
    MINIMUM_CHANGE_BUDGET,
    GitDiffError,
    capture_git_changes,
    change_context,
    render_change_context,
)
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
    # These symbols were read out of a Git blob that no longer exists, so their
    # line numbers are historical. The record says so once rather than on every
    # symbol, and the human surface still renders it per symbol.
    assert deleted["source_revision"]
    assert all("provenance" not in item for item in deleted["symbols"])
    rendered = render_change_context(result)
    assert f"(git:{deleted['source_revision']}:app/services/user_service.py:" in rendered
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


def test_a_budget_trims_from_the_bottom_and_says_so(small_app):
    """Silent truncation is the confidently-wrong answer the gate exists to stop.

    A caller that cannot tell a complete impact set from a trimmed one will
    treat the trimmed one as complete, which is worse than being told the
    payload was too large.
    """
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        full = change_context(small_app, store, include_text=False)
        trimmed = change_context(small_app, store, budget=1200, include_text=False)

    assert trimmed["truncation"]["applied"] is True
    assert trimmed["truncation"]["within_budget"] is True
    assert trimmed["token_estimate"] <= 1200
    assert trimmed["truncation"]["budget"] == 1200
    assert sum(trimmed["truncation"]["dropped"].values()) > 0
    # The diff itself outranks everything derived from it.
    assert len(trimmed["changes"]) == len(full["changes"])
    assert len(trimmed["docs_to_review"]) < len(full["docs_to_review"])
    # A trimmed payload must not carry attribution nothing left in it cites.
    cited = {index for bucket in trimmed["impact"].values()
             for item in bucket for index in item["reason_ids"]}
    cited |= {index for item in trimmed["tests_to_run"] for index in item["reason_ids"]}
    assert cited <= set(range(len(trimmed["reasons"])))
    assert len(trimmed["reasons"]) == len(cited)


def test_a_wide_diff_keeps_the_content_its_budget_can_afford(tmp_path):
    """Trimming must stop when the payload fits, not when the lists run out.

    The reason table shrinks as the items citing it are dropped, which a
    running per-item estimate cannot see. Without periodic resynchronisation
    the estimate never falls below the table's own size, so a budget with room
    to spare empties every list — including the diff. Only a diff wide enough
    for the table to be a large share of the payload exposes it.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Eight changed cores, each with several symbols, so the reason table is
    # large relative to the payload; thirty dependents so the impact set citing
    # it is large too. Eight stays inside the auto-index threshold.
    for core in range(8):
        (root / f"configuration_core_{core}.py").write_text("\n\n".join(
            f"def read_configuration_value_{core}_{slot}():\n    return {slot}\n"
            for slot in range(4)
        ))
    imports = "\n".join(
        f"from configuration_core_{core} import read_configuration_value_{core}_0"
        for core in range(8)
    )
    for index in range(30):
        (root / f"app_{index:02d}.py").write_text(
            f"{imports}\n\n\ndef use_{index:02d}():\n    return "
            + " + ".join(f"read_configuration_value_{core}_0()" for core in range(8))
            + "\n"
        )
    _init_repo(root)
    with _indexed(root) as store:
        for core in range(8):
            # Rewrite every line so all four functions fall inside the diff
            # range and each becomes its own changed target.
            (root / f"configuration_core_{core}.py").write_text("\n\n".join(
                f"def read_configuration_value_{core}_{slot}():\n"
                f"    return {slot} + 1\n"
                for slot in range(4)
            ))
        full = change_context(root, store, include_text=False)
        reason_tokens = (len(json.dumps(full["reasons"])) + 3) // 4
        # Below the untrimmed reason table, above the payload's own floor: the
        # band in which the unresynchronised estimate could never converge.
        budget = max(MINIMUM_CHANGE_BUDGET, reason_tokens - 1)
        trimmed = change_context(root, store, budget=budget, include_text=False)

    assert reason_tokens > MINIMUM_CHANGE_BUDGET, "fixture too small to test this"
    assert trimmed["truncation"]["applied"] is True
    assert trimmed["changes"], "trimming emptied the diff a smaller payload could hold"
    # Whatever attribution survives must belong to something still present.
    cited = {index for bucket in trimmed["impact"].values()
             for item in bucket for index in item["reason_ids"]}
    cited |= {index for item in trimmed["tests_to_run"] for index in item["reason_ids"]}
    assert len(trimmed["reasons"]) == len(cited)


def _wide_committed_diff(root: Path, *, cores: int = 30, symbols: int = 8) -> None:
    """A committed diff wide enough that full-fidelity `changes` alone busts the
    default budget, with dependents and tests so there is real evidence to lose.
    """
    root.mkdir()
    (root / "tests").mkdir()
    for core in range(cores):
        (root / f"configuration_core_{core:02d}.py").write_text("\n\n".join(
            f"def read_configuration_value_{core:02d}_{slot}():\n    return {slot}\n"
            for slot in range(symbols)
        ))
    imports = "\n".join(
        f"from configuration_core_{core:02d} import read_configuration_value_{core:02d}_0"
        for core in range(cores)
    )
    for index in range(6):
        (root / f"application_surface_{index}.py").write_text(
            f"{imports}\n\n\ndef use_{index}():\n    return "
            + " + ".join(f"read_configuration_value_{core:02d}_0()" for core in range(cores))
            + "\n"
        )
        (root / "tests" / f"test_application_surface_{index}.py").write_text(
            f"from application_surface_{index} import use_{index}\n\n\n"
            f"def test_use_{index}():\n    assert use_{index}() is not None\n"
        )
    _init_repo(root)
    for core in range(cores):
        (root / f"configuration_core_{core:02d}.py").write_text("\n\n".join(
            f"def read_configuration_value_{core:02d}_{slot}():\n    return {slot} + 1\n"
            for slot in range(symbols)
        ))
    _git(root, "commit", "-aqm", "widen every core")


def test_a_wide_diff_at_the_default_budget_still_buys_evidence(tmp_path):
    """The diff is the part an agent can already get; the blast radius is not.

    At the default budget a wide diff used to spend the whole allowance on
    full-fidelity change records and emit no impact and no tests — a worse
    version of `git diff --stat`. `changes` now sheds fidelity, in reported
    stages, before evidence derived from it is given up.
    """
    root = tmp_path / "repo"
    _wide_committed_diff(root)
    with _indexed(root) as store:
        full = change_context(root, store, base="HEAD~1", include_text=False)
        result = change_context(root, store, base="HEAD~1", include_text=False,
                                budget=DEFAULT_CHANGE_BUDGET)

    assert (len(json.dumps(full["changes"])) + 3) // 4 > DEFAULT_CHANGE_BUDGET, (
        "fixture too small: full-fidelity changes must not fit the default budget"
    )
    assert result["token_estimate"] <= DEFAULT_CHANGE_BUDGET
    assert len(result["changes"]) == len(full["changes"]), "every changed path survives"
    assert sum(len(bucket) for bucket in result["impact"].values()) > 0
    assert result["tests_to_run"], "the tests to run are what a diff cannot tell you"


def test_lost_change_fidelity_is_reported_like_any_other_truncation(tmp_path):
    """A thinner change record must never pass for a complete one.

    Dropping symbols or line ranges quietly would let a caller conclude a
    changed file has no symbols in the diff, which is the confidently-wrong
    answer the whole budget report exists to prevent.
    """
    root = tmp_path / "repo"
    _wide_committed_diff(root)
    with _indexed(root) as store:
        result = change_context(root, store, base="HEAD~1", include_text=True,
                                budget=DEFAULT_CHANGE_BUDGET)

    dropped = result["truncation"]["dropped"]
    surviving_symbols = sum(len(change["symbols"]) for change in result["changes"])
    assert dropped["changes.symbols"] > 0
    assert dropped["changes.symbols"] + surviving_symbols == 30 * 8
    for label, field in (("changes.file_node", "file_node"),
                         ("changes.line_ranges", "line_ranges")):
        assert dropped.get(label, 0) == sum(
            1 for change in result["changes"] if field not in change
        )
    assert "changes.symbols: " in result["text"]


def test_a_budget_below_the_payload_floor_reports_that_it_was_not_met(small_app):
    """Trimming everything still leaves the scaffolding, and that is reportable.

    Claiming a budget was met when it was not is the same class of quiet lie as
    truncating without saying so.
    """
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        result = change_context(small_app, store, budget=MINIMUM_CHANGE_BUDGET,
                                include_text=True)

    assert result["truncation"]["applied"] is True
    assert result["truncation"]["within_budget"] is False
    assert result["token_estimate"] > MINIMUM_CHANGE_BUDGET
    assert "nothing further can be dropped" in result["text"]


def test_a_budget_under_the_minimum_is_refused_rather_than_guessed(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        with pytest.raises(ValueError, match="at least"):
            change_context(small_app, store, budget=MINIMUM_CHANGE_BUDGET - 1)


def test_a_payload_inside_its_budget_is_left_exactly_alone(small_app):
    _init_repo(small_app)
    with _indexed(small_app) as store:
        _change_create_user(small_app)
        unbudgeted = change_context(small_app, store, include_text=False)
        budgeted = change_context(small_app, store, budget=100_000, include_text=False)

    assert budgeted["truncation"]["applied"] is False
    assert budgeted["truncation"]["dropped"] == {}
    # `freshness` describes the gate pass, not the payload, and the first call
    # is the one that repaired the index.
    metadata = {"truncation", "token_estimate", "token_heuristic", "budget", "freshness"}
    assert ({key: value for key, value in budgeted.items() if key not in metadata}
            == {key: value for key, value in unbudgeted.items() if key not in metadata})


def test_the_mcp_tool_is_budgeted_and_reports_it_like_the_cli(small_app):
    """The surface an agent is most likely to call had no budget at all.

    D45 built the budget and D47 fixed what it spends on, but both stopped at
    the CLI. An MCP caller received everything with no `budget`, no
    `token_estimate` and no `truncation` — nothing to read to discover the
    payload was complete, which is the one honesty property both decisions
    treat as non-negotiable.
    """
    _init_repo(small_app)
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    _change_create_user(small_app)

    result = tools.change_context()

    assert result["budget"] == DEFAULT_CHANGE_BUDGET
    assert result["truncation"]["applied"] is False
    assert result["token_estimate"] <= DEFAULT_CHANGE_BUDGET


def test_the_mcp_caller_can_ask_for_a_tighter_budget_than_the_default(small_app):
    """A default is only defensible if the caller can override it.

    An MCP caller sits inside a live session whose context it knows more about
    than RepoBrain does, so the budget is exposed rather than fixed.
    """
    _init_repo(small_app)
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    _change_create_user(small_app)

    trimmed = tools.change_context(budget=1200)

    assert trimmed["truncation"]["applied"] is True
    assert trimmed["truncation"]["budget"] == 1200
    assert trimmed["token_estimate"] <= 1200
    assert sum(trimmed["truncation"]["dropped"].values()) > 0


def test_an_mcp_budget_under_the_minimum_is_refused_not_silently_raised(small_app):
    """The wrapper catches `GitDiffError` only; a bad budget must not land there.

    Reporting an impossible budget as a Git failure would send the caller to
    debug its repository instead of its argument.
    """
    _init_repo(small_app)
    tools = RepoBrainTools(small_app)
    tools.index_repo()

    with pytest.raises(ValueError, match="at least"):
        tools.change_context(budget=MINIMUM_CHANGE_BUDGET - 1)


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
    # Every symbol is locatable from the record that encloses it: the change
    # carries the path, the symbol carries the line.
    assert all(item["start_line"] and (change["new_path"] or change["old_path"])
               for change in result["changes"] for item in change["symbols"])

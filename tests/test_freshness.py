import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from repobrain.agent_install import (
    GIT_MARKER_START,
    GIT_RUNNER,
    install_agent,
    uninstall_agent,
)
from repobrain.briefing import project_brief
from repobrain.cli import main
from repobrain.freshness import (
    FreshnessBlockedError,
    FreshnessPolicy,
    check_freshness,
    ensure_fresh,
)
from repobrain.graph.store import GraphStore
from repobrain.indexing.doc_references import MarkdownMentionReconciler
from repobrain.indexing.indexer import EXTRACTOR_FINGERPRINT_KEY, Indexer
from repobrain.mcp_server import RepoBrainTools
from repobrain.parsers import base as parsers_base
from repobrain.parsers.base import default_registry


def _store(root: Path) -> GraphStore:
    store = GraphStore(root / ".repobrain" / "repobrain.sqlite")
    Indexer(store).index(root)
    return store


def test_auto_index_threshold_boundaries_include_files_and_bytes(small_app):
    with _store(small_app) as store:
        (small_app / "boundary.txt").write_text("12345")
        allowed = ensure_fresh(
            small_app, store,
            policy=FreshnessPolicy(max_changed_files=1, max_changed_bytes=5),
        )
        assert allowed["status"] == "reindexed"
        assert allowed["before"]["out_of_date_count"] == 1
        assert allowed["before"]["changed_bytes"] == 5

        (small_app / "too-large.txt").write_text("123456")
        blocked = ensure_fresh(
            small_app, store,
            policy=FreshnessPolicy(max_changed_files=1, max_changed_bytes=5),
        )
        assert blocked["status"] == "blocked"
        assert blocked["reason"] == "threshold_exceeded"
        assert blocked["can_query"] is False


def test_freshness_classifies_add_change_delete_and_opt_out(small_app):
    with _store(small_app) as store:
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text() + "\n# changed\n")
        (small_app / "added.py").write_text("added = True\n")
        (small_app / "README.md").unlink()
        result = ensure_fresh(small_app, store, auto_index=False)

    assert result["reason"] == "auto_index_disabled"
    assert result["before"]["added"] == ["added.py"]
    assert result["before"]["changed"] == ["app/services/user_service.py"]
    assert result["before"]["deleted"] == ["README.md"]
    assert "Run `repobrain index`" in result["message"]


def test_brief_never_returns_stale_sections_when_repair_is_blocked(small_app):
    with _store(small_app) as store:
        for number in range(11):
            (small_app / f"large-{number}.py").write_text(f"value_{number} = {number}\n")
        with pytest.raises(FreshnessBlockedError) as exc:
            project_brief(small_app, store)
    assert exc.value.result["reason"] == "threshold_exceeded"
    assert exc.value.result["can_query"] is False

    mcp = RepoBrainTools(small_app).project_brief()
    assert mcp["status"] == "blocked"
    assert "sections" not in mcp
    assert "text" not in mcp


def test_failed_auto_index_rolls_back_and_never_allows_query(monkeypatch, small_app):
    with _store(small_app) as store:
        readme = small_app / "README.md"
        old = readme.read_text()
        readme.write_text(old + "\n`brand_new_symbol`\n")
        old_doc_count = store.conn.execute(
            "SELECT count(*) FROM nodes WHERE path='README.md'"
        ).fetchone()[0]

        def fail_reconcile(self, graph_store):
            raise RuntimeError("forced reconciliation failure")

        monkeypatch.setattr(MarkdownMentionReconciler, "reconcile", fail_reconcile)
        result = ensure_fresh(small_app, store)
        current_doc_count = store.conn.execute(
            "SELECT count(*) FROM nodes WHERE path='README.md'"
        ).fetchone()[0]

    assert result["status"] == "error"
    assert result["can_query"] is False
    assert result["reason"] == "auto_index_failed"
    assert current_doc_count == old_doc_count


def test_mcp_never_executes_query_after_auto_index_failure(monkeypatch, small_app):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    target = small_app / "README.md"
    target.write_text(target.read_text() + "\nstale\n")

    def fail_index(self, root, incremental=True):
        raise RuntimeError("forced index failure")

    monkeypatch.setattr(Indexer, "index", fail_index)
    result = tools.search_project("Small Python App")

    assert result["status"] == "error"
    assert result["freshness"]["reason"] == "auto_index_failed"
    assert "results" not in result


def test_cli_and_mcp_share_stale_opt_out_semantics(small_app):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    target = small_app / "app" / "services" / "user_service.py"
    target.write_text(target.read_text() + "\n# stale parity\n")

    mcp = tools.search_project("create_user", auto_index=False)
    cli = CliRunner().invoke(
        main, ["search", "create_user", "--path", str(small_app), "--no-auto-index"],
    )

    assert mcp["status"] == "blocked"
    assert mcp["freshness"]["reason"] == "auto_index_disabled"
    assert "results" not in mcp
    assert cli.exit_code == 1
    assert "automatic repair is disabled" in cli.output


def test_mcp_small_diff_is_repaired_before_query(small_app):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    added = small_app / "fresh_symbol.py"
    added.write_text("def uniquely_fresh_symbol():\n    return True\n")

    result = tools.find_symbol("uniquely_fresh_symbol", exact=True)

    assert result["status"] == "ok"
    assert result["freshness"]["status"] == "reindexed"
    assert result["symbols"][0]["path"] == "fresh_symbol.py"


@pytest.mark.parametrize("method,args", [
    ("search_project", ("user",)),
    ("explain_project", ()),
    ("project_brief", ()),
    ("explain_file", ("README.md",)),
    ("find_symbol", ("create_user",)),
    ("trace_symbol", ("create_user",)),
    ("trace_config", ("DATABASE_URL",)),
    ("trace_data_flow", ("POST /api/users",)),
    ("impact_analysis", ("app/services/user_service.py",)),
    ("docs_for_code", ("app/services/user_service.py",)),
    ("code_for_docs", ("README.md",)),
    ("read_agent_memory", ()),
    ("verify_agent_memory", ()),
])
def test_every_read_only_mcp_tool_refuses_stale_opt_out(small_app, method, args):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    target = small_app / "README.md"
    target.write_text(target.read_text() + "\nstale\n")
    result = getattr(tools, method)(*args, auto_index=False)
    assert result["status"] == "blocked"
    assert result["freshness"]["can_query"] is False


@pytest.mark.parametrize("args", [
    ["status"],
    ["brief"],
    ["search", "user"],
    ["find-symbol", "create_user"],
    ["docs-for-code", "app/services/user_service.py"],
    ["code-for-docs", "README.md"],
    ["explain", "project"],
    ["explain", "file", "README.md"],
    ["trace", "config", "DATABASE_URL"],
    ["trace", "data-flow", "POST /api/users"],
    ["impact", "app/services/user_service.py"],
    ["report"],
    ["memory", "read"],
    ["memory", "verify"],
])
def test_every_read_only_cli_surface_exposes_freshness_opt_out(small_app, args):
    with _store(small_app):
        pass
    target = small_app / "README.md"
    target.write_text(target.read_text() + "\nstale\n")
    result = CliRunner().invoke(
        main, [*args, "--path", str(small_app), "--no-auto-index"],
    )
    assert result.exit_code == 1, result.output
    assert "automatic repair is disabled" in result.output


def test_git_hooks_coexist_are_idempotent_and_uninstall_exactly(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    hooks = tmp_path / ".git" / "hooks"
    human = hooks / "post-commit"
    human.write_text("#!/bin/sh\necho human-hook\n")
    human.chmod(0o755)
    (tmp_path / "CLAUDE.md").write_text("# Human instructions\n")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))

    first = install_agent(tmp_path, git_hooks=True)
    second = install_agent(tmp_path, git_hooks=True)

    assert first["git_hooks"]["installed"] is True
    assert second["changed"] is False
    assert human.read_text().count(GIT_MARKER_START) == 1
    assert "echo human-hook" in human.read_text()
    assert (hooks / GIT_RUNNER).stat().st_mode & 0o111

    removed = uninstall_agent(tmp_path)
    assert removed["changed"] is True
    assert "echo human-hook" in human.read_text()
    assert GIT_MARKER_START not in human.read_text()
    assert not (hooks / "post-merge").exists()
    assert not (hooks / GIT_RUNNER).exists()
    assert (tmp_path / "CLAUDE.md").read_text() == "# Human instructions\n"
    assert json.loads(settings.read_text()) == {"permissions": {"allow": ["Read"]}}


def test_freshness_reports_a_stale_index_without_indexing_or_failing(small_app):
    """`freshness` is the one read surface that neither repairs nor refuses.

    Every gated command either auto-indexes a small diff or exits non-zero on
    a large one. A status display polling on a timer can afford neither, so
    this command reports the diff and leaves the database exactly as it found
    it, whatever the size of the diff.
    """
    with _store(small_app) as store:
        before_run = store.last_index_run()["id"]
    for index in range(12):
        (small_app / f"added_{index}.py").write_text(f"VALUE = {index}\n")

    result = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(small_app)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["is_stale"] is True
    assert payload["out_of_date_count"] == 12

    # The same diff through a gated command is refused outright.
    gated = CliRunner().invoke(main, ["status", "--path", str(small_app)])
    assert gated.exit_code == 1

    with GraphStore(small_app / ".repobrain" / "repobrain.sqlite") as store:
        assert store.last_index_run()["id"] == before_run


def test_freshness_reports_current_and_missing_indexes_as_ordinary_states(small_app, tmp_path):
    """Both ends of the range exit zero, so the caller never parses an error."""
    with _store(small_app):
        pass
    current = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(small_app)],
    )

    assert current.exit_code == 0, current.output
    payload = json.loads(current.output)
    assert payload["status"] == "ok"
    assert payload["is_stale"] is False
    assert payload["out_of_date_count"] == 0
    assert payload["files"] > 0
    assert payload["last_indexed_at"]

    # A directory that was never indexed is a state to display, not a crash.
    never_indexed = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(tmp_path)],
    )

    assert never_indexed.exit_code == 0, never_indexed.output
    assert json.loads(never_indexed.output)["status"] == "unavailable"
    assert not (tmp_path / ".repobrain").exists()


def test_freshness_names_each_unavailable_state_in_a_stable_code(small_app, tmp_path):
    """The orientation command's failure states must be dispatchable, not prose.

    `freshness --json` is the first call an agent makes, and two of its
    unavailable states need different responses: a missing index means run
    `repobrain index`, a schema mismatch means this database predates the
    installed RepoBrain. Distinguishing them by matching on `reason` would make
    every agent regex an English sentence.
    """
    never_indexed = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(tmp_path)],
    )
    assert never_indexed.exit_code == 0, never_indexed.output
    assert json.loads(never_indexed.output)["reason_code"] == "no_index"

    with _store(small_app):
        pass
    database = small_app / ".repobrain" / "repobrain.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")

    mismatched = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(small_app)],
    )
    assert mismatched.exit_code == 0, mismatched.output
    payload = json.loads(mismatched.output)
    assert payload["status"] == "unavailable"
    assert payload["reason_code"] == "schema_mismatch"
    # The human-readable reason survives alongside the code, not instead of it.
    assert "schema version" in payload["reason"]


def test_freshness_reports_an_extractor_change_without_claiming_files_moved(small_app):
    """A parser upgrade is stale for a reason no file count can express."""
    with _store(small_app) as store:
        store.set_meta(EXTRACTOR_FINGERPRINT_KEY, "built-by-an-older-repobrain")
        store.commit()

    result = CliRunner().invoke(
        main, ["freshness", "--json", "--path", str(small_app)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["is_stale"] is True
    assert payload["extractor_changed"] is True
    assert payload["out_of_date_count"] == 0

    human = CliRunner().invoke(main, ["freshness", "--path", str(small_app)])
    assert human.exit_code == 0, human.output
    assert "extractor" in human.output.lower()
    assert "0 file(s)" not in human.output


def test_an_extractor_change_is_stale_although_the_working_tree_never_moved(small_app):
    """The staleness a file's stat cannot express.

    Nothing about a file's size or mtime changes when RepoBrain's own
    extraction does, so a tree-diff check reports current while the graph is
    missing facts the current parsers would produce. That is a wrong answer
    served confidently, which is the one failure the gate exists to prevent.
    """
    with _store(small_app) as store:
        assert check_freshness(small_app, store)["is_stale"] is False

        store.set_meta(EXTRACTOR_FINGERPRINT_KEY, "built-by-an-older-repobrain")
        result = check_freshness(small_app, store)

    assert result["is_stale"] is True
    assert result["extractor_changed"] is True
    # The tree genuinely did not move; the two axes stay separately legible.
    assert result["out_of_date_count"] == 0


def test_an_extractor_change_is_repaired_regardless_of_the_diff_thresholds(small_app):
    """The threshold withholds trust from a large *tree* diff, not from a
    parser upgrade.

    An extractor change invalidates every file at once, so counting it in files
    would exceed any sane threshold and leave every read surface refusing until
    the user ran `repobrain index` by hand. The tree is unchanged and
    re-extraction is deterministic, so repairing it is safe; the cost is one
    slow query after an upgrade.
    """
    with _store(small_app) as store:
        store.set_meta(EXTRACTOR_FINGERPRINT_KEY, "built-by-an-older-repobrain")

        result = ensure_fresh(
            small_app, store,
            policy=FreshnessPolicy(max_changed_files=0, max_changed_bytes=0),
        )

        assert result["status"] == "reindexed"
        assert result["can_query"] is True
        assert result["before"]["extractor_changed"] is True
        # The repair is what makes the next read trustworthy, not just a flag flip.
        assert check_freshness(small_app, store)["extractor_changed"] is False


@pytest.fixture
def edited_parser_source():
    """Change the bytes under ``repobrain/parsers/`` without changing composition.

    This is the exact case D42 was written for and could not catch on its own:
    every parser keeps its name, the registry keeps its shape, and what gets
    extracted changes anyway.
    """
    probe = Path(parsers_base.__file__).parent / "_probe_parser.py"
    probe.write_text("# a parser learning to extract something new\n", encoding="utf-8")
    try:
        yield probe
    finally:
        probe.unlink(missing_ok=True)


def test_a_parser_source_change_moves_the_fingerprint_without_a_manual_bump(
    edited_parser_source,
):
    """The bump that nothing forces is the bump that does not happen.

    Registry composition catches a parser being added or removed. Only the
    sources catch a parser whose name held still while its output changed —
    the discipline that failed and produced the stale index D42 diagnoses.
    """
    edited_parser_source.unlink()
    before = default_registry().fingerprint()

    edited_parser_source.write_text("# now it extracts more\n", encoding="utf-8")
    assert default_registry().fingerprint() != before

    edited_parser_source.unlink()
    assert default_registry().fingerprint() == before


def test_a_parser_source_change_makes_a_stored_graph_stale(small_app):
    """The fingerprint only matters if it reaches the freshness gate."""
    probe = Path(parsers_base.__file__).parent / "_probe_parser.py"
    with _store(small_app) as store:
        assert check_freshness(small_app, store)["extractor_changed"] is False
        probe.write_text("# extraction changed\n", encoding="utf-8")
        try:
            result = check_freshness(small_app, store)
        finally:
            probe.unlink(missing_ok=True)

    assert result["extractor_changed"] is True
    assert result["is_stale"] is True
    # The tree genuinely did not move; the two axes stay separately legible.
    assert result["out_of_date_count"] == 0


def test_a_large_tree_diff_still_blocks_when_the_extractor_also_moved(small_app):
    """Bypassing the threshold is scoped to the extractor axis alone.

    A parser upgrade arriving in the same run as an unreviewably large tree
    diff must not smuggle that diff past the gate.
    """
    with _store(small_app) as store:
        store.set_meta(EXTRACTOR_FINGERPRINT_KEY, "built-by-an-older-repobrain")
        for index in range(3):
            (small_app / f"added_{index}.py").write_text("x = 1\n", encoding="utf-8")

        result = ensure_fresh(
            small_app, store, policy=FreshnessPolicy(max_changed_files=2),
        )

    assert result["status"] == "blocked"
    assert result["reason"] == "threshold_exceeded"
    assert result["can_query"] is False


def test_self_hosted_query_auto_repairs_a_small_diff(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    with GraphStore(tmp_path / "self.sqlite") as store:
        Indexer(store).index(project_root, incremental=False)
        result = ensure_fresh(project_root, store)
    assert result["status"] in {"current", "reindexed"}
    assert result["can_query"] is True

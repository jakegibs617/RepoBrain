import json
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
from repobrain.freshness import FreshnessBlockedError, FreshnessPolicy, ensure_fresh
from repobrain.graph.store import GraphStore
from repobrain.indexing.doc_references import MarkdownMentionReconciler
from repobrain.indexing.indexer import Indexer
from repobrain.mcp_server import RepoBrainTools


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


def test_self_hosted_query_auto_repairs_a_small_diff(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    with GraphStore(tmp_path / "self.sqlite") as store:
        Indexer(store).index(project_root, incremental=False)
        result = ensure_fresh(project_root, store)
    assert result["status"] in {"current", "reindexed"}
    assert result["can_query"] is True

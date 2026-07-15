import json
import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from repobrain.cli import main
from repobrain.config import RepoBrainConfig
from repobrain.graph.store import GraphStore
from repobrain.history import extract_history
from repobrain.indexing.indexer import Indexer
from repobrain.memory import read_agent_memory, write_agent_memory
from repobrain.memory import verify_agent_memory
from repobrain.mcp_server import RepoBrainTools


def _indexed(root: Path) -> GraphStore:
    store = GraphStore(root / RepoBrainConfig().db_path)
    Indexer(store).index(root)
    return store


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_git(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "memory@example.test")
    _git(root, "config", "user.name", "Memory Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")


def test_memory_preserves_handoff_and_writes_structured_graph(tmp_path):
    handoff = tmp_path / "AGENT_HANDOFF.md"
    original = "# My Handoff\n\nUser-authored context.\n"
    handoff.write_text(original, encoding="utf-8")

    result = write_agent_memory(
        tmp_path,
        "Implemented refresh flow.",
        decisions=["Refresh stays server-side."],
        assumptions=["Redis is available."],
        open_questions=["Confirm expiry policy."],
        changed_files=["src/auth.py"],
        next_steps=["Add an integration test."],
    )

    assert handoff.read_text(encoding="utf-8").startswith(original)
    assert "Implemented refresh flow." in handoff.read_text(encoding="utf-8")
    assert (tmp_path / ".repobrain" / "agent_memory.md").exists()
    assert result["nodes_written"] == 5
    with GraphStore(tmp_path / RepoBrainConfig().db_path) as store:
        counts = store.counts_by_type("nodes")
        assert counts["AgentNote"] == 1
        assert counts["Decision"] == 1
        assert counts["Assumption"] == 1
        assert counts["OpenQuestion"] == 1
        assert counts["Task"] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE type='CONTAINS' AND extractor='agent_memory'"
        ).fetchone()[0] == 4

    memory = read_agent_memory(tmp_path, topic="Redis")
    assert memory["entries"][0]["assumptions"] == ["Redis is available."]
    assert read_agent_memory(tmp_path, topic="unrelated")["entries"] == []


def test_memory_cli_accepts_structured_json_file(tmp_path):
    payload = tmp_path / "session.json"
    payload.write_text(json.dumps({"summary": "Finished parser.",
                                   "next_steps": ["Run system tests."]}), encoding="utf-8")
    runner = CliRunner()
    written = runner.invoke(main, ["memory", "write", "--path", str(tmp_path),
                                  "--from-file", str(payload)])
    assert written.exit_code == 0, written.output
    read = runner.invoke(main, ["memory", "read", "--path", str(tmp_path), "--json"])
    assert read.exit_code == 0, read.output
    assert json.loads(read.output)["entries"][0]["summary"] == "Finished parser."


def test_anchor_resolution_uses_exact_paths_unique_symbols_and_reports_ambiguity(small_app):
    (small_app / "app" / "one.py").write_text("def duplicate():\n    pass\n")
    (small_app / "app" / "two.py").write_text("def duplicate():\n    pass\n")
    with _indexed(small_app):
        result = write_agent_memory(
            small_app,
            "Updated `app/services/user_service.py` and create_user; "
            "`duplicate` remains ambiguous.",
        )
    entry = read_agent_memory(small_app)["entries"][0]
    anchors = entry["anchors"]
    assert {anchor["expected"]["type"] for anchor in anchors} == {"File", "Function"}
    assert {anchor["resolution_provenance"] for anchor in anchors} == {
        "exact-path", "unique-symbol-name",
    }
    ambiguous = [item for item in entry["anchor_resolutions"]
                 if item["reference"] == "duplicate"]
    assert ambiguous[0]["status"] == "ambiguous"
    assert len(ambiguous[0]["candidates"]) == 2
    assert result["ambiguous_references"] == 1


def test_memory_write_repairs_stale_graph_before_extracting_anchors(small_app):
    with _indexed(small_app):
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        result = write_agent_memory(small_app, "Now uses `create_account`.")
    entry = read_agent_memory(small_app)["entries"][0]
    assert result["anchor_freshness"]["status"] == "reindexed"
    assert entry["anchors"][0]["expected"]["name"] == "create_account"
    assert all(anchor["expected"]["name"] != "create_user" for anchor in entry["anchors"])


def test_memory_verdicts_are_verified_drifted_invalidated_and_unanchored(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "Uses `create_user`.")
        initial = verify_agent_memory(small_app, store)
        assert initial["entries"][0]["verification"]["verdict"] == "verified"

        target = small_app / "app" / "services" / "user_service.py"
        target.write_text("\n" + target.read_text())
        Indexer(store).index(small_app)
        moved = verify_agent_memory(small_app, store)
        assert moved["entries"][0]["verification"]["verdict"] == "drifted"
        anchor = moved["entries"][0]["verification"]["anchors"][0]
        assert anchor["evidence"]["provenance"] == "stable-node-id"
        assert anchor["evidence"]["found"]["start_line"] > anchor["evidence"]["expected"]["start_line"]

        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        Indexer(store).index(small_app)
        invalid = verify_agent_memory(small_app, store)
        assert invalid["entries"][0]["verification"]["verdict"] == "invalidated"
        assert invalid["entries"][0]["verification"]["anchors"][0]["evidence"]["found"] is None

        write_agent_memory(small_app, "Team aligned on the rollout strategy.")
        final = verify_agent_memory(small_app, store)
        assert final["entries"][0]["verification"]["verdict"] == "unanchored"


def test_rename_continuity_marks_memory_drifted_with_new_path(small_app):
    _init_git(small_app)
    old = "app/services/user_service.py"
    new = "app/services/account_service.py"
    with _indexed(small_app) as store:
        extract_history(small_app, store)
        write_agent_memory(small_app, f"The service lives at `{old}`.")
        _git(small_app, "mv", old, new)
        _git(small_app, "commit", "-qm", "rename service")
        Indexer(store).index(small_app)
        extract_history(small_app, store, config=RepoBrainConfig(history_max_commits=1))
        report = verify_agent_memory(small_app, store)
        stored_original = store.conn.execute(
            "SELECT original_path FROM git_commit_files WHERE path=?", (new,)
        ).fetchone()[0]
    entry = report["entries"][0]
    assert entry["verification"]["verdict"] == "drifted"
    evidence = entry["verification"]["anchors"][0]["evidence"]
    assert evidence["provenance"] == "git-rename-continuity"
    assert evidence["found"]["path"] == new
    assert stored_original == old


def test_verification_never_mutates_markdown_or_stored_entries(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "Uses `create_user`.")
        markdown = (small_app / ".repobrain" / "agent_memory.md").read_bytes()
        stored = store.conn.execute(
            "SELECT metadata_json FROM nodes WHERE type='AgentNote'"
        ).fetchone()[0]
        first = verify_agent_memory(small_app, store)
        second = verify_agent_memory(small_app, store)
        assert first == second
        assert (small_app / ".repobrain" / "agent_memory.md").read_bytes() == markdown
        assert store.conn.execute(
            "SELECT metadata_json FROM nodes WHERE type='AgentNote'"
        ).fetchone()[0] == stored


def test_memory_verify_cli_mcp_parity_and_freshness_gate(small_app):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    tools.write_agent_memory("Uses `create_user`.")
    target = small_app / "app" / "services" / "user_service.py"
    target.write_text("\n" + target.read_text())
    runner = CliRunner()
    blocked = runner.invoke(main, ["memory", "verify", "--path", str(small_app),
                                   "--no-auto-index", "--json"])
    assert blocked.exit_code != 0
    cli = runner.invoke(main, ["memory", "verify", "--path", str(small_app), "--json"])
    assert cli.exit_code == 0, cli.output
    cli_data = json.loads(cli.output)
    mcp_data = tools.verify_agent_memory()
    assert cli_data["counts"] == mcp_data["counts"]
    assert cli_data["entries"][0]["verification"] == mcp_data["entries"][0]["verification"]
    assert "expected" in runner.invoke(
        main, ["memory", "verify", "--path", str(small_app)]
    ).output


def test_self_hosted_handoff_anchor_validates_cleanly(tmp_path):
    project = Path(__file__).resolve().parents[1]
    root = tmp_path / "self"
    root.mkdir()
    shutil.copy(project / "AGENT_HANDOFF.md", root / "AGENT_HANDOFF.md")
    shutil.copytree(project / "repobrain", root / "repobrain")
    with _indexed(root) as store:
        write_agent_memory(root, "RepoBrain handoff is `AGENT_HANDOFF.md`.")
        report = verify_agent_memory(root, store)
    assert report["counts"]["verified"] == 1
    assert report["counts"]["invalidated"] == 0

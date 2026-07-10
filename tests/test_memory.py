import json

from click.testing import CliRunner

from repobrain.cli import main
from repobrain.config import RepoBrainConfig
from repobrain.graph.store import GraphStore
from repobrain.memory import read_agent_memory, write_agent_memory


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
        assert store.counts_by_type("edges")["CONTAINS"] == 4

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


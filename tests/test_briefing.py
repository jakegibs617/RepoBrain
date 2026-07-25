import json
from pathlib import Path

from click.testing import CliRunner

from repobrain.agent_install import HOOK_COMMAND, MARKER_START, install_agent
from repobrain.briefing import project_brief
from repobrain.cli import main
from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.memory import write_agent_memory


def _indexed(root: Path) -> GraphStore:
    store = GraphStore(root / ".repobrain" / "repobrain.sqlite")
    Indexer(store).index(root)
    return store


def test_brief_budget_degrades_by_priority_without_cutting_facts(small_app):
    with _indexed(small_app) as store:
        large = project_brief(small_app, store, budget=900)
        small = project_brief(small_app, store, budget=100)

    assert large["token_estimate"] <= 900
    assert small["token_estimate"] <= 100
    assert large["sections"][0]["title"] == "Purpose"
    assert len(large["sections"]) >= len(small["sections"])
    assert [item["title"] for item in small["sections"]] == [
        item["title"] for item in large["sections"][:len(small["sections"])]
    ]
    for section in small["sections"]:
        for fact in section["facts"]:
            assert f"{fact['text']} [{fact['type']}] ({fact['source']})" in small["text"]
    purpose_facts = [fact["text"] for fact in large["sections"][0]["facts"]]
    assert "A tiny flask-style user API used as a RepoBrain test fixture." in purpose_facts
    identities = [(fact["type"], fact["text"], fact["source"])
                  for section in large["sections"] for fact in section["facts"]]
    assert len(identities) == len(set(identities))


def test_brief_skips_oversized_atomic_fact_but_keeps_useful_context(small_app):
    with _indexed(small_app) as store:
        store.conn.execute(
            "UPDATE content_fts SET content=? WHERE name='Small Python App'",
            ("One complete but deliberately oversized purpose sentence. " * 100,),
        )
        result = project_brief(small_app, store, budget=150)

    assert result["token_estimate"] <= 150
    assert "deliberately oversized" not in result["text"]
    assert any(section["title"] == "Subsystems" for section in result["sections"])


def test_brief_entrypoints_are_grounded_in_produced_route_nodes(small_app):
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=2000)
        produced_types = {
            row["type"]
            for row in store.conn.execute("SELECT DISTINCT type FROM nodes")
        }

    entrypoints = next(
        section for section in result["sections"]
        if section["title"] == "Entrypoints"
    )
    assert {fact["type"] for fact in entrypoints["facts"]} == {"Route"}
    assert {fact["text"] for fact in entrypoints["facts"]} == {
        "POST /api/users",
        "GET /api/users/<int:user_id>",
    }
    assert {"CLICommand", "Script", "Endpoint", "ADR"}.isdisjoint(produced_types)


def test_brief_detects_added_changed_and_deleted_files(small_app):
    with _indexed(small_app) as store:
        assert project_brief(small_app, store)["staleness"]["is_stale"] is False
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text() + "\n# changed\n")
        (small_app / "new.py").write_text("value = 1\n")
        (small_app / "README.md").unlink()
        stale = project_brief(small_app, store)

    assert stale["freshness"]["status"] == "reindexed"
    assert stale["freshness"]["before"]["out_of_date_count"] == 3
    assert stale["freshness"]["before"]["added"] == ["new.py"]
    assert stale["freshness"]["before"]["changed"] == ["app/services/user_service.py"]
    assert stale["freshness"]["before"]["deleted"] == ["README.md"]
    assert stale["staleness"]["is_stale"] is False
    assert stale["text"].splitlines()[1] == "Index freshness: current."


def test_brief_includes_structured_memory_with_provenance(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "Added user creation.",
                           assumptions=["SQLite remains local."],
                           open_questions=["Should writes be retried?"])
        result = project_brief(small_app, store, budget=2000)
    assert "SQLite remains local. [Assumption] (.repobrain/agent_memory.md:" in result["text"]
    assert "Should writes be retried? [OpenQuestion] (.repobrain/agent_memory.md:" in result["text"]


def test_memory_provenance_uses_the_matching_newest_session(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "First session.", assumptions=["Repeated assumption."])
        write_agent_memory(small_app, "Second\nsummary.", assumptions=["Repeated assumption."])
        result = project_brief(small_app, store, budget=2000)
    lines = (small_app / ".repobrain" / "agent_memory.md").read_text().splitlines()
    occurrences = [index for index, line in enumerate(lines, 1) if "Repeated assumption." in line]
    assumption = next(fact for section in result["sections"] if section["title"] == "Active assumptions"
                      for fact in section["facts"] if fact["text"] == "Repeated assumption.")
    assert assumption["source"] == f".repobrain/agent_memory.md:{occurrences[-1]}"


def test_brief_surfaces_invalid_memory_first_without_presenting_it_as_current(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(
            small_app, "User creation assumption.",
            assumptions=["`create_user` remains the creation entrypoint."],
        )
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        Indexer(store).index(small_app)
        result = project_brief(small_app, store, budget=300)

    assert result["token_estimate"] <= 300
    assert result["memory_verification"] == {"invalidated": 1, "drifted": 0}
    assert "Memory verification: 1 invalidated, 0 drifted" in result["text"]
    assert result["sections"][0]["title"] == "Memory requiring attention"
    assert "[MemoryInvalidated]" in result["text"]
    active = next((section for section in result["sections"]
                   if section["title"] == "Active assumptions"), {"facts": []})
    assert not any("create_user" in fact["text"] for fact in active["facts"])


def test_brief_counts_invalidated_memory_older_than_recent_display_window(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "Old anchored memory uses `create_user`.")
        for number in range(6):
            write_agent_memory(small_app, f"Unanchored planning note {number}.")
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        Indexer(store).index(small_app)
        result = project_brief(small_app, store, budget=500)
    assert result["memory_verification"]["invalidated"] == 1
    assert "Old anchored memory" in result["text"]


def test_brief_alerts_do_not_consume_current_memory_display_limit(small_app):
    with _indexed(small_app) as store:
        write_agent_memory(small_app, "Verified older memory uses `get_user`.")
        for number in range(5):
            write_agent_memory(small_app, f"Invalidated note {number} uses `create_user`.")
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        Indexer(store).index(small_app)
        result = project_brief(small_app, store, budget=1200)
    assert result["memory_verification"]["invalidated"] == 5
    assert "Verified older memory uses `get_user`." in result["text"]


def test_brief_cli_plain_and_json(small_app):
    with _indexed(small_app):
        pass
    runner = CliRunner()
    plain = runner.invoke(main, ["brief", "--path", str(small_app), "--budget", "300"])
    machine = runner.invoke(main, ["brief", "--path", str(small_app), "--json"])
    assert plain.exit_code == 0
    assert "RepoBrain project brief" in plain.output
    assert json.loads(machine.output)["status"] == "ok"


def test_install_agent_preserves_content_and_is_idempotent(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Human instructions\n")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))

    first = install_agent(tmp_path)
    second = install_agent(tmp_path)

    assert first["changed"] is True
    assert second["changed"] is False
    assert (tmp_path / "CLAUDE.md").read_text().count(MARKER_START) == 1
    assert (tmp_path / "CLAUDE.md").read_text().startswith("# Human instructions")
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Read"]}
    commands = [hook["command"] for group in data["hooks"]["SessionStart"]
                for hook in group["hooks"]]
    assert commands == [HOOK_COMMAND]
    assert commands[0].startswith("uvx --from ")
    assert " repobrain brief " in commands[0]


def test_install_agent_repairs_owned_section_without_touching_later_content(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        "# Human instructions\n\n"
        f"{MARKER_START}\n## RepoBrain session context\nold or partial text\n\n"
        "## Human workflow\n\nKeep this exactly.\n"
    )

    result = install_agent(tmp_path)
    content = claude.read_text()

    assert result["changed"] is True
    assert content.count(MARKER_START) == 1
    assert "<!-- repobrain:brief:end -->" in content
    assert "old or partial text" not in content
    assert "## Human workflow\n\nKeep this exactly." in content


def test_install_agent_never_deletes_unbounded_trailing_prose(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(f"# Human\n\n{MARKER_START}\nTrailing prose without a heading.\n")
    install_agent(tmp_path)
    content = claude.read_text()
    assert "Trailing prose without a heading." in content
    assert content.count(MARKER_START) == 1

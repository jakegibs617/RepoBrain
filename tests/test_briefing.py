import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from repobrain.agent_install import HOOK_COMMAND, MARKER_START, install_agent
from repobrain.briefing import _render, _widest_footer, project_brief
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


def test_brief_names_a_cli_by_the_command_line_that_actually_runs(click_app):
    """A project whose whole interface is a CLI must not have an empty section.

    `Entrypoints` promoted only `Route`, so every CLI-only project — this
    repository included — got a brief that could not name a single way to
    invoke it. The promoted text has to be the invocation itself: a reader who
    types what the brief printed must get the command it described.
    """
    with _indexed(click_app) as store:
        result = project_brief(click_app, store, budget=2000)

    entrypoints = next(section for section in result["sections"]
                       if section["title"] == "Entrypoints")
    assert {fact["type"] for fact in entrypoints["facts"]} == {"CLICommand"}
    assert {fact["text"] for fact in entrypoints["facts"]} == {
        "mytool start", "mytool list-items", "mytool build-all",
        "mytool db migrate-up", "mytool db reset-db",
    }
    assert all(fact["source"].startswith("mytool/cli.py:")
               for fact in entrypoints["facts"])


def test_brief_states_which_kind_of_entrypoint_each_one_is(click_app, node_app):
    """Routes and commands share a section; the type tag is what separates them.

    They answer the same question — how do I invoke this? — and have nothing
    else in common. `_fact_line` already prints each fact's type, so one
    section can hold both without a reader having to guess which is which.
    """
    shutil.copytree(node_app, click_app / "service")
    with _indexed(click_app) as store:
        result = project_brief(click_app, store, budget=4000)

    entrypoints = next(section for section in result["sections"]
                       if section["title"] == "Entrypoints")
    kinds = [fact["type"] for fact in entrypoints["facts"]]

    assert set(kinds) == {"Route", "CLICommand"}
    assert kinds == sorted(kinds, key=["Route", "CLICommand"].index), (
        "the section's own type order is its ranking"
    )
    assert "- mytool start [CLICommand] (mytool/cli.py:" in result["text"]
    assert "[Route] (service/src/routes/users.js:" in result["text"]


def test_brief_omits_entrypoints_found_only_under_test_paths(small_app, tmp_path):
    """Routes that exist only in fixtures do not describe the project.

    The brief is the first thing an agent reads; promoting a test fixture's
    routes as this repository's entrypoints actively misdescribes it. They stay
    in the graph and stay queryable — they only lose promotion.
    """
    root = tmp_path / "project"
    (root / "tests" / "fixtures").mkdir(parents=True)
    shutil.move(str(small_app), str(root / "tests" / "fixtures" / "small_python_app"))
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)
        routes = [
            row["path"]
            for row in store.conn.execute("SELECT path FROM nodes WHERE type='Route'")
        ]

    assert routes, "the fixture's routes must still be extracted into the graph"
    assert all(section["title"] != "Entrypoints" for section in result["sections"])


def test_brief_ranks_subsystems_by_how_connected_they_are(tmp_path):
    """Twelve slots spent on the shortest paths is not a relevance ranking.

    The module every other module imports is the one an agent needs named
    first, however deep it sits; a short-pathed leaf that nothing depends on
    is not a subsystem of anything.
    """
    root = tmp_path / "project"
    (root / "core" / "services").mkdir(parents=True)
    (root / "core" / "services" / "settings_registry.py").write_text(
        "def load_settings():\n    return {}\n"
    )
    for index in range(12):
        (root / f"m{index}.py").write_text(
            "from core.services.settings_registry import load_settings\n\n\n"
            f"def use_{index}():\n    return load_settings()\n"
        )
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)

    subsystems = next(section for section in result["sections"]
                      if section["title"] == "Subsystems")
    names = [fact["text"] for fact in subsystems["facts"]]
    assert names[0] == "core.services.settings_registry", names


def test_brief_omits_entrypoints_found_only_under_example_paths(small_app, tmp_path):
    """A sample application is not a description of the project that ships it.

    D43 fixed this repository only because its fixtures live under `tests/`.
    An `examples/` directory is the more common layout and is not a test, so
    the predicate that withholds promotion cannot be the one that classifies
    test files.
    """
    root = tmp_path / "project"
    root.mkdir(parents=True)
    shutil.move(str(small_app), str(root / "examples" / "small_python_app"))
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)
        routes = [
            row["path"]
            for row in store.conn.execute("SELECT path FROM nodes WHERE type='Route'")
        ]

    assert routes, "the example app's routes must still be extracted into the graph"
    assert all(section["title"] != "Entrypoints" for section in result["sections"])


def test_brief_promotes_an_env_var_the_project_actually_reads(small_app):
    """`EnvVar` was named as a Configuration type and could never appear there.

    `_node_facts` required `start_line IS NOT NULL`, but D17 keys `EnvVar` nodes
    on `("EnvVar", name, "")` so that many readers converge on one repo-global
    node — pathless and lineless by design. The eligibility rule, not the
    extractor, was what excluded them. Their location lives in
    `metadata.observation`, which is where the citation must come from.
    """
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=2000)

    configuration = next(section for section in result["sections"]
                         if section["title"] == "Configuration")
    env_vars = [fact for fact in configuration["facts"] if fact["type"] == "EnvVar"]
    assert [fact["text"] for fact in env_vars] == ["DATABASE_URL"]
    # A citation an agent can open, not a bare name and not a fabricated one.
    # `app/db/config.py:11` is the `os.environ.get("DATABASE_URL", ...)` line
    # itself, not the enclosing `def` on line 10.
    assert env_vars[0]["source"] == "app/db/config.py:11"


def test_brief_withholds_promotion_from_env_vars_only_fixtures_read(small_app, tmp_path):
    """The D43 defect, reachable again through a node type that has no path.

    The unrepresentative-path predicate keys on `nodes.path`, which is `''` for
    every `EnvVar`. Relaxing eligibility without carrying the predicate onto the
    observation path would promote fixture config as the project's own.
    """
    root = tmp_path / "project"
    (root / "tests" / "fixtures").mkdir(parents=True)
    shutil.move(str(small_app), str(root / "tests" / "fixtures" / "small_python_app"))
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)
        env_vars = [row["name"] for row
                    in store.conn.execute("SELECT name FROM nodes WHERE type='EnvVar'")]

    assert env_vars, "the fixture's env vars must still be extracted into the graph"
    promoted = [fact for section in result["sections"] for fact in section["facts"]
                if fact["type"] == "EnvVar"]
    assert promoted == []


def _configuration_project(root: Path) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pytest\n"
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"hatchling\"]\nbuild-backend = \"hatchling.build\"\n\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndescription = \"d\"\n"
        "readme = \"README.md\"\nrequires-python = \">=3.11\"\nlicense = \"MIT\"\n"
        "authors = []\nkeywords = []\nclassifiers = []\ndependencies = []\n\n"
        # A nested table implies parent keys that no line declares.
        "[tool.hatch.build.targets.wheel]\npackages = [\"demo\"]\n"
    )


def test_brief_names_the_files_that_configure_a_project_before_their_keys(tmp_path):
    """Twelve slots of packaging metadata is not a description of configuration.

    `pyproject.toml` declares more keys than the section has slots, so ordering
    by path length spent every one of them on the first file it met and never
    reached the workflow that decides how the project is built and tested. What
    an agent needs first is which files configure this project at all.
    """
    root = tmp_path / "project"
    root.mkdir()
    _configuration_project(root)
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)

    configuration = next(section for section in result["sections"]
                         if section["title"] == "Configuration")
    assert [fact["type"] for fact in configuration["facts"][:2]] == ["ConfigFile"] * 2
    assert {fact["text"] for fact in configuration["facts"][:2]} == {
        "pyproject.toml", ".github/workflows/ci.yml",
    }
    # The keys still follow; they are demoted, not withheld.
    assert any(fact["type"] == "ConfigKey" for fact in configuration["facts"])
    # A fact that can cite a line outranks one that can only cite a file.
    # SQLite sorts NULLs first, which handed the tie-break to the implied
    # parent tables of `[tool.hatch.build.targets.wheel]` — the least useful
    # keys in the file winning on a technicality.
    assert all(":" in fact["source"] for fact in configuration["facts"]), [
        fact["source"] for fact in configuration["facts"]
    ]


def test_brief_does_not_promote_configuration_that_only_illustrates_it(tmp_path):
    """A config file under a documentation directory performs nothing.

    This repository's `docs/evaluation/*-facts.json` are expected-output
    fixtures for the evaluation harness. They are `ConfigFile` nodes only
    because the structured parser claims every `.json`, and they took ten of
    the section's twelve slots.
    """
    root = tmp_path / "project"
    root.mkdir()
    _configuration_project(root)
    (root / "docs" / "evaluation").mkdir(parents=True)
    for index in range(12):
        (root / "docs" / "evaluation" / f"facts-{index}.json").write_text(
            '{"expected": {"nodes": 3}}\n'
        )
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)
        indexed_docs = store.conn.execute(
            "SELECT count(*) FROM nodes WHERE type='ConfigFile' AND path GLOB 'docs/*'"
        ).fetchone()[0]

    assert indexed_docs == 12, "the documentation's config must still be in the graph"
    promoted = [fact["source"] for section in result["sections"]
                for fact in section["facts"]]
    assert not any(source.startswith("docs/") for source in promoted), promoted


def _purpose_texts(result: dict) -> list[str]:
    section = next((item for item in result["sections"] if item["title"] == "Purpose"),
                   None)
    return [fact["text"] for fact in section["facts"]] if section else []


def test_brief_purpose_skips_a_lead_in_for_the_prose_underneath_it(tmp_path):
    """`Implemented:` is not a source-grounded fact about anything.

    `_purpose_facts` took the first non-heading paragraph, so a section whose
    body opens with a bare list lead-in promoted the lead-in. The brief's whole
    claim is that everything in it is a fact; this one said nothing at all.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text(
        "# Demo\n\nDemo is a tool for indexing things.\n\n"
        "## Current status\n\nImplemented:\n\n"
        # Items carry sentence terminators, as this repository's own README
        # does. Without the list filter the whole 3,000-character block reads
        # as a statement and is promoted in place of the lead-in.
        "- Storage. Uses SQLite in WAL mode.\n"
        "- Retrieval. Keyword search over the graph.\n"
        "- Reporting. Renders the brief.\n\n"
        "The ten-milestone MVP is complete and running offline.\n"
    )
    (root / "demo.py").write_text("def run():\n    return 1\n")
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)

    texts = _purpose_texts(result)
    assert texts, "the section must not be emptied to remove one bad fact"
    assert "Implemented:" not in texts
    # The list underneath is not a statement either; the prose after it is.
    assert "The ten-milestone MVP is complete and running offline." in texts
    assert not any(text.lstrip().startswith("-") for text in texts), texts


def test_brief_purpose_survives_a_readme_too_terse_to_contain_a_sentence(tmp_path):
    """An absent `Purpose` is a worse outcome than a thin one.

    The substance test chooses *which* paragraph to promote; it never decides
    whether the section exists. A README whose entire purpose is a fragment
    still describes the project better than silence does.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# Terse\n\nA tiny indexer\n")
    (root / "terse.py").write_text("def run():\n    return 1\n")
    with _indexed(root) as store:
        result = project_brief(root, store, budget=2000)

    assert "A tiny indexer" in _purpose_texts(result)


def test_brief_reports_the_facts_it_declined_to_add(small_app):
    """A section that loses every slot must not vanish without a word.

    At this budget `Configuration` does not shrink, it disappears: an agent
    reading the result cannot tell "this project has no configuration" from
    "the configuration facts did not fit."
    """
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=300)

    titles = [section["title"] for section in result["sections"]]
    assert "Configuration" not in titles
    truncation = result["truncation"]
    assert truncation["applied"] is True
    assert truncation["budget"] == 300
    assert truncation["dropped"]["Configuration"] == 1
    assert set(truncation["dropped"]) >= {"Configuration"}


def test_brief_text_names_what_it_left_out(small_app):
    """D47 required this of `change-context` and D53 of `impact`.

    The brief's prose is the surface an agent actually reads; a payload key it
    never renders would leave the injected session context just as silent.
    """
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=300)

    text = result["text"]
    assert "Truncated to fit 300 tokens" in text
    for title, count in result["truncation"]["dropped"].items():
        assert f"- {title}: {count} fact(s) not shown" in text


def test_brief_reporting_truncation_stays_inside_the_budget(small_app):
    """The report of the omission is itself inside the budget (D48, D53).

    Appending the footer after selection is the obvious implementation and it
    lands over budget: at 300 the untruncated brief already costs 297 tokens.
    """
    with _indexed(small_app) as store:
        for budget in (64, 100, 150, 200, 250, 280, 300, 320, 400, 900, 2000):
            result = project_brief(small_app, store, budget=budget)
            truncation = result["truncation"]
            # Going over budget is only ever honest when nothing fit at all:
            # while any fact is being shown, one more could have been declined
            # instead. `within_budget` is not an excuse the brief may give
            # itself for a payload it chose to emit.
            if result["sections"]:
                assert truncation["within_budget"] is True, budget
                assert len(result["text"]) <= budget * 4, budget
                assert result["token_estimate"] <= budget, budget
            assert truncation["applied"] == bool(truncation["dropped"]), budget
            for title, count in truncation["dropped"].items():
                assert count > 0, (budget, title)
                assert f"- {title}: {count} fact(s) not shown" in result["text"]


def test_brief_that_fits_reports_no_omissions(small_app):
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=2000)

    assert result["truncation"] == {"applied": False, "budget": 2000,
                                    "within_budget": True, "dropped": {}}
    assert "Truncated to fit" not in result["text"]


def test_brief_does_not_report_sections_that_had_nothing_to_offer(small_app):
    """An empty section is not an omission.

    This repository's own `Entrypoints` is empty because no `Route` node exists
    outside test fixtures; reporting that as dropped facts would invent a
    shortfall the graph does not have.
    """
    with _indexed(small_app) as store:
        result = project_brief(small_app, store, budget=200)

    dropped = result["truncation"]["dropped"]
    assert dropped, "budget 200 must force omissions for this test to mean anything"
    for empty in ("Memory requiring attention", "Active assumptions",
                  "Open questions", "Recent memory"):
        assert empty not in dropped


def test_widest_footer_bounds_every_report_the_omissions_could_produce():
    """The reserve is what makes one pass enough, and only if it is an upper bound.

    Selection reserves this many characters and then renders the footer it
    actually needs; a reserve that any real footer can exceed would put the
    brief over the budget it just claimed to meet.
    """
    fact = {"text": "app.services.user_service", "type": "Module",
            "source": "app/services/user_service.py:1"}
    candidates = [("Purpose", [fact] * 2), ("Subsystems", [fact] * 12),
                  ("Entrypoints", [fact] * 2), ("Configuration", [fact] * 1),
                  ("Open questions", [])]
    staleness = {"is_stale": False}
    counts = {"invalidated": 0, "drifted": 0}
    reserve = _widest_footer(300, candidates)
    bare = len(_render(staleness, [], counts))

    for limit in range(1, 13):
        dropped = {title: min(limit, len(facts))
                   for title, facts in candidates if facts}
        for within_budget in (True, False):
            truncation = {"applied": True, "budget": 300,
                          "within_budget": within_budget, "dropped": dropped}
            footer = len(_render(staleness, [], counts, truncation)) - bare
            assert footer <= reserve, (limit, within_budget, footer, reserve)


def test_brief_says_when_it_cannot_fit_even_its_own_report(small_app):
    """The floor is the header plus the report of the omissions.

    Below it nothing can be done, and a brief that claimed `within_budget` here
    would be asserting it met a budget it did not meet.
    """
    with _indexed(small_app) as store:
        for number in range(3):
            write_agent_memory(small_app, f"Note {number} uses `create_user`.")
        target = small_app / "app" / "services" / "user_service.py"
        target.write_text(target.read_text().replace("def create_user", "def create_account"))
        Indexer(store).index(small_app)
        result = project_brief(small_app, store, budget=64)

    assert result["sections"] == []
    assert result["truncation"]["within_budget"] is False
    assert result["token_estimate"] > 64
    assert "- Still over budget; nothing further can be dropped." in result["text"]
    assert "- Memory requiring attention: 3 fact(s) not shown" in result["text"]


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
    assert commands[0].startswith("uvx ")
    assert " --from " in commands[0]
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

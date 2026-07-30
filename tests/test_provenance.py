"""The identity of the code that answered, and its refusal to become a gate."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from repobrain import provenance
from repobrain.briefing import project_brief
from repobrain.cli import main
from repobrain.freshness import check_freshness, ensure_fresh
from repobrain.graph.store import GraphStore
from repobrain.indexing import indexer as indexer_module
from repobrain.indexing.indexer import Indexer
from repobrain.mcp_server import RepoBrainTools
from repobrain.parsers.base import default_registry
from repobrain.provenance import (
    CODE_FINGERPRINT_KEY,
    code_identity,
    package_source_digest,
)


def _store(root: Path) -> GraphStore:
    store = GraphStore(root / ".repobrain" / "repobrain.sqlite")
    Indexer(store).index(root)
    return store


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def edited_read_path():
    """Change a module that no parser digest covers.

    ``briefing.py`` and ``graph/queries.py`` are the read path: they decide what
    an agent is told, and D44's fingerprint is scoped to ``parsers/`` on
    purpose, so neither of them moves it. A probe module in the package root
    stands in for them without editing shipped code.
    """
    probe = Path(provenance.__file__).resolve().parent / "_probe_read_path.py"
    probe.write_text("# a read-path module learning to answer differently\n", encoding="utf-8")
    try:
        yield probe
    finally:
        probe.unlink(missing_ok=True)


def test_a_read_path_change_moves_the_code_digest_and_not_the_extractor_one(edited_read_path):
    """The blind spot M13 exists for, stated as an assertion.

    The whole read path can be arbitrarily old while every surface reports
    ``current``, sincerely: old code and the index old code built are perfectly
    self-consistent. D44's fingerprint cannot see it — that scoping is correct
    for what D44 measures and is exactly why a second identity is needed.
    """
    edited_read_path.unlink()
    code_before = package_source_digest()
    extractor_before = default_registry().fingerprint()

    edited_read_path.write_text("# now it answers differently\n", encoding="utf-8")

    assert package_source_digest() != code_before
    assert default_registry().fingerprint() == extractor_before

    edited_read_path.unlink()
    assert package_source_digest() == code_before


def test_the_same_sources_digest_identically_wherever_they_are_installed(tmp_path):
    """An identity that changes with the install path names the machine, not the code.

    Two machines running the same build must agree, or the field cannot be
    compared across the only boundary anyone wants to compare it across.
    """
    files = {"__init__.py": "x = 1\n", "graph/queries.py": "def q(): ...\n"}
    first = _tree(tmp_path / "site-packages" / "repobrain", files)
    second = _tree(tmp_path / "checkout" / "repobrain", files)

    assert package_source_digest(first) == package_source_digest(second)


def test_a_change_in_any_subpackage_moves_the_digest(tmp_path):
    """Most of the read path lives below the package root, not in it.

    ``graph/queries.py``, ``indexing/indexer.py`` and the parsers are all one
    directory down; a digest that only reads the top level would report a
    stable identity for a build whose entire query layer had been replaced.
    """
    root = _tree(tmp_path / "repobrain", {
        "__init__.py": "x = 1\n",
        "graph/queries.py": "def q(): ...\n",
        "indexing/indexer.py": "def index(): ...\n",
    })
    before = package_source_digest(root)

    (root / "graph" / "queries.py").write_text("def q(): return 2\n", encoding="utf-8")

    assert package_source_digest(root) != before


def test_renaming_a_module_is_a_change_although_the_bytes_are_identical(tmp_path):
    """Which file holds a definition is part of what the build does.

    The corpus of bytes is unchanged by a rename, so hashing content alone
    reports two different builds as the same one.
    """
    root = _tree(tmp_path / "repobrain", {
        "__init__.py": "x = 1\n",
        "briefing.py": "def brief(): ...\n",
    })
    before = package_source_digest(root)

    (root / "briefing.py").rename(root / "briefing_v2.py")

    assert package_source_digest(root) != before


def test_the_code_identity_names_where_the_code_answered_from(small_app):
    """D52's F1 symptom was a path, not a version.

    A four-day-old cached wheel answered while a checkout sat next to it, and
    nothing in the output said which one had run. The fingerprint alone does
    not tell a human that; the directory does.
    """
    with _store(small_app) as store:
        identity = code_identity(store)

    assert identity["fingerprint"] == package_source_digest()
    assert Path(identity["path"]) == Path(provenance.__file__).resolve().parent
    assert identity["changed_since_index"] is False


def test_an_index_built_before_this_field_existed_is_unknown_not_changed(small_app):
    """Absent evidence is not evidence of a mismatch.

    Every database written before this decision lacks the key. Reporting those
    as changed would light up the advisory on every pre-existing install, which
    is how an advisory gets ignored.
    """
    with _store(small_app) as store:
        store.conn.execute("DELETE FROM meta WHERE key = ?", (CODE_FINGERPRINT_KEY,))
        store.commit()

        identity = code_identity(store)

    assert identity["changed_since_index"] is None
    assert identity["fingerprint"] == package_source_digest()


def test_a_build_that_cannot_read_its_own_sources_answers_anyway(small_app, monkeypatch):
    """A label must never be able to abort the thing it labels.

    This is not hypothetical: the first implementation digested the package
    inside the index run, and an unreadable module turned an advisory field
    into an ``OSError`` out of `index`. Provenance decorates an answer, so
    losing it costs a caller the name of the build and nothing else.

    ``parsers/`` is deliberately left readable. D44's digest of that directory
    is an *input to the gate* — unreadable parsers mean staleness cannot be
    decided, which is not the same situation as a label that cannot be printed
    — and softening it is a different decision from this one.
    """
    real_read_bytes = Path.read_bytes
    parsers = provenance.PACKAGE_ROOT / "parsers"

    def unreadable_package_source(self):
        if self.is_relative_to(provenance.PACKAGE_ROOT) and not self.is_relative_to(parsers):
            raise OSError("simulated I/O error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable_package_source)

    with _store(small_app) as store:
        identity = code_identity(store)
        gate = ensure_fresh(small_app, store)

    assert identity["fingerprint"] is None
    assert identity["changed_since_index"] is None
    assert gate["can_query"] is True


def test_an_unidentifiable_build_records_no_claim_over_the_facts_it_stored(
    small_app, monkeypatch,
):
    """Absent beats a stale claim.

    Leaving the previous run's identity in place would attribute this run's
    facts to a build that did not produce them — a confidently wrong answer of
    exactly the kind the freshness gate exists to refuse.
    """
    with _store(small_app) as store:
        assert store.get_meta(CODE_FINGERPRINT_KEY)

        store.set_meta(CODE_FINGERPRINT_KEY, "an-earlier-build")
        store.commit()
        monkeypatch.setattr(
            indexer_module, "code_identity", lambda store=None: {"fingerprint": None},
        )
        Indexer(store).index(small_app, incremental=False)

        assert store.get_meta(CODE_FINGERPRINT_KEY) is None
        assert code_identity(store)["changed_since_index"] is None


def test_code_that_moved_since_the_index_is_a_label_and_never_a_gate(small_app):
    """The invariant the whole decision rests on.

    Index staleness is reported by a system that can repair it: the agent runs
    `repobrain index`. An agent cannot reinstall the code it is running inside,
    and for a read-path change re-indexing would not alter a single stored
    fact. A `status` that can never become `ok` by any action available to the
    caller is a different object from the one D40 designed, so this axis stays
    out of `is_stale` and out of `can_query`.
    """
    with _store(small_app) as store:
        store.set_meta(CODE_FINGERPRINT_KEY, "built-by-a-different-build")
        store.commit()

        staleness = check_freshness(small_app, store)
        gate = ensure_fresh(small_app, store)

    assert staleness["is_stale"] is False
    assert gate["status"] == "current"
    assert gate["can_query"] is True
    assert gate["code"]["changed_since_index"] is True


def test_a_refused_query_still_names_the_code_that_refused_it(small_app):
    """Provenance is most useful on the surface that declined to answer."""
    with _store(small_app) as store:
        for index in range(12):
            (small_app / f"added_{index}.py").write_text(f"VALUE = {index}\n")
        blocked = ensure_fresh(small_app, store, auto_index=False)

    assert blocked["can_query"] is False
    assert blocked["code"]["fingerprint"] == package_source_digest()


def test_the_status_surfaces_carry_the_identity_and_still_exit_zero(small_app):
    """`freshness` is polled on a timer, so the field must cost nothing to read."""
    with _store(small_app):
        pass

    freshness = CliRunner().invoke(main, ["freshness", "--json", "--path", str(small_app)])
    status = CliRunner().invoke(main, ["status", "--json", "--path", str(small_app)])

    assert freshness.exit_code == 0, freshness.output
    payload = json.loads(freshness.output)
    assert payload["code"]["fingerprint"] == package_source_digest()
    assert payload["code"]["changed_since_index"] is False
    # The advisory is silent while the code and the index agree.
    human = CliRunner().invoke(main, ["freshness", "--path", str(small_app)])
    assert human.output.strip() == "Index freshness: current."

    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["code"]["fingerprint"] == package_source_digest()


def test_the_brief_names_a_mismatched_build_and_still_respects_its_budget(small_app):
    """The most-read surface, and the one that cannot afford an idle line.

    The SessionStart hook injects this text into every session, so the advisory
    appears only on a mismatch — and when it does appear it is rendered inside
    the selection loop like every other line, which is what keeps D55's
    guarantee that the text measured is the text emitted.
    """
    with _store(small_app) as store:
        quiet = project_brief(small_app, store, budget=200)
        assert "different code" not in quiet["text"]

        store.set_meta(CODE_FINGERPRINT_KEY, "built-by-a-different-build")
        store.commit()
        noisy = project_brief(small_app, store, budget=200)

    assert "different code" in noisy["text"]
    assert noisy["staleness"]["code"]["changed_since_index"] is True
    assert noisy["token_estimate"] <= 200
    assert noisy["truncation"]["within_budget"] is True


def test_the_mcp_envelope_carries_the_identity_to_every_tool(small_app):
    """Agents read the envelope, not the CLI; the two must not diverge."""
    tools = RepoBrainTools(small_app)
    tools.index_repo()

    result = tools.search_project("create_user")

    assert result["status"] == "ok"
    assert result["freshness"]["code"]["fingerprint"] == package_source_digest()
    assert result["freshness"]["code"]["changed_since_index"] is False


def test_a_code_mismatch_is_visible_to_a_human_without_being_an_error(small_app):
    """An advisory nobody reads is the same as no advisory.

    `freshness` exits zero for every reportable state, so the mismatch has to
    appear in the text or it appears nowhere.
    """
    with _store(small_app) as store:
        store.set_meta(CODE_FINGERPRINT_KEY, "built-by-a-different-build")
        store.commit()

    result = CliRunner().invoke(main, ["freshness", "--path", str(small_app)])

    assert result.exit_code == 0, result.output
    assert "Index freshness: current." in result.output
    assert "code" in result.output.lower()
    assert "STALE INDEX" not in result.output

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import repobrain.history as history_module

from repobrain.change_context import change_context
from repobrain.cli import main
from repobrain.config import RepoBrainConfig
from repobrain.freshness import ensure_fresh
from repobrain.graph.queries import historical_co_change, impact_analysis
from repobrain.graph.store import GraphStore
from repobrain.history import (
    EXTRACTOR_VERSION,
    GitHistoryError,
    MAX_CO_CHANGE_CONFIDENCE,
    MAX_SUPPORTING_COMMITS_STORED,
    _clean,
    _parse_numstat_log,
    co_change_partners,
    co_change_report,
    churn_hotspots,
    churn_report,
    extract_history,
    ownership,
    ownership_report,
    probe_repository,
    refresh_history,
)
from repobrain.indexing.indexer import Indexer
from repobrain.mcp_server import RepoBrainTools


def _git(root: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


def _init_repo(root: Path, email: str = "alice@example.test", name: str = "Alice") -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", email)
    _git(root, "config", "user.name", name)


def _commit_all(root: Path, message: str, *, author: str | None = None,
                date: str | None = None) -> str:
    _git(root, "add", "-A")
    args = ["commit", "-qm", message]
    if author:
        args.append(f"--author={author}")
    _git(root, *args, date=date)
    return _git(root, "rev-parse", "HEAD")


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _pair_repo(tmp_path: Path) -> Path:
    """a+b co-change in focused commits; one broad commit; c+d change once more."""
    root = tmp_path / "pair_repo"
    root.mkdir()
    _init_repo(root)
    for name in "abcd":
        _write(root, f"{name}.py", f"{name} = 0\n")
    _commit_all(root, "initial (broad: all four files)")          # k=4, w=1/3
    for revision in (1, 2):
        _write(root, "a.py", f"a = {revision}\n")
        _write(root, "b.py", f"b = {revision}\n")
        _commit_all(root, f"focused a+b {revision}")              # k=2, w=1
    _write(root, "c.py", "c = 1\n")
    _write(root, "d.py", "d = 1\n")
    _commit_all(root, "focused c+d")                              # k=2, w=1
    return root


def _indexed(root: Path) -> GraphStore:
    store = GraphStore(root / ".repobrain" / "repobrain.sqlite")
    Indexer(store, config=RepoBrainConfig.load(root)).index(root)
    return store


def _tables(store: GraphStore) -> tuple[list, list, list]:
    commits = [tuple(r) for r in store.conn.execute(
        "SELECT * FROM git_commits ORDER BY sha")]
    files = [tuple(r) for r in store.conn.execute(
        "SELECT * FROM git_commit_files ORDER BY sha, path")]
    edges = [tuple(r) for r in store.conn.execute(
        "SELECT id, source_node_id, target_node_id, metadata_json, confidence "
        "FROM edges WHERE type='CO_CHANGED_WITH' ORDER BY id")]
    return commits, files, edges


def test_parse_numstat_log_handles_renames_binary_and_empty_commits():
    text = (
        "sha1\x01100\x01Alice\x01a@x\n"
        "1\t2\tplain.py\0-\t-\tblob.bin\0"
        "3\t0\t\0old.py\0new.py\0"
        "\0sha2\x01200\x01Bob\x01b@x"          # empty commit: bare header
        "\0sha3\x01300\x01Cee\x01c@x\n5\t1\tx.py\0"
    )
    commits = _parse_numstat_log(text)
    assert [c.sha for c in commits] == ["sha1", "sha2", "sha3"]
    assert commits[0].committed_at == 100
    assert commits[0].entries == [
        (1, 2, "plain.py", None), (None, None, "blob.bin", None),
        (3, 0, "new.py", "old.py"),
    ]
    assert commits[1].entries == []
    assert commits[2].entries == [(5, 1, "x.py", None)]


def test_non_utf8_git_identities_are_sqlite_safe_and_remain_distinct():
    first = _clean("path-\udcff")
    second = _clean("path-\udcfe")
    valid_private_use = "path-\ue000\ue1ff"
    assert first != second
    assert first.startswith("\0git-bytes:")
    assert _clean(valid_private_use) == valid_private_use
    assert all(value.encode("utf-8") for value in (first, second))


def test_valid_private_use_path_keeps_graph_and_history_identity(tmp_path):
    root = tmp_path / "private_use_path"
    root.mkdir()
    _init_repo(root)
    special = "a\ue000.py"
    for revision in range(2):
        _write(root, special, f"a = {revision}\n")
        _write(root, "b.py", f"b = {revision}\n")
        _commit_all(root, f"shared {revision}")
    with _indexed(root) as store:
        extract_history(root, store)
        assert co_change_partners(store, special)[0]["partner_path"] == "b.py"


def test_extraction_is_idempotent_and_score_math_is_exact(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        first = extract_history(root, store)
        snapshot = _tables(store)
        second = extract_history(root, store)
        assert first["head"] == second["head"]
        assert snapshot == _tables(store)
        assert first["commits"] == 4

        partners = co_change_partners(store, "a.py")
        pair = next(item for item in partners if item["partner_path"] == "b.py")
        # broad initial commit contributes 1/3; two focused commits 1.0 each
        assert pair["support"] == 3
        assert pair["weighted_support"] == pytest.approx(7 / 3, abs=1e-3)
        assert pair["score"] == pytest.approx((7 / 3) / 3, abs=1e-3)
        assert pair["confidence"] == pytest.approx(
            MAX_CO_CHANGE_CONFIDENCE * (7 / 3) / 3, abs=1e-2)
        assert pair["confidence"] < 0.6  # never static-impact confidence
        assert len(pair["supporting_commits"]) == 3

        cd = next(item for item in co_change_partners(store, "c.py")
                  if item["partner_path"] == "d.py")
        assert cd["support"] == 2
        assert cd["score"] == pytest.approx((4 / 3) / 2, abs=1e-3)
        # broad-commit discount: focused a+b couples stronger than c+d
        assert pair["score"] > cd["score"]
        # a single shared commit never earns an edge
        assert not any(item["partner_path"] in {"c.py", "d.py"} for item in partners)


def test_oversized_commits_are_recorded_but_excluded_from_co_change(tmp_path):
    root = _pair_repo(tmp_path)
    config = RepoBrainConfig(history_max_files_per_commit=3)
    with _indexed(root) as store:
        extract_history(root, store, config=config)
        excluded = store.conn.execute(
            "SELECT COUNT(*) FROM git_commits WHERE co_change_excluded='oversized'"
        ).fetchone()[0]
        assert excluded == 1  # the 4-file initial commit
        pair = next(item for item in co_change_partners(store, "a.py")
                    if item["partner_path"] == "b.py")
        assert pair["support"] == 2
        assert pair["score"] == pytest.approx(1.0)
        # c+d only co-changed once outside the oversized commit: no edge
        assert co_change_partners(store, "c.py") == []


def test_rename_continuity_attributes_old_commits_to_current_identity(tmp_path):
    root = _pair_repo(tmp_path)
    _git(root, "mv", "a.py", "renamed.py")
    _commit_all(root, "rename a.py")
    with _indexed(root) as store:
        extract_history(root, store)
        pair = next(item for item in co_change_partners(store, "renamed.py")
                    if item["partner_path"] == "b.py")
        assert pair["support"] == 3  # pre-rename commits follow the rename
        assert not store.conn.execute(
            "SELECT 1 FROM git_commit_files WHERE path='a.py' LIMIT 1").fetchone()
        original = store.conn.execute(
            "SELECT DISTINCT original_path FROM git_commit_files WHERE path='renamed.py' "
            "ORDER BY original_path").fetchall()
        assert [row[0] for row in original] == ["a.py"]
        hot = {item["path"]: item for item in churn_hotspots(store)}
        assert hot["renamed.py"]["commits"] == 4


def test_history_rewrite_removes_extractor_owned_stale_facts(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        extract_history(root, store)
        dropped = _git(root, "rev-parse", "HEAD")
        _git(root, "reset", "-q", "--hard", "HEAD~1")  # test rewrites; RepoBrain never does
        Indexer(store, config=RepoBrainConfig.load(root)).index(root)
        result = refresh_history(root, store)
        assert result["status"] == "extracted"
        assert result["commits"] == 3
        assert not store.conn.execute(
            "SELECT 1 FROM git_commits WHERE sha=?", (dropped,)).fetchone()
        # c+d's second co-change was rewritten away: its edge must be gone
        assert co_change_partners(store, "c.py") == []


def test_merges_vendor_paths_and_repobrain_state_are_excluded(tmp_path):
    root = tmp_path / "merge_repo"
    root.mkdir()
    _init_repo(root)
    _write(root, "app.py", "app = 0\n")
    _write(root, "vendor/lib.py", "lib = 0\n")
    _write(root, ".repobrain/config.json", "{}\n")
    _commit_all(root, "initial")
    _git(root, "branch", "-M", "main")
    _git(root, "checkout", "-qb", "feature")
    _write(root, "app.py", "app = 1\n")
    _write(root, "vendor/lib.py", "lib = 1\n")
    _commit_all(root, "feature work")
    _git(root, "checkout", "-q", "main")
    _write(root, "other.py", "other = 0\n")
    _commit_all(root, "mainline work")
    _git(root, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge_sha = _git(root, "rev-parse", "HEAD")
    with _indexed(root) as store:
        extract_history(root, store)
        shas = {row[0] for row in store.conn.execute("SELECT sha FROM git_commits")}
        assert merge_sha not in shas
        assert len(shas) == 3
        paths = {row[0] for row in store.conn.execute(
            "SELECT DISTINCT path FROM git_commit_files")}
        assert paths == {"app.py", "other.py"}


def test_window_is_bounded_by_configured_commit_count(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        stats = extract_history(root, store, config=RepoBrainConfig(history_max_commits=2))
        assert stats["commits"] == 2
        assert store.conn.execute("SELECT COUNT(*) FROM git_commits").fetchone()[0] == 2


def test_extractor_version_change_forces_refresh_at_unchanged_head(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        assert refresh_history(root, store)["status"] == "extracted"
        params = json.loads(store.get_meta("history_params"))
        params["extractor_version"] = EXTRACTOR_VERSION - 1
        store.set_meta("history_params", json.dumps(params, sort_keys=True))
        store.commit()

        refreshed = refresh_history(root, store)
        assert refreshed["status"] == "extracted"
        assert refreshed["head"] == _git(root, "rev-parse", "HEAD")
        assert json.loads(store.get_meta("history_params"))["extractor_version"] == EXTRACTOR_VERSION


def test_supporting_commit_metadata_is_capped_without_losing_raw_evidence(tmp_path):
    root = tmp_path / "many_shared_commits"
    root.mkdir()
    _init_repo(root)
    for revision in range(MAX_SUPPORTING_COMMITS_STORED + 2):
        _write(root, "a.py", f"a = {revision}\n")
        _write(root, "b.py", f"b = {revision}\n")
        _commit_all(root, f"shared {revision}")

    with _indexed(root) as store:
        extract_history(root, store)
        pair = co_change_partners(store, "a.py")[0]
        assert pair["support"] == MAX_SUPPORTING_COMMITS_STORED + 2
        assert len(pair["supporting_commits"]) == MAX_SUPPORTING_COMMITS_STORED
        assert pair["supporting_commits_truncated"] is True
        historical = historical_co_change(store, ["a.py"])["items"][0]
        assert historical["supporting_commits_truncated"] is True
        metadata = json.loads(store.conn.execute(
            "SELECT metadata_json FROM edges WHERE type='CO_CHANGED_WITH'"
        ).fetchone()[0])
        assert metadata["supporting_commits_truncated"] is True
        raw_support = store.conn.execute(
            "SELECT COUNT(*) FROM git_commit_files a "
            "JOIN git_commit_files b ON b.sha=a.sha "
            "WHERE a.path='a.py' AND b.path='b.py'"
        ).fetchone()[0]
        assert raw_support == pair["support"]


def test_ownership_reports_share_and_recency_with_disclaimer(tmp_path):
    root = tmp_path / "owners_repo"
    root.mkdir()
    _init_repo(root)
    _write(root, "a.py", "a = 0\n")
    _commit_all(root, "one", date="2026-01-01T10:00:00")
    _write(root, "a.py", "a = 1\n")
    _commit_all(root, "two", date="2026-01-02T10:00:00")
    _write(root, "b.py", "b = 0\n")
    _commit_all(root, "bob adds b", author="Bob <bob@example.test>",
                date="2026-01-03T10:00:00")
    with _indexed(root) as store:
        extract_history(root, store)
        repo_wide = ownership(store)
        assert [item["author_name"] for item in repo_wide] == ["Alice", "Bob"]
        assert repo_wide[0]["commits"] == 2
        assert repo_wide[0]["share"] == pytest.approx(2 / 3, abs=1e-3)
        assert repo_wide[1]["last_committed_at"].startswith("2026-01-03")
        scoped = ownership(store, path="b.py")
        assert [item["author_name"] for item in scoped] == ["Bob"]
        report = ownership_report(root, store)
        assert report["status"] == "ok"
        assert "not an authorization model" in report["disclaimer"]
        assert report["history"]["head"] == _git(root, "rev-parse", "HEAD")


def test_churn_hotspots_rank_by_commits_and_lines_for_active_files_only(tmp_path):
    root = _pair_repo(tmp_path)
    (root / "d.py").unlink()
    _commit_all(root, "drop d.py")
    with _indexed(root) as store:
        extract_history(root, store)
        items = churn_hotspots(store)
        assert items[0]["path"] in {"a.py", "b.py"}
        assert items[0]["commits"] == 3
        assert items[0]["additions"] >= 2
        assert all(item["path"] != "d.py" for item in items)  # deleted: not actionable
        report = churn_report(root, store, limit=2)
        assert report["status"] == "ok" and len(report["items"]) == 2
        assert "Churn hotspots" in report["text"]


def test_impact_analysis_blends_history_as_separate_labeled_bucket(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        extract_history(root, store)
        result = impact_analysis(store, "a.py")
        bucket = result["historical_evidence"]
        assert "not a static dependency" in bucket["explanation"]
        assert bucket["provenance"]["head"] == _git(root, "rev-parse", "HEAD")
        partner = next(item for item in bucket["items"]
                       if item["node"]["path"] == "b.py")
        assert partner["via"] == "CO_CHANGED_WITH"
        assert partner["evidence"] == "git-history"
        assert partner["supporting_commits"]
        assert partner["confidence"] < 0.6
        static_ids = {item["node"]["id"]
                      for key in ("high_confidence", "medium_confidence", "low_confidence")
                      for item in result[key]}
        assert partner["node"]["id"] not in static_ids  # separate bucket, not blended in
        excluded = impact_analysis(store, "a.py", include_history=False)
        assert excluded["historical_evidence"]["items"] == []


def test_change_context_flags_unchanged_historical_partners(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        extract_history(root, store)
        _write(root, "a.py", "a = 99\n")
        result = change_context(root, store)
        historical = result["historical_impact"]
        assert historical["status"] == "ok"
        partner = next(item for item in historical["items"]
                       if item["node"]["path"] == "b.py")
        assert "unchanged here" in partner["why"]
        assert "Historical co-change (heuristic)" in result["text"]
        assert "b.py co-changed with a.py" in result["text"]

        _write(root, "b.py", "b = 99\n")  # partner changes too: no longer flagged
        both = change_context(root, store)
        assert all(item["node"]["path"] != "b.py"
                   for item in both["historical_impact"]["items"])


def test_branch_context_does_not_claim_committed_rename_history_is_unavailable(tmp_path):
    root = _pair_repo(tmp_path)
    _git(root, "branch", "base")
    _git(root, "mv", "a.py", "renamed.py")
    _commit_all(root, "rename a")
    with _indexed(root) as store:
        result = change_context(root, store, base="base")
        historical = result["historical_impact"]
        assert any(item["node"]["path"] == "b.py" for item in historical["items"])
        assert historical["notes"] == []


def test_freshness_gate_reextracts_on_new_commits_and_fails_closed_when_disabled(tmp_path):
    root = _pair_repo(tmp_path)
    with _indexed(root) as store:
        assert refresh_history(root, store)["status"] == "extracted"
        assert ensure_fresh(root, store)["history"]["status"] == "current"
        # a pure commit moves HEAD without touching the working tree
        _git(root, "commit", "-q", "--allow-empty", "-m", "empty")
        gate = ensure_fresh(root, store)
        assert gate["status"] == "current" and gate["history"]["status"] == "extracted"
        _git(root, "commit", "-q", "--allow-empty", "-m", "empty2")
        blocked = co_change_report(root, store, "a.py", auto_index=False)
        assert blocked["status"] == "history_stale"
        assert "repobrain index" in blocked["message"]
        repaired = co_change_report(root, store, "a.py")
        assert repaired["status"] == "ok"
        assert repaired["history"]["status"] == "extracted"


def test_refresh_retries_a_transient_extraction_failure(tmp_path, monkeypatch):
    root = _pair_repo(tmp_path)
    original = history_module._read_window
    calls = 0

    def flaky_read_window(path, limit):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("transient git timeout")
        return original(path, limit)

    monkeypatch.setattr(history_module, "_read_window", flaky_read_window)
    with _indexed(root) as store:
        first = refresh_history(root, store)
        second = refresh_history(root, store)
        assert first == {"status": "error", "error": "transient git timeout"}
        assert second["status"] == "extracted"
        assert calls == 2


def test_history_parameter_io_failure_does_not_block_static_freshness(tmp_path, monkeypatch):
    root = _pair_repo(tmp_path)

    def unreadable_params(_root, _config):
        raise OSError("ignore file unreadable")

    monkeypatch.setattr(history_module, "_history_params", unreadable_params)
    with _indexed(root) as store:
        gate = ensure_fresh(root, store)
        assert gate["can_query"] is True
        assert gate["history"] == {
            "status": "error", "error": "ignore file unreadable",
        }


def test_non_git_and_shallow_repositories_fail_honestly(tmp_path, small_app):
    with _indexed(small_app) as store:  # small_app fixture is not a Git repo
        assert probe_repository(small_app)["reason"] == "not_a_git_repository"
        gate = ensure_fresh(small_app, store)
        assert gate["can_query"] is True  # static queries unaffected
        assert gate["history"] == {"status": "unavailable",
                                   "reason": "not_a_git_repository"}
        report = co_change_report(small_app, store, "app/services/user_service.py")
        assert report["status"] == "history_unavailable"
        with pytest.raises(GitHistoryError):
            extract_history(small_app, store)

    source = _pair_repo(tmp_path)
    shallow = tmp_path / "shallow_clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{source}", str(shallow))
    probe = probe_repository(shallow)
    assert probe == {"available": False, "reason": "shallow_repository", "head": None}
    with _indexed(shallow) as store:
        report = churn_report(shallow, store)
        assert report["status"] == "history_unavailable"
        assert "shallow_repository" in report["message"]

    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert probe_repository(bare) == {
        "available": False, "reason": "bare_repository", "head": None,
    }


def test_extraction_never_mutates_git_state(tmp_path):
    root = _pair_repo(tmp_path)
    _write(root, "a.py", "a = 7\n")  # dirty working tree must survive untouched
    with _indexed(root) as store:
        before = (_git(root, "status", "--porcelain=v1"), _git(root, "rev-parse", "HEAD"))
        extract_history(root, store)
        refresh_history(root, store)
    assert (_git(root, "status", "--porcelain=v1"),
            _git(root, "rev-parse", "HEAD")) == before


def test_cli_and_mcp_history_surfaces_match(tmp_path):
    root = _pair_repo(tmp_path)
    tools = RepoBrainTools(root)
    tools.index_repo()
    runner = CliRunner()
    cli_pairs = runner.invoke(
        main, ["history", "co-change", "a.py", "--path", str(root), "--json"])
    assert cli_pairs.exit_code == 0, cli_pairs.output
    cli_data = json.loads(cli_pairs.output)
    mcp_data = tools.co_change("a.py")
    assert ([item["partner_path"] for item in cli_data["items"]]
            == [item["partner_path"] for item in mcp_data["items"]])
    assert cli_data["history"]["head"] == mcp_data["history"]["head"]

    cli_hot = runner.invoke(main, ["history", "hotspots", "--path", str(root), "--json"])
    assert cli_hot.exit_code == 0, cli_hot.output
    assert ([item["path"] for item in json.loads(cli_hot.output)["items"]]
            == [item["path"] for item in tools.churn_hotspots()["items"]])

    cli_owners = runner.invoke(main, ["history", "owners", "--path", str(root), "--json"])
    assert cli_owners.exit_code == 0, cli_owners.output
    assert ([item["author_email"] for item in json.loads(cli_owners.output)["items"]]
            == [item["author_email"] for item in tools.ownership()["items"]])

    text = runner.invoke(main, ["history", "co-change", "a.py", "--path", str(root)])
    assert text.exit_code == 0
    assert "Co-change partners of a.py" in text.output


def test_index_cli_reports_explicit_history_phase(tmp_path):
    root = _pair_repo(tmp_path)
    result = CliRunner().invoke(main, ["index", str(root)])
    assert result.exit_code == 0, result.output
    assert "history       : extracted 4 commit(s)" in result.output
    skipped = CliRunner().invoke(main, ["index", str(root), "--no-history"])
    assert skipped.exit_code == 0
    assert "history" not in skipped.output


def test_historical_co_change_query_handles_missing_history(tmp_path, store):
    assert historical_co_change(store, ["a.py"]) == {
        "items": [],
        "explanation": historical_co_change(store, [])["explanation"],
        "provenance": {"head": None, "extracted_at": None, "window": {},
                       "commit_count": 0, "extractor": "git-history"},
    }


def test_self_hosting_history_extraction_is_grounded(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    if not probe_repository(project_root)["available"]:
        pytest.skip("project checkout has no full git history")
    with GraphStore(tmp_path / "self.sqlite") as store:
        Indexer(store).index(project_root, incremental=False)
        stats = extract_history(project_root, store)
        assert stats["status"] == "extracted" and stats["commits"] > 0
        assert stats["head"] == _git(project_root, "rev-parse", "HEAD")
        rows = store.conn.execute(
            "SELECT metadata_json, confidence FROM edges WHERE type='CO_CHANGED_WITH'"
        ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata_json"])
            assert meta["support"] >= 2
            assert meta["supporting_commits"]
            assert row["confidence"] < 0.6

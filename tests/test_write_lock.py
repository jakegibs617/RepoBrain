"""Concurrent index runs are a shipped configuration, not an exotic race.

`install-agent --git-hooks` writes post-commit and post-merge dispatchers, and
the SessionStart hook indexes too, so a commit landing while a session starts
runs two indexers against one database. SQLite's lock keeps the graph correct;
these tests cover what the *operator* sees when it engages.
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest
from click.testing import CliRunner

from repobrain.cli import main
from repobrain.graph.store import LOCK_BUSY_EXIT_CODE


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    runner = CliRunner()
    assert runner.invoke(main, ["index", str(root)]).exit_code == 0
    return root


def _hold_write_lock(db_path):
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN EXCLUSIVE")
    return blocker


def test_blocked_index_reports_a_retryable_refusal_and_leaves_the_graph_alone(repo):
    # One blocked run carries both assertions: each costs a real busy_timeout
    # wait, and the message and the graph are two facts about the same event.
    db = repo / ".repobrain" / "repobrain.sqlite"
    with sqlite3.connect(str(db)) as probe:
        before = probe.execute("SELECT count(*) FROM nodes").fetchone()[0]
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    blocker = _hold_write_lock(db)
    try:
        result = CliRunner().invoke(main, ["index", str(repo)])
    finally:
        blocker.close()

    assert result.exit_code == LOCK_BUSY_EXIT_CODE
    assert "Traceback" not in result.output
    assert "OperationalError" not in result.output
    # Names the cause and what to do, in the same voice as the staleness refusal.
    assert "lock" in result.output.lower()
    assert "retry" in result.output.lower()

    with sqlite3.connect(str(db)) as probe:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert probe.execute("SELECT count(*) FROM nodes").fetchone()[0] == before


def test_lock_exit_code_is_distinguishable_from_ordinary_failure(repo):
    ordinary = CliRunner().invoke(main, ["explain", "file", "absent.py",
                                        "--path", str(repo)])

    assert ordinary.exit_code == 1
    assert LOCK_BUSY_EXIT_CODE != 1

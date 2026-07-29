import json
from pathlib import Path
import subprocess
import sys

import pytest

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.testing.accuracy import (
    collect_fact_keys,
    evaluate_corpus,
    evaluate_facts,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "evaluation" / "corpus.json"


def _repo(tmp_path, name, body):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "service.py").write_text(body, encoding="utf-8")
    return repo


def _spec(tmp_path, name, expected, forbidden):
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps({"expected": expected, "forbidden": forbidden}), encoding="utf-8"
    )
    return path


def test_labeled_accuracy_scores_expected_and_forbidden_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text(
        "def create_user(name):\n"
        "    return name\n",
        encoding="utf-8",
    )
    with GraphStore(tmp_path / "graph.sqlite") as store:
        Indexer(store).index(repo)
        facts = collect_fact_keys(store)
        function = next(
            fact for fact in facts
            if fact.startswith("node|Function:service.create_user@service.py")
        )
        missing = "node|Function:service.delete_user@service.py"
        result = evaluate_facts(
            store,
            expected=[function, missing],
            forbidden=[function.replace("create_user", "invented_user")],
        )

    assert result.true_positives == 1
    assert result.false_negatives == (missing,)
    assert result.false_positives == ()
    assert result.precision == 1.0
    assert result.recall == 0.5
    assert result.passed is False


def test_accuracy_spec_rejects_conflicting_labels(tmp_path):
    with GraphStore(tmp_path / "graph.sqlite") as store:
        with pytest.raises(ValueError, match="both expected and forbidden"):
            evaluate_facts(store, expected=["node|x"], forbidden=["node|x"])


def test_evaluation_script_is_a_runnable_release_gate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text("def current():\n    return 1\n", encoding="utf-8")
    specification = tmp_path / "accuracy.json"
    specification.write_text(
        json.dumps(
            {
                "expected": ["node|Function:service.current@service.py"],
                "forbidden": ["node|Function:service.stale@service.py"],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_extraction.py",
            str(repo),
            str(specification),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["passed"] is True
    assert payload["precision"] == 1.0
    assert payload["recall"] == 1.0


def _manifest(tmp_path, entries):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_corpus_mode_scores_every_entry_and_aggregates_totals(tmp_path):
    _repo(tmp_path, "one", "def alpha():\n    return 1\n")
    _repo(tmp_path, "two", "def beta():\n    return 2\n")
    manifest = _manifest(tmp_path, [
        {
            "name": "one",
            "repository": "one",
            "spec": "one.json",
        },
        {
            "name": "two",
            "repository": "two",
            "spec": "two.json",
        },
    ])
    _spec(tmp_path, "one",
          ["node|Function:service.alpha@service.py"],
          ["node|Function:service.gamma@service.py"])
    _spec(tmp_path, "two",
          ["node|Function:service.beta@service.py"],
          [])

    report = evaluate_corpus(manifest)

    assert report["passed"] is True
    assert [entry["name"] for entry in report["entries"]] == ["one", "two"]
    assert report["totals"]["expected_count"] == 2
    assert report["totals"]["forbidden_count"] == 1
    assert report["totals"]["precision"] == 1.0
    assert report["totals"]["recall"] == 1.0


def test_corpus_mode_fails_the_whole_run_when_one_entry_regresses(tmp_path):
    _repo(tmp_path, "one", "def alpha():\n    return 1\n")
    _repo(tmp_path, "two", "def beta():\n    return 2\n")
    manifest = _manifest(tmp_path, [
        {"name": "one", "repository": "one", "spec": "one.json"},
        {"name": "two", "repository": "two", "spec": "two.json"},
    ])
    _spec(tmp_path, "one", ["node|Function:service.alpha@service.py"], [])
    _spec(tmp_path, "two", ["node|Function:service.vanished@service.py"], [])

    report = evaluate_corpus(manifest)

    assert report["passed"] is False
    assert report["entries"][0]["passed"] is True
    failing = report["entries"][1]
    assert failing["passed"] is False
    assert failing["false_negatives"] == ["node|Function:service.vanished@service.py"]
    assert report["totals"]["recall"] == 0.5


def test_corpus_paths_resolve_relative_to_the_manifest(tmp_path):
    nested = tmp_path / "docs" / "evaluation"
    nested.mkdir(parents=True)
    _repo(tmp_path, "one", "def alpha():\n    return 1\n")
    manifest = nested / "corpus.json"
    manifest.write_text(
        json.dumps({"entries": [
            {"name": "one", "repository": "../../one", "spec": "one-facts.json"}
        ]}),
        encoding="utf-8",
    )
    (nested / "one-facts.json").write_text(
        json.dumps({"expected": ["node|Function:service.alpha@service.py"],
                    "forbidden": []}),
        encoding="utf-8",
    )

    assert evaluate_corpus(manifest)["passed"] is True


def test_corpus_script_runs_the_committed_corpus_as_a_release_gate():
    completed = subprocess.run(
        [sys.executable, "scripts/evaluate_extraction.py", "--corpus", str(CORPUS)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"] is True
    assert report["totals"]["expected_count"] >= 40
    assert report["totals"]["forbidden_count"] >= 15


def test_committed_corpus_covers_every_language_with_a_parser():
    entries = json.loads(CORPUS.read_text(encoding="utf-8"))["entries"]
    covered = {language for entry in entries for language in entry["languages"]}

    assert {"python", "javascript", "typescript", "go", "java", "ruby", "php"} <= covered

import json
import subprocess
import sys

import pytest

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.testing.accuracy import collect_fact_keys, evaluate_facts


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

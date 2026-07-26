import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]

PAGE = (
    '<div class="metric-grid">\n'
    '  <article class="metric primary" data-quality-metric="tests" data-value="10">'
    "<p>Test suite</p><strong>10</strong><small>collected</small></article>\n"
    '  <article class="metric" data-quality-metric="files" data-value="20">'
    "<p>Self-index</p><strong>20</strong><small>real project files</small></article>\n"
    '  <article class="metric" data-quality-metric="graph" data-value="30">'
    "<p>Knowledge graph</p><strong>30</strong>"
    "<small>11 nodes + 19 edges</small></article>\n"
    '  <article class="metric"><p>Index warnings</p><strong>0</strong></article>\n'
    "</div>\n"
)
HANDOFF = "intro\n- 10 tests are collected; the full suite passes\nrest\n"


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_setup_metrics", ROOT / "scripts" / "verify_setup_metrics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_rewrites_every_copy_of_a_published_number(verifier):
    page, handoff = verifier.sync_metrics(
        PAGE, HANDOFF, {"tests": 1234, "files": 150, "graph": 6354}, nodes=2015, edges=4339
    )

    assert 'data-quality-metric="tests" data-value="1234"' in page
    assert "<strong>1,234</strong>" in page
    assert 'data-quality-metric="files" data-value="150"' in page
    assert "<strong>150</strong>" in page
    assert 'data-quality-metric="graph" data-value="6354"' in page
    assert "<strong>6,354</strong>" in page
    assert "<small>2,015 nodes + 4,339 edges</small>" in page
    assert "- 1234 tests are collected;" in handoff


def test_sync_leaves_unlabeled_metrics_and_surrounding_text_alone(verifier):
    page, handoff = verifier.sync_metrics(
        PAGE, HANDOFF, {"tests": 11, "files": 21, "graph": 31}, nodes=12, edges=19
    )

    assert '<article class="metric"><p>Index warnings</p><strong>0</strong></article>' in page
    assert page.startswith('<div class="metric-grid">')
    assert handoff.startswith("intro\n") and handoff.endswith("rest\n")


def test_sync_is_idempotent(verifier):
    once = verifier.sync_metrics(
        PAGE, HANDOFF, {"tests": 99, "files": 5, "graph": 40}, nodes=15, edges=25
    )
    twice = verifier.sync_metrics(
        *once, {"tests": 99, "files": 5, "graph": 40}, nodes=15, edges=25
    )

    assert once == twice


def test_synced_values_read_back_through_the_verifier(verifier):
    """The writer and the checker must agree, or the gate can never go green."""
    page, handoff = verifier.sync_metrics(
        PAGE, HANDOFF, {"tests": 347, "files": 150, "graph": 6354}, nodes=2015, edges=4339
    )

    assert verifier._published_metric(page, "tests") == 347
    assert verifier._published_metric(page, "files") == 150
    assert verifier._published_metric(page, "graph") == 6354
    assert int(re.search(r"^- (\d+) tests are collected;", handoff, re.M).group(1)) == 347


def test_sync_refuses_to_silently_skip_a_missing_metric(verifier):
    with pytest.raises(ValueError, match="absent"):
        verifier.sync_metrics(
            PAGE, HANDOFF, {"nonexistent": 1}, nodes=1, edges=1
        )

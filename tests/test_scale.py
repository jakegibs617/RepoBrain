"""Deterministic indexing/traversal scale hardening (docs/SCALE_BENCHMARKS.md).

Uses the synthetic large-repository generator in
`repobrain/testing/synthetic_repo.py` (>= 1,000 supported files, generated
fresh into a pytest tmp dir — never committed) to assert:

- known graph answers hold at scale (not just "it didn't crash"),
- no-change and small-change incremental runs stay proportional to the
  changed work (measured in SQL statements issued, not wall clock — see
  `repobrain/testing/perf.py`),
- deliberately global/repository-wide reconciler passes are exercised and
  their cost stays bounded (a handful of statements, not one per row), and
- representative query results match small-corpus semantics (freshness,
  root pinning, bounded depth/limit contracts).

A generous wall-clock ceiling is asserted too, but only as a safety net
against gross regressions — the primary assertions are deterministic counts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from repobrain.freshness import ensure_fresh
from repobrain.graph.queries import (
    docs_for_code,
    explain_file,
    find_symbol,
    impact_analysis,
    trace_data_flow,
)
from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.retrieval.keyword import search
from repobrain.testing.perf import count_queries
from repobrain.testing.synthetic_repo import generate_synthetic_repo, touch_modules

N_MODULES = 1050

# Generous, hardware-independent safety ceilings. These exist to catch a
# gross algorithmic regression (e.g. an accidental O(n^2)), not to pin exact
# machine performance.
FULL_INDEX_CEILING_SECONDS = 90.0
INCREMENTAL_CEILING_SECONDS = 20.0

# Deterministic work-invariant ceilings: SQL statements issued for runs whose
# changed-file count is bounded should stay bounded too, independent of how
# many files are in the untouched corpus. These are generous multiples of
# what a single chunked touch/reconcile pass costs, not a tight bound.
NO_CHANGE_STATEMENT_CEILING = 100
SMALL_CHANGE_STATEMENT_CEILING = 1500


@pytest.fixture
def scale_repo(tmp_path):
    """Function-scoped: several tests mutate/delete generated files, and a
    shared module-scoped corpus would make those tests order-dependent.
    Generation is fast (well under a second for 1,000+ files — pure string
    writes, no parsing), so regenerating per test keeps every test isolated
    at a small, acceptable cost.
    """
    root = tmp_path / "repo"
    return generate_synthetic_repo(root, n_modules=N_MODULES)


@pytest.fixture
def scale_store(tmp_path):
    store = GraphStore(tmp_path / "db" / "repobrain.sqlite")
    yield store
    store.close()


def _count(store: GraphStore, sql: str, *args) -> int:
    return store.conn.execute(sql, args).fetchone()[0]


def test_full_index_matches_known_graph_answers(scale_repo, scale_store):
    import time

    indexer = Indexer(scale_store)
    start = time.perf_counter()
    stats = indexer.index(scale_repo.root, incremental=False)
    elapsed = time.perf_counter() - start

    assert elapsed < FULL_INDEX_CEILING_SECONDS, (
        f"full index of {scale_repo.n_modules} files took {elapsed:.1f}s, "
        f"exceeding the generous {FULL_INDEX_CEILING_SECONDS}s safety ceiling"
    )

    assert stats.files_scanned == scale_repo.total_files
    assert stats.files_changed == scale_repo.total_files
    assert stats.files_deleted == 0
    assert stats.warnings == []

    # Repo-global EnvVar identity (D17): every reader converges on one node.
    assert _count(scale_store, "SELECT COUNT(*) FROM nodes WHERE type='EnvVar'") == 1
    assert _count(
        scale_store, "SELECT COUNT(*) FROM edges WHERE type='READS_ENV'"
    ) == scale_repo.env_reader_count

    # Every worker_i -> helper_{i+1 % n} bare call is globally unique, so
    # finish_run's cross-file name-match pass (D16) resolves every one of
    # them; every Service{i}.run -> worker_i same-file call resolves during
    # parsing. Both counts are exactly n_modules.
    assert _count(
        scale_store, "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND is_inferred=1"
    ) == scale_repo.n_modules
    assert _count(
        scale_store, "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND is_inferred=0"
    ) == scale_repo.n_modules

    # Each doc has one root-anchored backtick path reference and one exact
    # qualified-symbol reference (D20); both resolve.
    assert _count(
        scale_store, "SELECT COUNT(*) FROM edges WHERE type='MENTIONS'"
    ) == 2 * len(scale_repo.doc_paths)


def test_no_change_run_stays_proportional_to_changed_work(scale_repo, scale_store):
    indexer = Indexer(scale_store)
    indexer.index(scale_repo.root, incremental=False)

    with count_queries(scale_store.conn) as stats:
        rerun = indexer.index(scale_repo.root, incremental=True)

    assert rerun.files_changed == 0
    assert rerun.files_deleted == 0
    assert rerun.warnings == []
    # Before the touch_paths batching fix this was O(files): ~2 statements
    # per unchanged file (one UPDATE each for nodes/edges last_seen_at).
    # Batched into chunked IN(...) updates, a no-op run over 1,000+ files
    # costs a small, corpus-size-independent number of statements.
    assert stats.total < NO_CHANGE_STATEMENT_CEILING, (
        f"no-change run over {scale_repo.n_modules} files issued "
        f"{stats.total} SQL statements; expected a small, bounded count"
    )


def test_small_change_run_stays_proportional_to_changed_work(scale_repo, scale_store):
    indexer = Indexer(scale_store)
    indexer.index(scale_repo.root, incremental=False)
    changed = touch_modules(scale_repo, [0, 1, 2, 3, 4])

    with count_queries(scale_store.conn) as stats:
        rerun = indexer.index(scale_repo.root, incremental=True)

    assert rerun.files_changed == len(changed)
    assert rerun.files_deleted == 0
    assert stats.total < SMALL_CHANGE_STATEMENT_CEILING, (
        f"a 5-file change over {scale_repo.n_modules} files issued "
        f"{stats.total} SQL statements; expected work proportional to the "
        f"changed set, not the whole corpus"
    )


def test_single_file_deletion_stays_bounded(scale_repo, scale_store):
    indexer = Indexer(scale_store)
    indexer.index(scale_repo.root, incremental=False)
    victim = scale_repo.root / scale_repo.module_path(0)
    victim.unlink()

    with count_queries(scale_store.conn) as stats:
        rerun = indexer.index(scale_repo.root, incremental=True)

    assert rerun.files_deleted == 1
    # Deletion triggers the intentionally-global runtime/markdown reconciler
    # rebuild (D20/D28) and the directory-liveness sweep, each of which is a
    # small, fixed number of *statements* even though they read/process
    # every node (documented repository-wide cost, not per-file query
    # fan-out). The regression this guards is per-file query fan-out, e.g.
    # a future change re-introducing one query per unchanged path.
    assert stats.total < SMALL_CHANGE_STATEMENT_CEILING


def test_representative_queries_match_small_corpus_semantics(scale_repo, scale_store):
    indexer = Indexer(scale_store)
    indexer.index(scale_repo.root, incremental=False)
    sample = scale_repo.n_modules // 2
    expected_next = (sample + 1) % scale_repo.n_modules

    exact = find_symbol(scale_store, f"worker_{sample}", exact=True)
    assert len(exact) == 1
    assert exact[0]["path"] == scale_repo.module_path(sample)

    explanation = explain_file(scale_store, scale_repo.module_path(sample))
    assert explanation is not None
    callee_qnames = {c["callee"] for c in explanation["calls_out"]}
    assert f"{scale_repo.qname(expected_next)}.helper_{expected_next}" in callee_qnames
    assert f"{scale_repo.qname(sample)}.worker_{sample}" in callee_qnames

    hits = search(scale_store, f"helper_{sample}", limit=10)
    assert hits and any(h.path == scale_repo.module_path(sample) for h in hits)

    doc_hits = docs_for_code(scale_store, scale_repo.module_path(sample))
    # Only modules chosen by the doc generator's stride are referenced;
    # verify the contract (bounded, well-formed) rather than assume a hit.
    assert isinstance(doc_hits, list)

    total_edges = _count(scale_store, "SELECT COUNT(*) FROM edges")
    flow = trace_data_flow(scale_store, scale_repo.qname(sample), depth=4, limit=50)
    assert flow is not None
    # `limit` stops enqueueing new frontier work once reached (a soft cap: a
    # single node's full edge set can still be appended past it), so assert
    # the traversal stayed bounded relative to the corpus rather than
    # re-walked the whole graph.
    assert len(flow["edges"]) < total_edges / 10

    impact = impact_analysis(
        scale_store, scale_repo.module_path(sample), depth=2, include_history=False
    )
    assert impact is not None
    assert impact["historical_evidence"]["items"] == []  # excluded by caller, as asked


def test_freshness_gate_and_root_pin_hold_at_scale(scale_repo, scale_store):
    indexer = Indexer(scale_store)
    indexer.index(scale_repo.root, incremental=False)

    # Freshness gate: current tree reports current without touching content.
    result = ensure_fresh(scale_repo.root, scale_store, auto_index=False)
    assert result["status"] == "current"
    assert result["can_query"] is True

    # Root pinning (D10): a different root is refused, not silently merged.
    from repobrain.indexing.indexer import RepoRootMismatchError

    other_root = scale_repo.root.parent / "not-the-indexed-root"
    other_root.mkdir(exist_ok=True)
    with pytest.raises(RepoRootMismatchError):
        indexer.index(other_root, incremental=True)

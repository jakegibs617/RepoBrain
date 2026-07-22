#!/usr/bin/env python3
"""Reproducible scale benchmark for indexing/traversal above 1,000 files.

Generates a deterministic synthetic repository (never committed — written to
a temp directory that is removed when the script exits), indexes it, and
reports phase timings plus SQL statement counts for:

  1. full (non-incremental) index
  2. no-change incremental index
  3. small-change incremental index (5 files touched)
  4. FTS search
  5. find-symbol
  6. explain file
  7. bounded graph traversal (trace_data_flow)
  8. impact analysis

Usage:
    .venv/bin/python scripts/benchmark_scale.py [--modules N] [--keep]

The corpus size, timings, and query counts are the artifact of this script;
see docs/SCALE_BENCHMARKS.md for the last recorded before/after numbers and
what changed.
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from repobrain.graph.queries import explain_file, find_symbol, impact_analysis, trace_data_flow
from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer
from repobrain.retrieval.keyword import search
from repobrain.testing.perf import Timing, count_queries, timer
from repobrain.testing.synthetic_repo import generate_synthetic_repo, touch_modules


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", type=int, default=1200, help="number of synthetic Python files")
    parser.add_argument("--keep", action="store_true", help="keep the generated corpus/db on exit")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="repobrain-scale-bench-"))
    timings: list[Timing] = []
    try:
        root = workdir / "repo"
        with timer("generate synthetic repo", timings):
            info = generate_synthetic_repo(root, n_modules=args.modules)

        db_path = workdir / "db" / "repobrain.sqlite"
        store = GraphStore(db_path)
        indexer = Indexer(store)

        with timer("full index (non-incremental)", timings), count_queries(store.conn) as q_full:
            stats = indexer.index(root, incremental=False)
        print(f"full index: files_scanned={stats.files_scanned} "
              f"files_changed={stats.files_changed} nodes={stats.nodes_created} "
              f"edges={stats.edges_created} warnings={len(stats.warnings)} "
              f"sql_statements={q_full.total}")

        with timer("no-change incremental index", timings), count_queries(store.conn) as q_noop:
            noop_stats = indexer.index(root, incremental=True)
        print(f"no-change index: files_changed={noop_stats.files_changed} "
              f"files_deleted={noop_stats.files_deleted} sql_statements={q_noop.total}")

        changed = touch_modules(info, [0, 1, 2, 3, 4])
        with timer("small-change incremental index (5 files)", timings), count_queries(store.conn) as q_small:
            small_stats = indexer.index(root, incremental=True)
        print(f"small-change index: files_changed={small_stats.files_changed} "
              f"(touched {len(changed)}) sql_statements={q_small.total}")

        sample = info.n_modules // 2
        with timer("fts search", timings):
            search(store, f"helper_{sample}", limit=10)
        with timer("find-symbol", timings):
            find_symbol(store, f"worker_{sample}", exact=True)
        with timer("explain file", timings):
            explain_file(store, info.module_path(sample))
        with timer("trace_data_flow (depth 4)", timings):
            trace_data_flow(store, info.qname(sample), depth=4)
        with timer("impact_analysis (depth 3)", timings):
            impact_analysis(store, info.module_path(sample), depth=3, include_history=False)

        store.close()

        print()
        print(f"{'phase':<45}{'seconds':>10}")
        for t in timings:
            print(f"{t.label:<45}{t.seconds:>10.3f}")
    finally:
        if args.keep:
            print(f"\nKept corpus/db at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()

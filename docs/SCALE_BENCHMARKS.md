# Scale benchmarks: indexing and traversal above 1,000 files

This documents the deterministic scale-hardening milestone: what was
measured, what changed, and what remains an intentional, documented cost.

## Reproducing

```
.venv/bin/python scripts/benchmark_scale.py --modules 1200
.venv/bin/python scripts/benchmark_scale.py --modules 6000
```

The script generates a deterministic synthetic repository (see
`repobrain/testing/synthetic_repo.py`) into a temp directory, indexes it,
runs a no-change incremental pass, a 5-file "small change" incremental pass,
and representative queries (FTS search, `find-symbol`, `explain file`,
`trace_data_flow`, `impact_analysis`), printing phase timings and SQL
statement counts. Nothing it generates is committed; the temp directory is
removed on exit (`--keep` to inspect it).

`tests/test_scale.py` runs the same generator (1,050 files — above the
1,000-file threshold) as part of the ordinary suite. Its assertions are
deterministic work invariants (known node/edge counts, SQL statement counts,
bounded-traversal contracts) plus generous wall-clock safety ceilings; it
does not gate on tight timing.

## What was measured

Fresh-process runs, same machine, before vs. after this milestone's fixes
(`repobrain/graph/store.py: GraphStore.touch_paths`,
`repobrain/parsers/code_treesitter.py: CodeParser.finish_run`):

### 1,200 files (7,407 nodes / 9,936 edges)

| phase                              | before (SQL stmts) | after (SQL stmts) | before (s) | after (s) |
|-------------------------------------|--------------------:|-------------------:|-----------:|----------:|
| full index (non-incremental)        |               45,616 |              44,419 |      0.489 |     0.479 |
| no-change incremental index         |                2,431 |                  13 |      0.076 |     0.080 |
| small-change incremental (5 files)  |                2,793 |                 381 |      0.099 |     0.110 |

### 6,000 files (36,879 nodes / 49,488 edges)

| phase                              | before (SQL stmts) | after (SQL stmts) | before (s) | after (s) |
|-------------------------------------|--------------------:|-------------------:|-----------:|----------:|
| full index (non-incremental)        |              227,224 |             221,239 |      2.551 |     2.576 |
| no-change incremental index         |               12,031 |                  33 |      0.415 |     0.414 |
| small-change incremental (5 files)  |               12,393 |                 401 |      0.501 |     0.502 |

Representative query timings at 6,000 files (after): FTS search 9ms,
`find-symbol` 16ms, `explain file` 3ms, `trace_data_flow` (depth 4) 30ms,
`impact_analysis` (depth 3, history excluded) 2ms. All sub-second at this
corpus size; `EXPLAIN QUERY PLAN` was checked for the highest-volume lookups
(exact/partial symbol name search, file path resolution) and every one used
an existing index (`idx_nodes_type`, `sqlite_autoindex_files_1`) except the
substring branch of keyword search's node-name LIKE, which is an inherent
full scan for a leading-wildcard pattern (no B-tree index can serve it) —
at these corpus sizes it is a sub-millisecond scan of the `nodes` table, so
no schema index was added without corpus evidence it helps (per the
milestone's constraint against speculative indexes/caches).

## What changed

**`GraphStore.touch_paths`** refreshed `last_seen_at` for every *unchanged*
file's nodes/edges with one `UPDATE ... WHERE path = ?` statement per path
(`executemany`, but each row is still its own statement to SQLite). On a
no-change run over N files this was O(N) statements — the dominant cost
above, and a direct violation of "no-change runs stay proportional to
changed work". It now batches into chunked `WHERE path IN (...)` statements
(500 paths per statement), since every touched row gets the identical
timestamp anyway — same result, O(N / 500) statements instead of O(N).
`last_seen_at` has no query consumers today (verified via repo-wide grep);
this change does not alter what is stored, only how many round trips it
costs to store it.

**`CodeParser.finish_run`** (cross-file name-only `CALLS` resolution, D16)
issued one `SELECT ... WHERE name = ?` per *distinct* pending callee name —
an N+1 pattern when a large run queues many distinct bare-call names (every
full index of this fixture queues exactly `n_modules` distinct names). It
now batches distinct names into chunked `name IN (...)` queries. This also
incidentally removed a `LIMIT 3` that could, in a corpus with more than 3
same-named candidates, produce an arbitrary (order-dependent) ambiguity
verdict; the batched query fetches every candidate for a name, so the
"exactly one global match" rule is now evaluated against the true candidate
set rather than a possibly-truncated one.

Code review on this change found that fetching every candidate for a name
reintroduced a different cost: filtering candidates down to "does exactly
one live outside my own file" was done per pending call by re-scanning that
name's whole candidate list, which is quadratic for a name that is common
across a real repo (`run`, `__init__`, ...) even though the fixture's
guaranteed-unique names never exercised it. Fixed by precomputing a
per-path match count for each name once, so "is there exactly one match
outside my file" is an O(1) arithmetic check per pending call; the O(matches)
list scan to find *which* row it is only runs in the rare case where the
arithmetic says a unique match remains. `tests/test_code_parser.py` gained
two regression tests: many same-named duplicates (exercises the batched
query path beyond the pre-existing 2-duplicate case) and a same-named
decoy *method* living in the caller's own file (exercises the per-path
exclusion arithmetic directly).

## Deliberately unavoidable repository-wide costs (not "fixed")

These are correct by design (see DECISIONS.md D12, D20, D25, D28) and were
confirmed, not "optimized away":

- **`freshness.check_freshness` / any incremental `scan()`** walks and
  stats every file under the repo root on every gated read, even when
  nothing changed. This is the only way to *know* nothing changed without
  trusting external signals; it is the dominant wall-clock cost of a
  no-change run at scale (the 0.41s at 6,000 files above is almost entirely
  this walk, not SQL — statement count dropped 365x while wall time barely
  moved, which is exactly the intended outcome: the *SQL* work is now
  proportional to changed work, while the *filesystem* work remains
  proportional to total tracked files, as D12 documents).
- **`RuntimeAdapterReconciler` / `MarkdownMentionReconciler`** (D28/D20)
  fully rebuild their owned fact families whenever any file changed or is
  deleted, because an unchanged Route/Markdown file can gain or lose a
  relationship when a *different* file changes. They already do this with a
  small, bounded number of SQL statements (a handful of `SELECT ... WHERE
  type = ...` queries that return many rows, not one query per row), so
  their statement count does not grow with corpus size — but their
  Python-side processing is O(total Routes/Modules/Documents) by design.
- **`repobrain/history.py` extraction** re-mines the bounded commit window
  from scratch whenever HEAD or history parameters change (D25); this is
  independent of working-tree file count.
- **`_cleanup_directories`** (indexer) still calls `store.active_files()`
  (a single query returning every active file, not one query per file) to
  recompute directory liveness after a deletion. Statement count is O(1);
  row-processing cost is O(total files). Measured at 71 SQL statements for
  a single-file deletion out of 1,200 files (down from 2,487 before the
  `touch_paths` fix, since deletion also triggers the reconciler rebuild
  path touched by that fix).

## Verified at scale (representative-result parity)

`tests/test_scale.py::test_representative_queries_match_small_corpus_semantics`
and `test_freshness_gate_and_root_pin_hold_at_scale` assert, over the
1,050-file corpus: exact `find-symbol` returns exactly one hit with the
right path, `explain file` reports the correct inferred and observed callee
qualified names, `trace_data_flow`'s bounded-`limit` contract holds, the
freshness gate reports `current`/`can_query=True` without auto-indexing an
unchanged tree, and indexing a different root through the same database
still raises `RepoRootMismatchError` (D10 root pinning is not weakened by
any of the above).

## Remaining limits

- The synthetic fixture exercises Python only (deepest-covered language);
  it does not stress JS/TS/Go/Java/Ruby/PHP extraction volume specifically.
- No memory/RSS profiling was captured (optional per the milestone scope;
  not verified as locally available in this environment).
- The filesystem-walk cost of freshness checking was not addressed — it is
  the correct, documented behavior (D12), not an oversight.

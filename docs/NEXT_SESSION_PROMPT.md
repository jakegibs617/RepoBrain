# Next Session Prompt

**M13 shipped as D56.** M12 carries forward at its original number, re-measured
against this branch and unchanged. It is now the only milestone item left.

The method note this session produced came from a test that was written for
something else entirely.

**A label must never be able to abort the thing it labels.** The first
implementation digested the package's sources inside the index run, and
`test_unreadable_file_is_skipped_with_warning` — a test about an unreadable
*repository* file, with no connection to provenance — failed with an `OSError`
raised out of `Indexer.index`. It was tempting to read that as
collateral damage from a global `monkeypatch` on `Path.read_bytes` and adjust
the test. It was not: an advisory field had acquired the power to crash an
index run, which is D54's lesson arriving from a new direction. **When an
unrelated test fails after you add a field to a hot path, assume the test is
right until you can say precisely why it is not.** The fix is in D56: unknown
is a value, and an identity that cannot be computed reports unknown rather than
raising.

The corollary for scope: the same softening was deliberately *not* applied to
D44's parser digest, which can still raise if a parser source is unreadable.
That digest is an input to the gate — unreadable parsers mean staleness cannot
be decided — which is a different situation from a caption that cannot be
printed. It is a real if remote robustness gap, and it is not this item's.

## Where things stand

- **D51** (#25) — scanning is contained to the *resolved* root. A symlink named
  `notes.md` pointing at credentials outside the repository was indexed and
  returned verbatim by `search`; ignore rules match names, and a link is reached
  by one name and read from another. Note the shape of this bug for next time:
  directory symlinks were already safe (`os.walk` defaults to
  `followlinks=False`), so the boundary looked intact under casual testing and
  only file symlinks leaked. **Half a working defense is harder to notice than
  none**, and it survived two prior audits for that reason.
  `EXTRACTOR_VERSION` → 2.
- **D52** (#26) — editable installs pass `--with-editable` in the generated
  SessionStart hook and `.mcp.json`. Before this, the agent-facing surfaces ran a
  four-day-old cached wheel (a 184-line `briefing.py` against a 301-line source)
  while printing `Index freshness: current.` Pre-D52 configs are recognized as
  owned and upgraded in place.
- **D53** (#27) — M10, closed. Defaults were part of the answer and a budget was
  the rest: `trace_data_flow` cost 367/565/25,247 tokens at depth 1/2/4, so its
  default was simply wrong, but `trace_symbol` costs 17,334 tokens at *depth 1*
  for a hub symbol, so lowering depth alone would not have been enough.
  `apply_query_budget` defaults to 10,000 and reports `truncation`.
- **D54** (#28) — a lost write-lock race is a retryable refusal with exit 75, not
  a traceback. `busy_timeout=5000` absorbs the ordinary overlap.
- **D55** (#32) — M11, closed. `project_brief` reports `truncation` in D48's
  shape and names the omissions in its `text`. The interesting part was not the
  reporting but paying for it: the footer's size depends on what was dropped, so
  re-running selection against its own last footer is the obvious fix and
  **oscillates rather than converging**. It is now at most two passes, the
  second reserving the widest report those candidates could produce.
- **D56** — M13, closed. Every status surface carries `code`: a digest of the
  installed package's sources plus the directory they were loaded from. D44's
  fingerprint deliberately scopes to `parsers/`, so editing `briefing.py` or
  `graph/queries.py` moved nothing and the entire read path could be arbitrarily
  old while every surface reported `current`. The identity is 40 files /
  489,671 bytes / **2.31 ms**, against D44's 9 files / 148,653 bytes / 0.35 ms;
  `repobrain freshness` end-to-end is unchanged at 0.17 s, so D44's refusal to
  cache survives the widening. `RECORD` was rejected on evidence — under an
  editable install it is 12 lines and lists no source file at all.
  **`changed_since_index` is a label and never a gate**: no agent can reinstall
  the code it is running inside, so an axis that can never become `ok` would
  break what `can_query` promises.

**M13's named residual.** The identity always says *which* build answered, which
was the complaint. The comparison says only that the reading build differs from
the indexing build: it is evidence of drift, not a measure of age, and
re-indexing with the old build clears it while the old build is still installed.
Absolute staleness needs a reference this process does not have. Do not treat
that as an unfinished edge — it is stated in D56 and in the skill reference, and
closing it means acquiring a reference (a release feed, or the checkout's own
sources when they are not the ones installed), which is a new decision.

## M12: this repository's brief has no `Entrypoints` section at all

Carried forward. Re-measured on this branch — three sections, and the numbers
D55 recorded still reproduce exactly (497 tokens at the default budget,
`applied: false`; 274 at `--budget 300`, reporting `Configuration: 12` and
`Subsystems: 6`):

```
Purpose
Subsystems
Configuration
```

D43 is why, and it is correct: every `Route` node still lives under
`tests/fixtures/`, and there are still **zero** `CLICommand` nodes.

```sql
SELECT path, count(*) FROM nodes WHERE type='Route' GROUP BY path;
-- tests/fixtures/node_api_app/src/routes/users.js     2
-- tests/fixtures/small_python_app/app/api/routes.py   2
SELECT count(*) FROM nodes WHERE type='CLICommand';  -- 0
```

So the brief for a project whose entire interface is a 19-command CLI cannot name
a single way to invoke it. `CLICommand` is declared and deliberately not
synthesised — README: *"reserved types such as `Endpoint`, `CLICommand`,
`Script`, and `ADR` are not synthesized."* That was right when nothing consumed
them. `Entrypoints` consumes them now, and its absence is the visible cost.

It is the only remaining item that touches extraction, so it moves D44's
fingerprint — budget for a re-index and check `test_extractor_source_digest`
before starting. Note that D51 bumped `EXTRACTOR_VERSION` to 2 for a scanner
change; read that decision for the precedent on when a non-parser change needs
the bump, and note that D56 did **not** need one (a new module outside
`parsers/` changes no extraction). Two things to settle:

1. Whether `CLICommand` extraction is Click-specific or a general decorator
   pattern. RepoBrain's own CLI is Click; `argparse` and `typer` are the obvious
   next two, and a `Route`-shaped extractor that only understands one framework
   is the runtime-adapter problem again.
2. Whether an entrypoint that is a CLI command belongs in the same section as an
   HTTP route. They answer the same agent question — *how do I invoke this?* —
   which argues yes; they have nothing else in common, which argues for the
   section to state which kind each one is.

## Housekeeping

- The database is `.repobrain/repobrain.sqlite`. A zero-byte `.repobrain/graph.db`
  sat beside it since 2026-07-29 and was deleted this session; it was never a
  database and nothing referenced it.
- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` fails until
  `scripts/refresh_snapshot.py` runs and the result is committed. This file is
  not an exception. Re-run it *after* any rebase, not just after the edit.
- The primary repo's `.venv` is an editable install pinned to the primary repo's
  own path; running its pytest from inside a worktree silently tests the wrong
  code unless `PYTHONPATH` points at the worktree under test.
- **Suite wall-clock is not a stable signal.** The same suite has measured 30 s
  and 67 s on the same machine minutes apart, so a slower run is not evidence
  that a change was expensive. Use `--durations` and compare against a stashed
  baseline before believing a regression.
- **Micro-benchmarks need a warm loop before they are quotable.** D56's digest
  measured 1.12 ms over 20 cold iterations and 2.31 ms in steady state — a 2x
  error, in the direction that would have made a cost look acceptable. Take the
  median of several 100-iteration runs, and say which one the decision records.
- `.venv/bin/ruff check .` reports nine pre-existing errors in `tests/`; CI runs
  `ruff check repobrain`, which is clean. Match CI's scope before concluding a
  change broke lint.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question below is resolved by a human.
- Merge policy is unchanged: auto-merge when green, full suite plus review with
  confirmed findings fixed. Stop the loop rather than merge anything
  questionable.
- Mutation-check every test written to catch a specific defect, check that the
  mutation is the *behavioural* one, **and check that it kills a test at all**.
  D56 ran seven mutations, all killed, including the one that matters most —
  feeding `changed_since_index` into `is_stale`, which is the invariant the
  whole decision rests on. A one-off script that applies each mutation, runs the
  single test that should die, and restores the file is worth writing; it is
  faster than doing it by hand and it leaves a record of what was checked.
- When six PRs must land the same day and each re-runs `refresh_snapshot.py`,
  they all touch `setup/graph-data.js`, `setup/evaluation.html` and
  `AGENT_HANDOFF.md`. Merging each when green and branching the next from updated
  `main` avoided the collisions entirely.
- `--base <commit>` is how to reach a wide diff without tripping the ten-file
  auto-index threshold: commit the changes, leave the tree clean, and diff
  against an earlier commit.
- Cite a **SHA, never `HEAD~N`**, in anything that outlives the session.

## Still needs a human, still not blocking

Unchanged. Neither should be decided by this loop:

- **Embeddings.** Trades away the deterministic-first stance every D-series
  decision has protected, for recall that name-based matching cannot reach. If
  taken, it needs a human call on keeping it optional and local-only rather than
  compromising the no-network invariant.
- **Multi-repo support.** D8/D10 pin a database to the repository root it
  indexes; supporting more is an architecture decision, not an incremental
  slice, and needs scoping before any code is written.

Two more that the 2026-07-29 audit raised and that this loop cannot close on its
own evidence:

- **A Windows CI leg.** The POSIX-shell/WSL limitation is documented in the
  README and on the setup site, but native Windows behavior has never been
  executed — three audits have now recorded it as *Unable to evaluate*. A minimal
  `windows-latest` index + search + MCP-config smoke (Git hooks excluded) would
  convert a documented caveat into evidence. It needs a human to decide whether
  the matrix cost is worth it.
- **A committed scale corpus.** `docs/SCALE_BENCHMARKS.md` is credible and not
  reproducible at a fixed revision. The audit's own 6,000-file measurement
  (2.9 s) corroborates the direction but is not a substitute.

Read `AGENT_HANDOFF.md` in full before making any of these calls. This file is
not a substitute for it.

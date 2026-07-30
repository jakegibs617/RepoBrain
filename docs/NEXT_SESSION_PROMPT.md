# Next Session Prompt

**M11 shipped as D55.** M12 and M13 carry forward at their original numbers,
unchanged and un-re-measured this session — the last file measured both at
`50ce295` and nothing since has touched extraction or the freshness envelope.

The method note this session produced is about a defect in the *fix*, found by
a test rather than by review.

**A test that excuses the case it exists to catch is not a test.** M11's budget
sweep first read `if truncation["within_budget"]: assert len(text) <= budget*4`
— which is vacuous exactly when the implementation is wrong, because a brief
that overflows sets `within_budget` to false and skips the assertion. The
mutation that proved it: capping the selection loop at one pass left the sweep
green while three unrelated tests failed. Restated as *any brief showing a fact
at all must be within budget* — on the reasoning that while facts are being
shown, one more could always have been declined instead — it caught a real
oscillation in the first implementation. **When a test guards an assertion on a
field the implementation also computes, check that the guard is not the
implementation marking its own homework.**

The corollary for mutation testing: a mutation that survives is not always a
missing test. Understating the footer reserve by one digit per section survived
every test, and it *should* have — at this repository's scale it is a four-byte
difference, not a behavioural change. The behavioural mutation was removing the
reserve entirely, which five tests killed. The invariant behind it (the reserve
bounds any report it could produce) got its own direct assertion instead of a
contorted end-to-end one.

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
- **D55** — M11, closed. `project_brief` reports `truncation` in D48's shape and
  names the omissions in its `text`. At budget 300 this repository now says
  `Configuration: 12 fact(s) not shown` where it previously dropped the section
  in silence; the default budget is byte-identical to before. The interesting
  part was not the reporting but paying for it: the footer's size depends on
  what was dropped, so re-running selection against its own last footer is the
  obvious fix and **oscillates rather than converging**. It is now at most two
  passes, the second reserving the widest report those candidates could produce.

M12 and M13 below were measured against `main` at **50ce295**. Read D52 before
touching M13; it sits directly on D52's closing paragraph.

## M13: nothing in the output names the code that produced it

D52 removed the way a stale build got installed. It did not make code staleness
*observable*, and said so. This is that item.

The freshness envelope has six fields and not one of them describes the software
that answered:

```json
{"status": "ok", "is_stale": false, "extractor_changed": false,
 "out_of_date_count": 0, "changed_bytes": 0, "files": 155,
 "last_indexed_at": "2026-07-30T12:55:22Z"}
```

D44's fingerprint is not this. It hashes `repobrain/parsers/**` on purpose, and
that scope is exactly the blind spot — measured at `50ce295`:

```
baseline fingerprint:           dd895d0cbf161a4b
after editing briefing.py:      dd895d0cbf161a4b   <- the file that was stale in F1
after editing graph/queries.py: dd895d0cbf161a4b
after editing a parser:         515cef4dd31d796d   <- moves, as D44 intends
```

So the entire read path can be arbitrarily old and every surface still reports
`current`, sincerely: old code and the index old code built are perfectly
self-consistent. `__version__` is no help — `0.1.0` was set once and has never
moved across all 79 commits.

Two things to settle, and they are the whole item:

1. **What the identity is.** A digest over the installed package's own sources
   is the obvious extension of D44, but D44 also documented why it refuses to
   cache that digest, and widening it from ~147 KB of parsers to the whole
   package changes that cost calculation on a surface a statusline may poll on a
   timer. Measure it before choosing. The alternatives — the wheel's `RECORD`
   hash, or a git SHA when the install is a checkout — are cheaper but describe
   only some install kinds, and a field that is present for wheels and absent
   for editables is worse than no field.
2. **Whether this is a gate or a label, because it cannot be a gate.** Index
   staleness is reported by a system that can *fix* it: the agent runs
   `repobrain index`. An agent cannot reinstall the code it is running inside.
   A freshness `status` that can never become `ok` by any action available to
   the caller is a different object from the one D40 designed, and putting it in
   the same field would break the contract that `can_query` means something
   actionable. Decide whether this belongs in `freshness` at all, or in `status`
   and the brief header as advisory provenance.

## M12: this repository's brief has no `Entrypoints` section at all

Carried forward unchanged. Re-measured at `50ce295` — three sections:

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

Still the largest of the three and still the only one that touches extraction, so
it moves D44's fingerprint — budget for a re-index and check
`test_extractor_source_digest` before starting. Note that D51 has since bumped
`EXTRACTOR_VERSION` to 2 for a scanner change; read that decision for the
precedent on when a non-parser change needs the bump. Two things to settle:

1. Whether `CLICommand` extraction is Click-specific or a general decorator
   pattern. RepoBrain's own CLI is Click; `argparse` and `typer` are the obvious
   next two, and a `Route`-shaped extractor that only understands one framework
   is the runtime-adapter problem again.
2. Whether an entrypoint that is a CLI command belongs in the same section as an
   HTTP route. They answer the same agent question — *how do I invoke this?* —
   which argues yes; they have nothing else in common, which argues for the
   section to state which kind each one is.

## Housekeeping

- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` fails until
  `scripts/refresh_snapshot.py` runs and the result is committed. This file is
  not an exception. Re-run it *after* any rebase, not just after the edit.
- The primary repo's `.venv` is an editable install pinned to the primary repo's
  own path; running its pytest from inside a worktree silently tests the wrong
  code unless `PYTHONPATH` points at the worktree under test.
- **Suite wall-clock is not a stable signal.** The same 409-test suite measured
  30 s and 67 s on the same machine minutes apart, so a slower run is not
  evidence that a change was expensive. Use `--durations` and compare against a
  stashed baseline before believing a regression. Two write-lock tests each pay a
  real 5 s `busy_timeout`; folding two assertions into one blocked run took the
  file from 13 s to 6.8 s and lost nothing.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question below is resolved by a human.
- Merge policy is unchanged: auto-merge when green, full suite plus review with
  confirmed findings fixed. Stop the loop rather than merge anything
  questionable.
- Mutation-check every test written to catch a specific defect, check that the
  mutation is the *behavioural* one, **and check that it kills a test at all**.
  Two new data points: the 2 MB size cap had no test at all until #29, found by
  mutating `MAX_FILE_SIZE` to 1 GB and watching all 395 tests pass; and the two
  security tests in #25 were confirmed as real detectors by reverting the fix and
  watching them fail, rather than by assuming a new test tests something.
- When six PRs must land the same day and each re-runs `refresh_snapshot.py`,
  they all touch `setup/graph-data.js`, `setup/evaluation.html` and
  `AGENT_HANDOFF.md`. Merging each when green and branching the next from updated
  `main` avoided the collisions entirely — simpler than the stack-and-rebase
  approach the previous file recommended, and worth preferring when items are
  independent.
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

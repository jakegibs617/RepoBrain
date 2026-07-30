# Next Session Prompt

One of the three items the previous version queued shipped: **M10** became D53
(PR #27). **M11 and M12 did not, and carry forward unchanged** — both were
re-measured at `50ce295` and both still reproduce exactly as written. The third
item below, M13, replaces nothing; it is the residual D52 named and deliberately
declined to fix in the same PR.

This session was not a dogfooding pass. An independent `/audit-app` run at
`cb692dd` produced six findings, and all six shipped as PRs #25–#30 (D51–D54,
plus two changes small enough not to warrant a decision record). M10 was one of
them, arrived at independently — which is mild evidence the queue is pointing at
real things.

On method, two things landed, and both are about verifying the *fix* rather than
the defect.

**A remedy is a claim too, and the audit's was wrong.** The audit recommended
closing F1 with `uvx --refresh-package`. One measurement killed it: neither
`--refresh` nor `--refresh-package` invalidates a `file://` directory build —
both return the stale wheel in 0.15 s. Only `--with-editable` (0.17 s) or
`--no-cache` (1.39 s) actually rebuild. The previous file's rule — *read the code
before believing the item's framing, including this file's* — extends to the
proposed solution, not just the diagnosis. Measure the remedy before writing it
into a decision record.

**Assert on the surface the caller receives, not the one you computed.** D53's
first implementation applied the budget inside the MCP callback and still
returned 10,080 tokens against a 10,000 budget, because `_query` attaches the
freshness envelope *after* the callback returns. It was trimming a payload that
does not exist by the time anyone reads it. The tests now assert on the tool's
return value rather than the query function's; that is the only reason the bug
surfaced before merge rather than after.

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

Everything below was measured against `main` at **50ce295**. Read D52 and D53
before touching M13 or M11 respectively; M13 sits directly on D52's closing
paragraph.

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

## M11: `project_brief` drops facts silently — the one thing D45, D47, D48 and now D53 all refuse to do

Carried forward unchanged, and now the sole holdout. D53 gave three more tools
`truncation` reporting; `project_brief` reports `budget` and `token_estimate` and
**no `truncation` key**. It does not trim a payload — it declines to add facts
that will not fit, one at a time, and never says how many it declined.
Re-measured at `50ce295`:

```
budget 4000 → 26 facts   (Purpose 2, Subsystems 12, Configuration 12)
budget 2000 → 26 facts
budget  800 → 26 facts
budget  300 → 11 facts   (Purpose 2, Subsystems 9 — Configuration is GONE)
```

Sharper than the previous file recorded: at 300 the `Configuration` section does
not shrink, it **disappears entirely**, and the result says nothing. An agent
reads eleven grounded facts and three section headings' worth of silence. That is
the confidently-wrong answer the freshness gate exists to prevent, on the single
most-read surface RepoBrain has.

The fix is probably small; the design question is not. Settle two things:

1. Whether "declined to add" and "trimmed after the fact" report through the same
   key. They are different mechanisms with the same consequence, and an agent
   cares only about the consequence. D53's `apply_query_budget` now provides a
   second precedent for the `truncation.dropped` shape — check whether the brief
   can reuse it outright rather than growing a third dialect.
2. Whether the human `text` rendering says so too. D47 required it for
   `change-context` and D53 added it to `impact`; a brief whose prose is silent
   about omissions is the surface an agent actually reads.

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

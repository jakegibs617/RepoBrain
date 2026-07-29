# Next Session Prompt

Three well-scoped engineering gaps are queued below. Unlike the chain that
ended at D34, none of these were manufactured by looking for something to do —
each one was found by RepoBrain failing at its own job on its own repository,
which is the only source of work this loop should trust.

The previous version of this file recommended a human planning checkpoint on
the grounds that the loop had run out of real gaps. That was true of the
call-resolution chain and is still true of it. It stopped being true of the
project: the audit remediation (D35–D39), the freshness display surface
(D40–D41), and the extractor-identity work (D42) all came from dogfooding, and
two of the three items below were found the same way inside a single session.
The embeddings/multi-repo decision at the bottom is still unresolved and still
needs a human; it is no longer blocking, because there is real work in front of
it.

## Where things stand

PR #12 merged as `76209a7`. It shipped `repobrain freshness`, snapshot
freshness gating, the agent skill, and then — from reviewing itself — D42 and a
fix to the read-only open. Read D40, D41, and D42 in `DECISIONS.md` before
touching any of the three items below; all three sit directly on top of them.

## M1: `change-context --json` cannot be used for what it is for

The command exists so an agent can understand a diff. It currently cannot: on
the two-commit diff ending at `76209a7` — fourteen files — `--json` emits

```
1,837,378 chars = ~459,000 tokens
```

That is not "large", it is unusable by an order of magnitude, and it is worse
per-file than the ~162k measured on PR #12's own 51-file diff. The distribution
says why:

| key | chars | items |
| --- | ---: | ---: |
| `impact` | 829,713 | 3 |
| `changes` | 224,620 | 84 |
| `text` | 157,277 | — |
| `tests_to_run` | 94,917 | 32 |

`impact` is three confidence buckets holding 830k characters, so the cost is in
how many rows those buckets carry and how much of each node they embed.

Two things to establish before designing a fix, in this order:

1. **Is `setup/graph-data.js` distorting this?** It is a generated, minified,
   single-line ~2 MB artifact that is nonetheless a first-class graph citizen,
   and it entered the diff in this very range. If a generated snapshot of the
   graph is inflating the graph's own impact analysis, that is a more
   interesting bug than the token count, and it may be the whole story. Measure
   with and without it before assuming a budget is the answer.
2. **Only then, a budget.** `brief` already has the shape worth copying:
   `--budget` with the deterministic `ceil(chars / 4)` estimate, defaulting to
   something a session can actually spend. Truncation must be reported in the
   payload rather than silent — a quietly truncated impact set is exactly the
   confidently-wrong answer the freshness gate exists to prevent.

## M2: every entrypoint the brief promotes is a test fixture

`repobrain brief` on this repository, right now:

```
Entrypoints
- POST /api/users            (tests/fixtures/node_api_app/src/routes/users.js:6)
- GET  /api/users/:id        (tests/fixtures/node_api_app/src/routes/users.js:19)
- POST /api/users            (tests/fixtures/small_python_app/app/api/routes.py:6)
- GET  /api/users/<int:...>  (tests/fixtures/small_python_app/app/api/routes.py:10)
```

Not "some fixtures rank too highly" — all four, and RepoBrain is a CLI with no
routes of its own, so the correct output here is an empty or absent section.
This is the first thing an agent reads at session start, and it is currently
100% noise that actively misdescribes the project.

The fix is a ranking question, not a filtering one: fixture and test paths
should lose entrypoint promotion, not disappear from the graph. Check how
`briefing.py` selects entrypoints and whether the existing exclude/include
config already expresses "indexed but not representative", since inventing a
second mechanism for that would be worse than the bug.

Worth confirming while in there: this is a self-index artifact, but a real user
with an `examples/` or `fixtures/` directory hits it too.

## M3: nothing forces `EXTRACTOR_VERSION` to be bumped

D42's own named follow-on, and it is the load-bearing weakness in what D42
built. `ParserRegistry.fingerprint()` hashes `EXTRACTOR_VERSION` with the
sorted parser names. Composition changes are caught automatically. The case
D42 was written for — a parser keeping its name while changing its output — is
caught **only if a human remembers to bump the constant**, which is precisely
the discipline that failed and produced the stale index D42 diagnoses.

So D42 currently detects the class of bug that has never bitten us and relies
on memory for the one that has. Two candidate designs, both cheap:

- **Hash the parser sources.** Fold a digest of `repobrain/parsers/**` into the
  fingerprint. Fully automatic, no discipline required. Costs a spurious full
  re-index on comment-only or refactor-only edits, which is a real but bounded
  annoyance — measure it against the ~25 s full index of this repo before
  ruling it out.
- **Make CI enforce the bump.** Fail when `repobrain/parsers/**` changes in a
  diff without `EXTRACTOR_VERSION` moving. Keeps re-indexing precise, but only
  binds contributors who go through CI, and D37 is explicit that CI should
  re-derive facts rather than police process.

Pick one deliberately and record it; do not do both.

## Housekeeping

- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` will fail
  until `scripts/refresh_snapshot.py` runs and the result is committed. This
  file is not an exception — rewriting it is itself a snapshot-drifting change.
- The primary repo's `.venv` is an editable install pinned to the primary
  repo's own path; running its pytest from inside a worktree silently tests the
  wrong (stale) code unless `PYTHONPATH` points at the worktree under test.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question below is explicitly resolved by a
  human, this constraint stays in force.
- Merge policy is unchanged: auto-merge when green, full suite plus review with
  confirmed findings fixed. Stop the loop rather than merge anything
  questionable.

## Still needs a human, still not blocking

Unchanged from the previous version of this file, and still the two biggest
levers left un-pulled. Neither should be decided by this loop:

- **Embeddings.** Trades away the deterministic-first stance every D-series
  decision has protected, in exchange for recall on queries that name-based
  matching cannot reach. If taken, it needs a human call on how to keep it
  optional and local-only rather than compromising the no-network invariant.
- **Multi-repo support.** D8/D10 pin a database to the repository root it
  indexes; supporting more is an architecture decision, not an incremental
  slice, and needs scoping before any code is written.

Read `AGENT_HANDOFF.md` in full before making either call. This file is not a
substitute for it.

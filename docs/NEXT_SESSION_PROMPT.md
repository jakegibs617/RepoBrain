# Next Session Prompt

The three items the previous version queued — M1, M2, M3 — shipped as PRs #13,
#14, and #15 (D43, D44, D45). Each was found by RepoBrain failing at its own
job on its own repository, and each of the three below was found the same way,
two of them while fixing the first three.

One thing worth carrying forward about method: M1's queued hypothesis was
wrong. The prompt suspected `setup/graph-data.js` was inflating the impact
analysis and asked for that to be established *before* designing a fix. It
contributes 0 impact items and 278 characters. Measuring first cost one command
and redirected the entire piece of work. Keep doing that.

## Where things stand

- **D43** — `brief` withholds entrypoint promotion from paths carrying a
  `TestFile` node. All four promoted routes were fixtures.
- **D44** — the extractor fingerprint hashes `repobrain/parsers/*.py`, so a
  parser changing its output can no longer depend on someone remembering to
  bump `EXTRACTOR_VERSION`. Full re-index of this repo measured at 0.59 s,
  against the ~25 s the prompt assumed.
- **D45** — `change-context --json` went from ~461,000 tokens to 142,848
  losslessly, then to a reported, budgeted ~14,600 by default.

Read D43, D44, and D45 before touching anything below; all three items sit on
them.

## M4: the default budget buys the diff and nothing derived from it

D45 names this as unanswered, which makes it the most load-bearing loose end
this session leaves.

`changes` is now the dominant key — about 59,000 tokens for the 84-file diff
that motivated D45. Priority ranks the diff above evidence derived from it, so:

```
budget  15000:  14593 tokens | changes 27/86 | impact  0 | tests  0
budget  60000:  59472 tokens | changes 86/86 | impact 26 | tests 32
```

At the default, a wide diff yields no impact and no tests — which is the part
an agent cannot get from `git diff --stat`. The command degrades into a worse
version of a tool the agent already has.

The ordering is right for a working diff of a handful of files and the question
is whether it should hold at this width. The candidate D45 names is degrading
`changes` to paths-only past some width and spending the difference on impact.
Before building that, establish what the 59,000 tokens is actually made of:
`symbols` was 171,354 of the 237,155 characters, and a symbol record still
carries `path` (the parent change has it) and `provenance` (derivable from
`path` and `start_line`). A second lossless pass may move this enough that no
priority change is needed. Measure before designing, as above.

## M5: `Subsystems` has M2's bug, ranked by path length

`repobrain brief` on this repository, right now:

```
Subsystems
- setup/graph      [Module] (setup/graph.js:1)
- setup/script     [Module] (setup/script.js:1)
- repobrain.cli    [Module] (repobrain/cli.py:1)
- tests.conftest   [Module] (tests/conftest.py:1)     <-
- repobrain.config [Module] (repobrain/config.py:1)
- ...
- tests.test_scale [Module] (tests/test_scale.py:1)   <-
- tests.test_memory  [Module] (tests/test_memory.py:1)  <-
- tests.test_search  [Module] (tests/test_search.py:1)  <-
```

Four of twelve slots are test modules, and `repobrain.briefing`,
`repobrain.freshness`, and `repobrain.indexing` are absent. This is milder than
M2 — tests are genuinely part of the project, so this is "ranked too highly"
rather than "categorically wrong" — but the cause is worse: `_node_facts`
orders by `length(path),path`, so the twelve subsystems an agent is shown are
the ones with the shortest paths. That is not a relevance ranking at all.

Two questions to settle in order:

1. Does the D43 `TestFile` predicate simply apply here too? It is already
   written and already tested. If demoting test modules is the whole fix, do
   only that.
2. Only if it is not: what *should* order subsystems? Edge degree is the
   obvious source already in the graph and needs no new extraction. Do not
   invent a config key for this.

## M6: `examples/` and `fixtures/` are still promoted

D43's own named limitation. `is_test_file` matches the path segments
`{tests, test, __tests__, spec}`, so this repository was fixed only because its
fixtures happen to live under `tests/`. A user whose sample application sits in
a top-level `examples/` directory still gets its routes promoted as their
project's entrypoints — the exact bug D43 fixed, for the more common layout.

D43 deliberately did not widen `_TEST_DIRS`: that set feeds `TestFile`
classification globally, including `recommended_tests` in `impact_analysis`
(`repobrain/graph/queries.py`), and changing it is an extraction change that
now moves the extractor fingerprint under D44 and forces a re-index for every
user on upgrade. That is a real cost, and D44 makes it visible rather than
silent, which is why this is worth doing deliberately rather than as a
one-line edit.

The question is whether "not representative" and "is a test" should be the same
predicate at all. They were the same thing for RepoBrain and are not in
general: an `examples/` directory is not tests. Decide that before writing code.

## Housekeeping

- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` fails until
  `scripts/refresh_snapshot.py` runs and the result is committed. This file is
  not an exception.
- The primary repo's `.venv` is an editable install pinned to the primary
  repo's own path; running its pytest from inside a worktree silently tests the
  wrong code unless `PYTHONPATH` points at the worktree under test.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question below is resolved by a human.
- Merge policy is unchanged: auto-merge when green, full suite plus review with
  confirmed findings fixed. Stop the loop rather than merge anything
  questionable.
- Two of this session's three fixes were only provable by mutation — narrowing
  `test_no_change_fast_path_does_not_read_file_bodies` (D44) and the budget
  resync (D45) both had tests that passed against the broken code first. When a
  test is written to catch a specific defect, break the code and confirm it
  fails.

## Still needs a human, still not blocking

Unchanged. Neither should be decided by this loop:

- **Embeddings.** Trades away the deterministic-first stance every D-series
  decision has protected, for recall that name-based matching cannot reach. If
  taken, it needs a human call on keeping it optional and local-only rather
  than compromising the no-network invariant.
- **Multi-repo support.** D8/D10 pin a database to the repository root it
  indexes; supporting more is an architecture decision, not an incremental
  slice, and needs scoping before any code is written.

Read `AGENT_HANDOFF.md` in full before making either call. This file is not a
substitute for it.

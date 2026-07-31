# Next Session Prompt

**M12 shipped as D57. The milestone list is empty.** Every numbered item from
the ten-milestone MVP through M13 is closed. What remains is four questions that
need a human and a set of named residuals that are deliberately open — none of
them is an unfinished edge, and none should be closed by this loop on its own
authority.

The method note this session produced came from a test that was measured instead
of exempted, and from a benchmark that was wrong in the flattering direction.

**An invariant nothing enforces is an invariant that grows by accident.** Adding
one bounded `pyproject.toml` read in `begin_run` failed
`test_no_change_fast_path_does_not_read_file_bodies`. The tempting reading was
that a manifest read is obviously fine and the test needed an exemption. It was
not: measured, an unchanged run of a *two-file* repository reads `go.mod` **and**
`pyproject.toml`, and always has. D19's read goes through `Path.read_text` while
the guard patched `Path.read_bytes`, so the exemption existed, was never decided,
and grew the moment a second reader arrived using the other method. **Before
exempting your change from a rule, check whether the rule was ever enforcing
anything** — the answer here was that it had been enforcing half of itself for a
year. `RUN_SCOPED_MANIFESTS` (`repobrain/parsers/base.py`) now names the set and
the guard covers both methods.

**A benchmark that shows no difference is a claim, and it can be wrong the same
way any other claim can.** The first cost comparison for D57 showed the new
parser costing nothing at all. It was invalid: `indexer.py` does `from
..parsers.base import default_registry`, binding the function at import, so
patching `repobrain.parsers.base.default_registry` changed nothing and both arms
measured the identical registry. The real figure was +35%, and the fix that
brought it to +15% only got written because the second measurement was taken.
**When a change measures as free, suspect the harness before believing it** —
pass the dependency in explicitly (`Indexer(store, registry=reg)`) and assert the
arms actually differ.

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
- **D56** (#33) — M13, closed. Every status surface carries `code`: a digest of
  the installed package's sources plus the directory they were loaded from.
  D44's fingerprint deliberately scopes to `parsers/`, so editing `briefing.py`
  or `graph/queries.py` moved nothing and the entire read path could be
  arbitrarily old while every surface reported `current`. **`changed_since_index`
  is a label and never a gate**: no agent can reinstall the code it is running
  inside, so an axis that can never become `ok` would break what `can_query`
  promises.
- **D57** — M12, closed. `CliParser` produces `CLICommand` from the decorator
  shape, and `Entrypoints` promotes routes and commands in one section because
  `_fact_line` already prints each fact's type. The brief now names 12 of this
  repository's 25 commands, prefixed with the console script from
  `[project.scripts]`. The name is the whole deliverable, so it is checked
  against Click rather than against a literal list: Click derives an unnamed
  command as `f.__name__.lower().replace("_", "-")`, which makes `find_symbol`
  invocable as `find-symbol` and nothing else, and `pyproject.toml` pins only
  `click>=8.1`. Brief cost went 497 → 678 tokens at the default budget; at
  `--budget 300` it is 279 and now reports `Entrypoints: 12` among its
  omissions. `EXTRACTOR_VERSION` did **not** move, which is D44 working as
  designed — a new module inside `parsers/` is caught by the source digest and
  by registry composition both.

## What is actually left

**Nothing numbered.** Do not invent an M14 to have something to do. The
residuals below are stated in their decisions and in the README's Limitations;
each of them is a new decision, not a loose end, and the honest default is to
leave them.

- **D56's residual.** The identity always says *which* build answered, which was
  the complaint. The comparison says only that the reading build differs from the
  indexing build: it is evidence of drift, not a measure of age, and re-indexing
  with the old build clears it while the old build is still installed. Absolute
  staleness needs a reference this process does not have — a release feed, or the
  checkout's own sources when they are not the ones installed.
- **D57's residuals.** No edge links a `CLICommand` to the function it decorates;
  `RUNS_COMMAND` exists in `EdgeType` and is unused, and the handler's name and
  line are in metadata so a reconciler has what it needs. `argparse` is not
  covered — a genuinely different shape, `subparsers.add_parser(...)` calls with
  no decorator. Typer is covered by shape and by no indexed Typer project, and
  its nested groups come from an `add_typer(...)` *call* rather than a decorator,
  so they do not resolve.
- **Three parsers now `ast.parse` the same Python files independently.**
  `RouteParser` does it on every Python file unconditionally, `CliParser` on the
  21 that contain the substring `command`, and `CodeParser` runs tree-sitter over
  all of them. A shared per-file parse handed to parsers alongside `content`
  would pay for itself on the existing baseline, not just on the new parser —
  the measured full re-index is 0.981 s for 163 files, of which the D57 guard
  already recovered 0.17 s. It is an indexer-shaped change, not a parser-shaped
  one, and it is worth scoping properly before writing any of it.
- **`Entrypoints` promotes twelve, ranked by path then line.** So this
  repository's last thirteen commands — `search`, `impact`, `memory write`,
  everything after `repobrain/cli.py:507` — never appear. That is the same
  ceiling `Subsystems` and `Configuration` have always had. A relevance ranking
  for commands would be its own decision and should not ride inside a promotion
  fix.

## Housekeeping

- The database is `.repobrain/repobrain.sqlite`.
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
- **Micro-benchmarks need a warm loop before they are quotable**, and an A/B
  benchmark needs its arms asserted. D56's digest measured 1.12 ms over 20 cold
  iterations and 2.31 ms in steady state — a 2x error, in the direction that
  would have made a cost look acceptable. D57's first A/B measured a 35% cost as
  0%, because both arms were the same registry. Take the median of several
  100-iteration runs, say which one the decision records, and assert that the
  two arms actually differ before trusting a null result.
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
  D57 ran thirteen and one survived on the first pass — the mutation widened a
  receiver check to `ast.Attribute` when the test's case was an `ast.Call`, so it
  changed nothing the test looked at. The rewritten mutation (accept any receiver
  via `ast.unparse`) killed it. A one-off script that applies each mutation, runs
  the single test that should die, and restores the file is worth writing; it is
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

Unchanged. None should be decided by this loop:

- **Embeddings.** Trades away the deterministic-first stance every D-series
  decision has protected, for recall that name-based matching cannot reach. If
  taken, it needs a human call on keeping it optional and local-only rather than
  compromising the no-network invariant.
- **Multi-repo support.** D8/D10 pin a database to the repository root it
  indexes; supporting more is an architecture decision, not an incremental
  slice, and needs scoping before any code is written.
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

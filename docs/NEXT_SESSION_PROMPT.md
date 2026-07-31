# Next Session Prompt

**Nothing is queued.** The milestone list is empty and the working state is
clean: every numbered item from the ten-milestone MVP through the post-MVP
M11–M16 track and the audit-derived M10–M13 track is closed and merged. As of
`670a5e2` the repository has one branch, `main`, on the remote and locally; no
pull request is open; and every decision through **D57** is in `DECISIONS.md`.

This file is not a backlog. It exists so a session that starts with no
instruction knows what is deliberately open, what is waiting on a human, and
which traps this project has already paid for.

**Do not invent a milestone to have something to do.** If you arrived here
without a task from the user, the correct first move is to ask for one. The
items below are each a *new decision* — scoped, argued, and worth doing only if
someone chooses them — not unfinished edges left behind by the last session.

## Read before deciding anything

- `AGENT_HANDOFF.md` — delivery status, architecture, known pitfalls. Longer
  than this file and authoritative where they overlap.
- `DECISIONS.md` — 57 decisions, each with the reasoning and the scope boundary.
  A residual named in a decision is a boundary someone drew, not an oversight.
- `README.md` §Limitations — what the tool says it does not do. The residuals
  below are documented there too; changing one means changing that text.

## Deliberately open

- **Absolute staleness has no reference (D56).** Every status surface names
  *which* build answered, and the comparison says the reading build differs from
  the indexing build. That is evidence of drift, not a measure of age:
  re-indexing with the old build clears it while the old build is still
  installed. Measuring age needs a reference this process does not have — a
  release feed, or the checkout's own sources when they are not the ones
  installed.
- **No edge links a `CLICommand` to its handler (D57).** `RUNS_COMMAND` exists
  in `EdgeType` and is unused; the handler's name and line are in the node's
  metadata so a reconciler has what it needs. Resolution belongs to a reconciler
  for the same reason `HANDLES_ROUTE` is kept out of `RouteParser`.
- **`argparse` and Typer groups are uncovered (D57).** `argparse` declares
  subcommands through `subparsers.add_parser(...)` calls with no decorator — a
  genuinely different shape. Typer is covered by shape and by no indexed Typer
  project; its nested groups come from an `add_typer(...)` *call*, so they do
  not resolve.
- **Three parsers `ast.parse` the same Python files independently.**
  `RouteParser` on every Python file unconditionally, `CliParser` on the subset
  containing the substring `command`, and `CodeParser` runs tree-sitter over all
  of them. A shared per-file parse handed to parsers alongside `content` would
  pay for itself on the existing baseline, not only on the newest parser. It is
  an **indexer**-shaped change, not a parser-shaped one, and is worth scoping
  properly before any of it is written.
- **Promotion ranks by path then line, and stops at twelve.** `Entrypoints`
  therefore names the first twelve of this repository's 25 commands and never
  the rest; `Subsystems` and `Configuration` have always had the same ceiling. A
  relevance ranking is its own decision and must not ride inside a promotion fix.

## Still needs a human

None of these should be decided by an agent loop:

- **Embeddings.** Trades away the deterministic-first stance every D-series
  decision has protected, for recall that name-based matching cannot reach. If
  taken, it needs a human call on keeping it optional and local-only rather than
  compromising the no-network invariant.
- **Multi-repo support.** D8/D10 pin a database to the repository root it
  indexes. Supporting more is an architecture decision, not an incremental
  slice, and needs scoping before any code is written.
- **A Windows CI leg.** The POSIX-shell/WSL limitation is documented in the
  README and on the setup site, but native Windows behavior has never been
  executed — three audits have recorded it as *Unable to evaluate*. A minimal
  `windows-latest` index + search + MCP-config smoke (Git hooks excluded) would
  convert a documented caveat into evidence. Whether the matrix cost is worth it
  is a human call.
- **A committed scale corpus.** `docs/SCALE_BENCHMARKS.md` is credible and not
  reproducible at a fixed revision. The 2026-07-29 audit's own 6,000-file
  measurement (2.9 s) corroborates the direction but is not a substitute.

## Housekeeping

Facts about this repository that cost a session to learn.

- The database is `.repobrain/repobrain.sqlite`. `repobrain freshness` never
  indexes and always exits 0.
- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` fails until
  `scripts/refresh_snapshot.py` runs and the result is committed. This file is
  not an exception. Re-run it *after* any rebase, not just after the edit.
- The primary repo's `.venv` is an editable install pinned to the primary repo's
  own path; running its pytest from inside a worktree silently tests the wrong
  code unless `PYTHONPATH` points at the worktree under test.
- `.venv/bin/ruff check .` reports nine pre-existing errors in `tests/`. CI runs
  `ruff check repobrain`, which is clean — match CI's scope before concluding a
  change broke lint.
- The full suite is 454 tests. **Wall-clock is not a stable signal**: the same
  suite has measured 30 s and 67 s on the same machine minutes apart. Use
  `--durations` against a stashed baseline before believing a regression.
- **Before exempting a change from a rule, check whether the rule was ever
  enforcing anything.** The read-body guard patched `Path.read_bytes` while the
  reader it was written for used `read_text`, so the exemption had existed for a
  year, undecided, and grew the moment a second reader arrived.
  `RUN_SCOPED_MANIFESTS` (`repobrain/parsers/base.py`) now names the set.
- **When a change measures as free, suspect the harness before believing it.**
  Pass the dependency in explicitly (`Indexer(store, registry=reg)`) and assert
  the two arms actually differ. A cost comparison that patched a module
  attribute already bound at import measured a real +35% as 0%.
- **Micro-benchmarks need a warm loop before they are quotable.** A digest
  measured 1.12 ms over 20 cold iterations and 2.31 ms in steady state — a 2x
  error in the flattering direction. Take the median of several 100-iteration
  runs and say which one the decision records.
- **Mutation-check every test written to catch a specific defect**, check that
  the mutation is the *behavioural* one, and check that it kills a test at all.
  A mutation that widens a check the test never exercises proves nothing. A
  one-off script that applies each mutation, runs the single test that should
  die, and restores the file is worth writing.
- `--base <commit>` is how to reach a wide diff without tripping the ten-file
  auto-index threshold: commit the changes, leave the tree clean, and diff
  against an earlier commit.
- Cite a **SHA, never `HEAD~N`**, in anything that outlives the session.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question above is resolved by a human.
- Merge policy: one branch per change, full suite plus review with confirmed
  findings fixed, auto-merge when green, delete the branch on merge. Stop rather
  than merge anything questionable. When several PRs must land the same day and
  each re-runs `refresh_snapshot.py`, they all touch `setup/graph-data.js`,
  `setup/evaluation.html` and `AGENT_HANDOFF.md` — merge each when green and
  branch the next from updated `main` to avoid the collisions entirely.

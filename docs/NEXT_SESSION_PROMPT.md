# Next Session Prompt

The three items the previous version queued — M7, M8, M9 — all shipped, as PRs
#21, #22 and #23 (D48, D49, D50). The queue is empty; the three below replace
it. Each was found the same way the last nine were: by running RepoBrain
against its own repository and reading the output as an agent would.

On method, two things landed this time.

**Both of the previous file's open questions were answered by one command
each, and both answers made the work smaller.** M7 asked whether
`project_brief` had the same missing-budget gap as `change_context`; it does
not, and has not since it shipped. M8 asked whether `EnvVar` nodes were
*supposed* to carry a location, which would have made the fix an extraction
change that moves D44's fingerprint; they are not — D17 makes them pathless on
purpose and their location already survives in `metadata.observation`. Read the
code before believing the item's framing of the problem, including this file's.

**A mutation that kills nothing is evidence about the test, not the code.** The
list-block rule in D50 was nearly deleted as dead weight because the fixture
written for it used list items with no full stops, so the weaker rule covered
the case alone. The real README's items do carry full stops. The rule is
load-bearing and the fixture was lying.

## Where things stand

- **D48** — the MCP `change_context` tool takes `budget`, defaulting to the
  CLI's 15,000. On this repository's 87-file diff: 137,858 tokens with no
  `truncation` key → 14,906 tokens, within budget, 87/87 changes, 17 impact,
  32 tests.
- **D49** — brief eligibility asks whether a fact can be *cited*, not whether
  it has a line, so repo-global nodes are promotable; `Configuration` ranks by
  kind rather than degree, and withholds config found under documentation.
- **D50** — `Purpose` promotes the first paragraph that makes a statement.

All three items below were measured against `main` at **e69d41f**. Read D48–D50
before touching anything below; M10 sits directly on D48.

## M10: two MCP tools are four times larger than the payload D48 was written for

D48 gave `change_context` a budget because 137,858 unbudgeted tokens is not an
answer. It fixed one tool. Measured on this repository at `e69d41f`, every MCP
tool, same minute:

| tool | tokens | reports its own limits? |
| --- | ---: | --- |
| `trace_symbol("index")` | **61,151** | no |
| `impact_analysis("repobrain/graph/store.py")` | **45,970** | no |
| `co_change("repobrain/cli.py")` | 3,740 | no |
| `explain_project()` | 3,083 | no |
| `churn_hotspots()` | 1,424 | no |
| `project_brief()` | 1,398 | partial — see M11 |
| `search_project("index")` | 807 | no |
| `ownership()` | 494 | no |
| `read_agent_memory()` | 115 | no |

`trace_symbol` at depth 2 emits four times the budget D48 just established as
the right size for a whole change context, and says nothing about having done
so. `impact_analysis` is three times it. Both are ordinary calls an agent makes
early, before it knows enough to pass a narrower argument.

Do **not** reflexively bolt `_apply_budget` onto all nine. Two questions first,
and they are the whole item:

1. `trace_symbol` and `impact_analysis` already take `depth` and `limit`. Find
   out whether the size is a missing budget or a bad default — a `depth=2`
   traversal over a 2,158-node graph may simply be the wrong default, in which
   case a budget hides the defect rather than fixing it. Measure the token
   count by depth before designing anything.
2. D45's trim order and D47's fidelity tiers exist because *what* to shed is a
   per-surface judgement. A traversal has no equivalent of "the diff outranks
   what is derived from it" — decide what a truncated trace should keep, or
   establish that truncation is the wrong tool for this shape of result.

## M11: `project_brief` drops facts silently — the one thing D45, D47 and D48 all refuse to do

`project_brief` reports `budget` and `token_estimate` and **no `truncation`
key**. It does not trim a payload; it declines to add facts that will not fit,
one at a time, and never says how many it declined. Measured at `e69d41f`:

```
budget 2000 → 26 facts
budget  300 → 11 facts        (15 facts gone, nothing in the result says so)
```

An agent reading the 300-token brief sees eleven source-grounded facts and no
indication that more than half the project's promoted facts were withheld. That
is the confidently-wrong answer the freshness gate exists to prevent, on the
single most-read surface RepoBrain has — and the honesty property D45 named
non-negotiable, D47 extended to fidelity tiers, and D48 carried to MCP.

The fix is probably small; the design question is not. `change-context` reports
`truncation.dropped` as counts per labelled bucket, and the brief's units are
sections and facts, so the shape is available. Settle two things:

1. Whether "declined to add" and "trimmed after the fact" should report through
   the same key. They are different mechanisms with the same consequence, and
   an agent cares only about the consequence.
2. Whether the human `text` rendering says so too. D47 required it for
   `change-context`; a brief whose prose is silent about omissions is the
   surface an agent actually reads.

## M12: this repository's brief has no `Entrypoints` section at all

```
RepoBrain project brief
Index freshness: current.

Purpose
Subsystems
Configuration
```

Three sections. There is no `Entrypoints`, and D43 is why — correctly. Every
`Route` node in the graph lives under `tests/fixtures/`:

```sql
SELECT path, count(*) FROM nodes WHERE type='Route' GROUP BY path;
-- tests/fixtures/node_api_app/src/routes/users.js     2
-- tests/fixtures/small_python_app/app/api/routes.py   2
```

So the brief for a project whose entire interface is a CLI cannot name a single
way to invoke it. `CLICommand` is declared in the schema and deliberately not
synthesised — README: *"reserved types such as `Endpoint`, `CLICommand`,
`Script`, and `ADR` are not synthesized."* That was the right call when nothing
consumed them. `Entrypoints` consumes them now, and its absence is the visible
cost.

This is the largest of the three and the only one that touches extraction, so
it moves D44's fingerprint — budget for a re-index and check
`test_extractor_source_digest` before starting. Two things to settle:

1. Whether `CLICommand` extraction is Click-specific or a general decorator
   pattern. RepoBrain's own CLI is Click; `argparse` and `typer` are the
   obvious next two, and a `Route`-shaped extractor that only understands one
   framework is the runtime-adapter problem again.
2. Whether an entrypoint that is a CLI command belongs in the same section as
   an HTTP route. They answer the same agent question — *how do I invoke this?*
   — which argues yes; they have nothing else in common, which argues for the
   section to state which kind each one is.

## Housekeeping

- **Any edit under `docs/` or to a tracked Markdown file drifts the published
  self-snapshot.** `test_published_repository_snapshot_is_current` fails until
  `scripts/refresh_snapshot.py` runs and the result is committed. This file is
  not an exception. Re-run it *after* any rebase, not just after the edit.
- The primary repo's `.venv` is an editable install pinned to the primary
  repo's own path; running its pytest from inside a worktree silently tests the
  wrong code unless `PYTHONPATH` points at the worktree under test.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question below is resolved by a human.
- Merge policy is unchanged: auto-merge when green, full suite plus review with
  confirmed findings fixed. Stop the loop rather than merge anything
  questionable.
- Mutation-check every test written to catch a specific defect, check that the
  mutation is the *behavioural* one, **and check that it kills a test at all**.
  Mangling a SQL string failed fifteen tests on a parameter mismatch and proved
  nothing — the third time this repository has recorded that trap. A mutation
  that kills nothing means the fixture does not reproduce the defect.
- `--base <commit>` is how to reach a wide diff without tripping the ten-file
  auto-index threshold: commit the changes, leave the tree clean, and diff
  against an earlier commit.
- Cite a **SHA, never `HEAD~N`**, in anything that outlives the session.
- Stacking PRs on the previous branch and rebasing with
  `git rebase --onto origin/main <old-base> <branch>` after each squash-merge
  kept three same-day PRs from colliding in `DECISIONS.md`. Worth repeating
  when items append to the same file.

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

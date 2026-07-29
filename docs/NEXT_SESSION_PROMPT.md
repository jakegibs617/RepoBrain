# Next Session Prompt

The three items the previous version queued — M4, M5, M6 — shipped as PRs #17
and #18 (D46, D47). M5 and M6 turned out to be one decision and went together.
Each of the three below was found the same way the last six were: by running
RepoBrain against its own repository and reading the output as an agent would.

On method, the same lesson landed twice more. **Both** queued hypotheses were
wrong, and one command each was enough to establish it:

- M5 asked whether D43's existing `TestFile` predicate was the whole fix for
  `Subsystems`. It is not. With test modules removed the twelve promoted
  subsystems were still the twelve shortest paths, still missing `graph.store`,
  `graph.queries`, `indexing.indexer`, `parsers.base`, and `change_context`.
- M4 asked whether a second lossless pass over symbol records would move enough
  that no priority change was needed. It saves 9,680 tokens and still yields
  zero impact and zero tests at the default budget. So does discarding every
  symbol record.

Measure first. It has now redirected three of the last four pieces of work.

## Where things stand

- **D46** — brief promotion ranks `Subsystems` by edge degree, not path length,
  and withholds promotion from `examples/`-style paths through a predicate
  local to `briefing.py` rather than by widening the parser's `_TEST_DIRS`.
  Measured cost at scale: 1.0 ms → 3.2 ms inside a 42 ms brief.
- **D47** — `changes` sheds fidelity in three reported stages
  (`changes.symbols`, `changes.file_node`, `changes.line_ranges`) before the
  evidence derived from the diff is given up. On this repository's own 87-file
  diff (`--base b80a0a9`) at the default budget: 27/87 changes, 0 impact, 0
  tests → 87/87 changes, 18 impact, 32 tests.

All three items below were re-measured against `main` at 2544cc4, after both
merges, and all three still reproduce. The commands that produced each number
are in the item.

Read D46 and D47 before touching anything below; M7 and M8 sit directly on
them.

## M7: the MCP `change_context` tool has no budget at all

D45 built the budget, D47 fixed what it spends on — and neither reaches the
surface an agent is most likely to call. `RepoBrainTools.change_context`
(`repobrain/mcp_server.py:142`) passes `base` and `auto_index` and nothing
else, so `budget` defaults to `None` and the tool emits everything:

Measured against `b80a0a9`, an 87-file diff (pinned deliberately — a
`HEAD~N` reference decays the moment this repository moves):

```
CLI  `change-context --base b80a0a9 --json`  →   14,895 tokens, truncation reported
MCP  change_context(base="b80a0a9")          →  137,858 tokens, no `truncation` key
```

Same repository, same diff, same minute. The MCP payload is not merely larger:
it carries no `budget`, no `token_estimate`, and no `truncation`, so a caller
has nothing to read to discover it was complete — the one honesty property D45
and D47 both treat as non-negotiable.

This is the highest-value item here and probably the smallest. The obvious fix
is to give the tool the CLI's default and expose the parameter, and it is
likely correct. Two things to settle before writing it:

1. `project_brief` is the other budgeted surface — check whether its MCP tool
   has the same gap rather than fixing one and leaving its twin.
2. Whether the MCP default should equal the CLI's 15,000. An MCP caller is
   inside a live session, so its budget is arguably tighter, not equal. Do not
   invent a config key for this; pick one and record why.

## M8: `Configuration` cannot show an environment variable, and shows packaging boilerplate instead

`repobrain brief` on this repository, right now, after D46:

```
Configuration
- pyproject.toml [ConfigFile] (pyproject.toml:1)
- build-system.requires [ConfigKey] (pyproject.toml:2)
- build-system.build-backend [ConfigKey] (pyproject.toml:3)
- project.name / .version / .description / .readme / .requires-python
- project.license / project.authors ...
```

Twelve slots, every one of them `pyproject.toml` packaging metadata. Nothing an
agent needs in order to run or configure anything. This is M5's bug in the
section D46 deliberately left alone — the ordering there is still
`length(path),path`, and I declined to change it on the grounds that it had no
*measured* defect. It has one now.

The sharper half is not ranking but eligibility. `_node_facts` requires
`start_line IS NOT NULL`, and this repository's three `EnvVar` nodes —
`DATABASE_URL`, `LOG_LEVEL`, `PORT` — carry `path=''` and `start_line=NULL`:

```sql
SELECT count(*) FROM nodes WHERE type='EnvVar';                          -- 3
SELECT count(*) FROM nodes WHERE type='EnvVar' AND start_line IS NOT NULL; -- 0
```

So `EnvVar` is named as one of the section's three node types and can never
appear in it. Establish which of the two is true before designing anything:
whether `EnvVar` nodes are *supposed* to carry a location (their `READS_ENV`
edges do — check what `explain file` reports for a file that reads one), or
whether the brief's eligibility rule is what is wrong. Those lead to different
fixes, one of them an extraction change that moves D44's fingerprint. The same
`start_line IS NOT NULL` filter is why `Directory` nodes have never been
brief-eligible either (D46 records this); that may be the same question asked a
third time.

## M9: `Purpose` promotes a heading fragment as a fact

```
Purpose
- RepoBrain is a **local-first "second brain" for AI coding agents**. It indexes … [Purpose] (README.md:1)
- Implemented: [Purpose] (README.md:11)
```

The second fact is the word `Implemented:` and nothing else. `_purpose_facts`
(`repobrain/briefing.py`) takes the first non-heading paragraph of each
matching section, and a section whose body opens with a bare list lead-in
yields the lead-in. It costs an agent little, which is why this is third — but
the brief is the one surface whose whole claim is that everything in it is a
source-grounded fact, and `Implemented:` is not one.

Cheapest defensible fix is a minimum-substance test on the candidate paragraph
before promoting it. Resist anything that needs a word list or a heuristic
about English; a paragraph that is one short colon-terminated fragment is
recognisable without either. Confirm against the other fixture READMEs that
the rule does not silently empty the section for a project whose README is
terse — an absent `Purpose` is a worse outcome than a thin one.

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
- Mutation-check every test written to catch a specific defect, and check that
  the mutation you chose is the *behavioural* one: reverting D46's ordering by
  deleting a SQL fragment broke thirteen tests through a parameter-count
  mismatch and proved nothing. Reverting it at the call site failed exactly one
  test, which is the signal you want.
- `--base <commit>` is how to reach a wide diff without tripping the ten-file
  auto-index threshold: commit the changes, leave the tree clean, and diff
  against an earlier commit. Both D47 regression tests are built this way.
  Cite a **SHA, never `HEAD~N`**, in anything that outlives the session — this
  file quoted `HEAD~8` for an hour and three merges later it named a 16-file
  diff instead of an 87-file one.

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

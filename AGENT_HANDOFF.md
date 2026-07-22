# Agent Handoff

## Project Summary

RepoBrain: a local-first second brain for AI coding agents (see `prd.md`).
All ten PRD milestones, post-MVP Milestones 11–16, and protocol-level MCP
hardening are now implemented. Milestones 1–4 delivered storage,
incremental indexing, code/docs parsing, search, and documentation mapping.
Milestones 5–10 add config adapters and tracing, route/data-flow analysis,
impact analysis, MCP tools, durable agent memory, and Markdown/HTML reports.

## Delivery Status

- Scale hardening is implemented on `feat/scale-hardening`: a deterministic
  synthetic large-repo generator (`repobrain/testing/synthetic_repo.py`, 1,050+
  real parseable files with known graph answers, never committed) plus
  SQL-statement-counting instrumentation (`repobrain/testing/perf.py`) drove a
  measured baseline and two confirmed fixes. `GraphStore.touch_paths` batched
  from one `UPDATE` per unchanged file into chunked `WHERE path IN (...)`
  statements (a no-op reindex of 1,200/6,000 files dropped from
  2,431/12,031 SQL statements to 13/33). `CodeParser.finish_run`'s cross-file
  name-match `CALLS` resolution batched from one query per distinct callee
  name into chunked `name IN (...)` queries, then had a code-review-caught
  quadratic candidate-rescan fixed with a precomputed per-path match-count
  arithmetic check.
- `tests/test_scale.py` (6 tests) asserts deterministic work invariants (known
  node/edge/CALLS/EnvVar/MENTIONS counts, bounded SQL-statement ceilings for
  no-change/small-change/deletion runs, representative query parity, freshness
  gate and root-pinning behavior) over a 1,050-file corpus, plus generous
  wall-clock safety ceilings (not the primary assertion). `scripts/
  benchmark_scale.py` is the reproducible manual benchmark; `docs/
  SCALE_BENCHMARKS.md` records the measured before/after numbers at 1,200 and
  6,000 files and which repository-wide costs (freshness `scan()`, the
  Markdown/runtime-adapter global reconcilers, Git history extraction) are
  intentionally left as documented, bounded-statement-count-but-not-bounded-
  row-processing costs rather than "fixed".
- Scale-hardening verification: 222 pytest tests passed (214 baseline + 6 new
  scale tests + 2 new `finish_run` regression tests covering many-duplicate
  ambiguity and same-file-decoy exclusion arithmetic); compilation and
  whitespace checks were clean. Two independent review passes ran; the first
  caught the `finish_run` quadratic-rescan regression (fixed before merge),
  the second found nothing further.

- Protocol-level MCP hardening is implemented. A bounded integration harness
  launches the real CLI server over stdio and uses the official MCP client for
  initialize/capability negotiation, discovery, representative reads, and
  success/not-found/error/tool-error envelopes. A narrow raw harness covers
  malformed JSON, cancellation notification handling, EOF, invalid-path exit,
  timeouts, and robust terminate/kill cleanup without duplicating tool logic.
- Transport tests prove small-diff repair, opted-out and oversized fail-closed
  behavior with no stale result payload, absolute-path confinement, and
  protocol-only stdout. An offline isolated `uvx` smoke builds a clean wheel
  and launches the exact installer-generated argument array against a
  repository path containing spaces; it skips only with explicit evidence
  when optional tools or cached offline dependencies are unavailable.
- The README now documents the stdio lifecycle, result/error contract,
  cancellation limitation, and out-of-scope transports. Protocol verification:
  61 focused MCP/freshness/distribution tests and 214 full pytest tests passed;
  compilation and whitespace checks were clean. Review fixed an overly broad
  isolated-smoke skip that could have hidden real packaging failures.

- M16 framework/runtime adapters are implemented on
  `feat/framework-runtime-adapters`. A transaction-local reconciler consumes
  persisted syntax facts and emits precise Flask-style/Express route-handler
  relationships plus conservative SQLAlchemy Table/READS_TABLE/WRITES_TABLE
  evidence without importing target frameworks.
- Express inline callbacks now have deterministic Function identities and
  callback-attributed CALLS; named callbacks resolve through exact local or
  persisted import bindings. Dynamic/multiple callbacks and receivers are
  skipped. SQLAlchemy operations require one exact model binding and one
  literal `__tablename__`, and are labeled inferred at confidence 0.85.
- Adapter facts globally converge on additions, renames, deletions, ambiguity,
  stale-edge cleanup, unchanged callers, no-change runs, and versioned legacy
  graph backfill. Shared data-flow, impact, and change-context queries consume
  the new evidence without CLI/MCP forks.
- M16 verification: 208 pytest tests passed; Python compilation and whitespace
  checks were clean. The final structured diff review found no remaining
  actionable regressions after fixing versioned backfill, exact Flask span
  disambiguation, and empty adapter-set injection.

- Distribution is implemented on `feat/distribution-agent-install`.
  `repobrain install-agent` now installs one exact repository-scoped
  `mcpServers.repobrain` entry in `.mcp.json`, the marker-owned CLAUDE.md
  session context, the exact SessionStart hook, and optional Git hooks in one
  command. JSON is preflighted before writes; malformed shapes and conflicting
  RepoBrain servers fail closed (including different versions/sources),
  repeated installs from the same provenance converge, and uninstall preserves
  unrelated or user-modified configuration.
- The MCP entry uses `uvx --from <repobrain[mcp]-requirement> repobrain mcp
  --path <resolved-root>` as a JSON argument array, pinning a registry install's exact
  version or retaining a local wheel/editable direct URL. Persisted Claude/Git
  automation uses the same stable provenance rather than an installation
  environment's disposable interpreter; legacy interpreter-based SessionStart
  entries are migrated without duplication.
- Distribution verification builds and inspects wheel/sdist contents and the
  console entry point. A clean local wheel ran under isolated `uvx`, and an
  isolated environment imported the MCP extra successfully. Hatchling is an
  explicit dev dependency so artifact inspection remains offline in the test
  suite after normal dev installation. Full verification: 199 pytest tests
  passed; compilation and whitespace checks were clean.
- M15 memory verification is implemented on `feat/m15-memory-verification`.
  New memory writes store exact file/unambiguous-symbol anchors plus resolution
  evidence; every shared read annotates entries as verified, drifted,
  invalidated, or unanchored without changing Markdown or stored metadata.
- `repobrain memory verify` and MCP `verify_agent_memory` run behind the M12
  gate with matching grounded JSON/plain results. Line moves use stable node
  identity, path moves use M14 `git_commit_files` rename continuity, and
  ambiguity is exposed rather than guessed.
- M11 briefs put invalidated/drifted memory first, include an explicit count,
  and omit invalidated sessions from current assumptions/recent-memory facts
  while retaining atomic budget behavior.
- M15 verification: 183 pytest tests passed; compilation and whitespace
  checks were clean, and the final Codex review found no actionable regressions.

- M14 is implemented on `feat/m14-git-history-extractor`: a deterministic
  Git history extractor (`repobrain/history.py`) mines a bounded recent
  commit window (default 500 commits, local plumbing only) into
  `git_commits`/`git_commit_files` tables and extractor-owned
  `CO_CHANGED_WITH` edges with rename continuity, broad-commit discounting,
  merge/vendor/`.repobrain` exclusions, and supporting commit ids on every
  relationship.
- `repobrain history co-change|hotspots|owners` and the matching MCP tools
  (`co_change`, `churn_hotspots`, `ownership`) serve gated, provenance-stamped
  reports; `impact_analysis` and `change_context` gained a separately labeled
  `historical_evidence`/`historical_impact` bucket capped below static
  confidence. The M12 gate re-extracts when HEAD or history parameters move;
  shallow/non-Git repos report `unavailable` without blocking static queries.
- The M14 refinement pass uses one `rev-parse` probe spawn, reports bare
  repositories explicitly, invalidates caches on extractor/ignore/config
  changes, centralizes File identity in `file_node_id`, and caps edge metadata
  at 20 newest supporting commits while retaining the complete evidence set in
  `git_commit_files`.
- M14 verification: 171 pytest tests passed; whitespace checks and the
  uncommitted Codex review were clean before merge.
- M13 is implemented on `feat/m13-diff-aware-change-context`:
  `repobrain change-context` and the matching MCP tool capture a working or
  merge-base branch diff before freshness repair, then map changed lines to
  symbols, aggregate impact/tests, and flag unchanged docs that mention the
  changed code.
- Change capture distinguishes add/modify/rename/delete/copy/type changes,
  preserves old/new paths and old/new line ranges, exposes binary/special
  files honestly, and never mutates Git state. Deleted symbols are parsed
  deterministically from the captured Git blob with revision provenance.
- M13 verification: 146 pytest tests passed; Python compilation and whitespace
  checks were clean before review.
- M12 is implemented on `feat/m12-freshness-automation`: every read-only CLI
  and MCP query passes through one freshness gate. Small diffs are repaired
  incrementally; large, opted-out, or failed repairs refuse to serve stale
  facts with a structured/actionable envelope.
- `install-agent --git-hooks` adds marker-owned post-commit/post-merge
  dispatch blocks and an owned index runner. `uninstall-agent` removes only
  RepoBrain-owned settings, Markdown, runner, and dispatcher blocks.
- M12 verification: 133 pytest tests passed; Python compilation and whitespace
  checks were clean before review.
- M11 is implemented on `feat/m11-session-start-briefing`: `repobrain brief`
  and the matching `project_brief` MCP tool produce a fixed-priority,
  source-grounded orientation pack under an approximate token budget.
- The brief performs a read-only size+mtime freshness check before serving
  facts. `repobrain install-agent` adds an idempotent Claude Code SessionStart
  hook and a marker-owned CLAUDE.md section without replacing human content.
- M11 verification: 99 pytest tests passed; compilation and whitespace checks
  were clean before review.
- PR #2, `feat: complete RepoBrain MVP (Milestones 3-10)`, was reviewed and
  merged into `main` on 2026-07-10.
- Merge commit: `6039214305c1f435fbe1b120d1dc1e1284c9a94b`.
- Review fixed MCP/CLI query drift and confined MCP indexing to its configured
  repository root before merge.
- Final verification: 90 pytest tests passed; Python compilation and whitespace
  checks were clean. GitHub reported a clean, mergeable PR with no configured
  remote status checks.

## Current Architecture Understanding

- `repobrain/graph/schema.py` — full NodeType/EdgeType enums, `Node`/`Edge`/
  `FtsRow` dataclasses, deterministic sha1 id helpers (D2).
- `repobrain/graph/store.py` — `GraphStore`: WAL SQLite, tables `nodes`,
  `edges`, `files`, `index_runs`, `meta` (repo-root pin), FTS5
  `content_fts(path, name, content, node_id UNINDEXED)`.
- `repobrain/graph/queries.py` — reusable traversals returning plain dicts
  (`find_symbol`, `explain_file`, config/data-flow tracing, impact analysis,
  and doc/code mapping). CLI and MCP layers call these shared queries.
- `repobrain/indexing/indexer.py` — scan → diff → parse → single-transaction
  store. Two optional parser hooks (duck-typed via `getattr`):
  `begin_run(known_paths)` before parsing (import resolution needs the full
  scanned file set) and `finish_run(store) -> list[Edge]` inside the
  transaction after upserts (cross-file name-match CALLS), before the orphan
  edge sweep — which also removes any deterministically-computed edge targets
  that turned out not to exist.
- `repobrain/indexing/doc_references.py` — **new**: `MarkdownMentionReconciler`
  resolves structured Markdown references against the complete persisted graph.
  It globally rebuilds its own `MENTIONS` edges after relevant changes so
  incremental runs converge when either side of a relationship changes.
- `repobrain/indexing/runtime_adapters.py` — `RuntimeAdapter` protocol,
  `RuntimeAdapterReconciler`, precise Flask/Express handler reconciliation,
  and exact-model SQLAlchemy table flow. Adapter-owned facts are globally
  rebuilt inside the index transaction and guarded by a version stamp.
- `repobrain/parsers/route_parser.py` — source-local Flask-style/Express Route
  facts only; handler resolution is deliberately outside the parser.
- `repobrain/parsers/code_treesitter.py` — **new**: `CodeParser` +
  per-language `_Extractor` subclasses (Python, JS/TS shared, PHP, Bash, Go,
  Java, Ruby). One compiled tree-sitter Query per grammar (lru_cache); the
  query captures def/import/call/env candidates and Python-side logic does
  scoping, qualified names, and resolution. Emits Module/Function/Class/
  Method/Variable/TestFile/TestCase/EnvVar nodes; DEFINES/CONTAINS/IMPORTS/
  CALLS/READS_ENV edges; FTS rows per symbol (name + qualified name +
  signature line).
- `repobrain/parsers/base.py` — `Parser` interface, `ParseResult`, registry
  (now with `.all()`), `GenericFileParser`. All matching parsers run per file:
  a `.py` file gets File (generic) + Module/symbols (code parser).
- `repobrain/cli.py` — click CLI: `init`, `index`, `status`, `search`,
  **`find-symbol NAME [--exact] [--limit] [--path] [--json]`**, and the new
  `explain` group with **`explain file FILEPATH [--path] [--json]`**.
- `repobrain/briefing.py` — assembles the M11 project brief from indexed graph
  facts and structured memory. Sections degrade atomically in fixed priority
  order using `ceil(characters / 4)` as the documented token estimate.
- `repobrain/freshness.py` — read-only working-tree comparison using the same
  scanner configuration and size+mtime shortcut as incremental indexing, plus
  the M12 shared pre-query gate and conservative auto-index thresholds.
- `repobrain/agent_install.py` — preflighted conservative merge of the exact
  RepoBrain `.mcp.json` server entry, SessionStart hook, marker-owned CLAUDE.md
  snippet, and optional marker-owned Git hook artifacts, with exact uninstall
  and legacy-hook migration.
- `repobrain/change_context.py` — M13 Git plumbing, changed-line mapping,
  impact/test aggregation, live MENTIONS traversal, historical exact-path doc
  evidence for renamed/deleted targets, and shared plain/JSON-ready results.
- `repobrain/history.py` — M14 Git history extractor: read-only plumbing over
  a bounded window, `-z --numstat` state-machine parser (renames, binary,
  empty commits), rename-continuity aliasing, co-change scoring, churn and
  ownership queries, `refresh_history` (the gate's history phase), and the
  gated CLI/MCP report builders. Raw evidence lives in `git_commits` /
  `git_commit_files`; only `CO_CHANGED_WITH` edges enter the graph (D25).
- `repobrain/memory.py` — M15 append-first memory, deterministic anchor
  extraction, pure verification annotations, Git rename fallback, and stable
  plain/JSON report assembly. CLI/MCP/brief layers reuse this shared path.
- `repobrain/testing/` — **new**: scale-hardening-only tooling, not shipped
  runtime code. `synthetic_repo.py` generates a deterministic large corpus
  with known graph answers; `perf.py` counts SQL statements via
  `sqlite3.Connection.set_trace_callback` for hardware-independent work
  assertions. Used by `tests/test_scale.py` and `scripts/benchmark_scale.py`.

## Important Files

- `prd.md` — full spec; milestone plan in section 26.
- `DECISIONS.md` — D1–D19. D14–D19 cover the M3 choices (DEFINES vs CONTAINS,
  import resolution, CALLS confidence ladder, repo-global EnvVars, TestFile/
  TestCase shape, language tiers). Read D14–D16 before touching edges.
- `tests/fixtures/small_python_app/`, `node_api_app/` (**new**, PRD 27.2:
  Express-style routes/service/config + `.env.example` + jest-style test +
  package.json), `markdown_docs_app/`.

## Recent Changes

- Markdown nodes now store structured link and inline-code reference candidates.
- Exact local paths and unambiguous symbols become source-grounded `MENTIONS`
  edges with confidence and inference provenance.
- Added `tests/test_doc_code_mapping.py` covering forward/reverse traversal,
  section provenance, ambiguity, and incremental convergence.
- Added a self-hosting quality gate that indexes RepoBrain itself and verifies
  symbol search, file explanation, doc/code traversal, and no-change
  incremental behavior.
- Added a complex lifecycle scenario covering ambiguity, target-side changes,
  delayed documentation updates, stale-edge cleanup, and incremental
  convergence, plus a roadmap-wide evaluation strategy. 73 tests total.

## Decisions

See `DECISIONS.md` D24 for M13 change context, D25 for M14 Git history,
D26 for M15 memory verification, D27 for distribution, D28 for runtime
adapters, D29 for the protocol-level MCP boundary, and D30 for scale
hardening (touch_paths/finish_run batching, and which repository-wide costs
are deliberately left unbatched).

## Assumptions

- One repository per database; paths relative to the indexed root.
- The scanned-file set passed to `begin_run` is the complete universe for
  import resolution (files excluded by ignore rules are "external").
- Cross-file CALLS precision: a name-only match must be globally unique.
- EnvVar nodes with `path=""` are never removed by path-based cleanup; a
  reader's deletion removes only its READS_ENV edges.

## Open Questions

- Should INSTANTIATES edges be emitted for `ClassName()` calls (currently
  skipped for precision)?
- Should orphaned EnvVar nodes (no remaining READS_ENV edges) be swept, or
  left for M5 config parsing to reclaim?
- Go/Java internal import resolution (needs go.mod / package-root awareness).

## Known Pitfalls

- The language pack's Python grammar inlines `expression_statement`, so
  module-level assignments match `(module (assignment))` — the query includes
  both shapes. If a grammar bump changes node shapes, tests in
  `test_code_parser.py` will catch it.
- Inferred (name-match) CALLS edges are only computed for files parsed in the
  current run; an unchanged caller won't link to a newly added callee until it
  changes or a `--no-incremental` run happens.
- `finish_run` must run inside the index transaction and **before**
  `delete_orphan_edges` (dangling import-qualified targets rely on the sweep).
- Runtime adapters must run after `finish_run` and before Markdown/orphan
  cleanup. Bump `RUNTIME_ADAPTER_VERSION` whenever adapter output shape or
  reconciliation semantics change, or fresh legacy databases will not backfill.
- Express registrations with middleware/multiple callbacks and dynamic
  callback expressions are deliberately unresolved. SQLAlchemy operations are
  skipped unless one exact model binding maps to one literal `__tablename__`.
- pytest `norecursedirs = ["fixtures"]` keeps fixture apps' own tests out of
  collection — don't remove.
- Never leave `.repobrain/` dirs inside `tests/fixtures/*` after manual CLI
  runs (conftest ignores them when copying, but keep the tree clean).
- The M11 freshness check is deliberately stat-only. As with incremental
  indexing (D12), a same-size edit whose mtime is restored can evade it.
- Brief budgets are approximate, not model-tokenizer exact. Facts are never
  cut mid-item, so a very small budget may omit whole lower-priority sections.
- M12 counts the full current size of added/changed files and the last indexed
  size of deleted files toward its byte threshold; it does not compute byte
  deltas. Queries over either threshold deliberately fail closed.
- M13 captures Git state before M12 auto-indexing. Deleted nodes and their
  graph edges are gone afterward, so deleted symbol spans come from the old
  Git blob and deleted impact relationships are reported as unavailable.
- Stale-doc candidates are review evidence, not semantic claims. Live targets
  use MENTIONS edges; renamed/deleted paths use only exact structured path
  references because their live MENTIONS edge necessarily disappears.
- `CO_CHANGED_WITH` edges carry `path=""` so path-based cleanup never touches
  them; removal happens via the extractor-owned rebuild and the orphan-edge
  sweep. Don't give them a real path.
- `refresh_history` compares HEAD **and** the `history_params` meta stamp
  (window, file cap, include/exclude patterns, ignore-file digest, extractor
  version). Bump `EXTRACTOR_VERSION` in
  `repobrain/history.py` whenever extraction or scoring output changes shape,
  or upgraded databases will keep serving old-shaped history as "current".
- File node ids are `node_id("File", path, path)` — `Node.id` prefers
  `qualified_name` (the full path) over `name` (the basename). Building File
  ids from basenames silently creates edges that join to nothing. Use
  `file_node_id(path)` rather than re-deriving this invariant.
- `CO_CHANGED_WITH.metadata.supporting_commits` is intentionally capped at
  the 20 newest SHAs. `supporting_commits_truncated=true` says the display
  sample is incomplete; the full intersection remains derivable from
  `git_commit_files` and must not be inferred from the capped list.
- History staleness never blocks static queries: with `--no-auto-index` the
  gate reports history `stale` and only history-backed answers fail closed.
  Non-Git and shallow repositories are `unavailable`, not errors.
- History extraction errors are not persisted as permanent cache entries:
  Git timeouts and filesystem failures can be transient, so the next gated
  read retries and may recover at the same HEAD.
- Memory anchors are write-time observations, not semantic truth. Validation
  checks graph identity/existence and movement only; it cannot decide whether
  unanchored prose remains conceptually correct.
- Verification must stay a pure read. Never rewrite stored `metadata_json` or
  Markdown with verdicts; unchanged graph state must produce identical output.
- Rename drift requires unambiguous `git_commit_files` continuity. Without
  history, a missing old identity is invalidated with honest unavailable
  evidence rather than guessed from a similar name.
- Non-UTF-8 Git metadata is escaped injectively into a SQLite-safe NUL-tagged
  hex representation. Valid Unicode remains unchanged so history paths retain
  graph identity; distinct malformed paths/authors must never collapse.
- Treat any existing `mcpServers.repobrain` value, including JSON `null`, as a
  conflict unless it exactly matches the generated entry. Absence and a falsey
  user value are not equivalent.
- Persisted agent commands must not capture `sys.executable` from `uvx`; that
  interpreter lives in a disposable cache. Use the stable `uvx` invocation and
  retain recognition of the exact legacy interpreter command during migration
  and uninstall.
- `GraphStore.touch_paths` batches by identical timestamp into chunked
  `WHERE path IN (...)` statements; it relies on every path in one call
  getting the same `now()` value. Don't split it back into a per-row loop
  without re-checking the no-change-run statement-count regression test in
  `tests/test_scale.py`.
- `CodeParser.finish_run`'s per-name candidate exclusion is path-only (not
  path-or-id) on purpose: a node's id embeds its path (D2), and a pending
  call's caller always lives in that same path, so excluding by path alone
  is equivalent to the old combined filter — don't "restore" an id check,
  it's redundant and was only ever removed for clarity, not correctness.
  Its per-path match-count arithmetic assumes `total - path_counts[path]`
  correctly represents "candidates outside this path"; if a future change
  needs the actual excluded rows (not just the count) for some new reason,
  recompute from `rows`, don't try to reverse-engineer them from the count.
- `repobrain/testing/` is test/benchmark-only tooling, not part of the
  shipped package surface — don't import it from `repobrain/` runtime code,
  and don't let its synthetic fixtures' guaranteed-unique naming scheme
  stand in for a fixture that exercises name collisions (see
  `tests/test_code_parser.py`'s dedicated ambiguity tests for that).

## Suggested Next Steps

A ready-to-use prompt for the next session (scoped to protocol-level MCP
integration tests, the last remaining engineering follow-up below) is in
`docs/NEXT_SESSION_PROMPT.md`.

### Product direction (post-MVP review, 2026-07-10)

The MVP measures graph quality, but the PRD's product goals (§6.2) are about
agent outcomes: fewer tokens rediscovering structure, safer changes, durable
knowledge. The biggest remaining risks are that agents won't call the tools,
the graph goes stale, and memory rots. Recommended next milestones, in
priority order:

1. **M11 — Session-start briefing (push, don't pull).** **Delivered.** Agents don't call MCP
   tools unprompted. Add `repobrain brief --budget N` emitting a token-budgeted
   orientation pack (purpose, subsystems, entrypoints, active assumptions,
   open questions, recent memory), injectable via a Claude Code SessionStart
   hook or generated CLAUDE.md section. Zero agent behavior change required;
   makes the "fewer tokens" goal measurable.
2. **M12 — Freshness automation.** **Delivered.** A stale brain misleads and destroys trust.
   Add a staleness check on every query (size+mtime diff is already cheap),
   auto-reindex-on-query for small diffs, and optional git post-commit /
   post-merge hooks. The brief in M11 must never be served stale.
3. **M13 — Diff-aware change context.** **Delivered.** The agent's unit of work is a change,
   not a lookup. `repobrain change-context` over the working diff/branch:
   impacted symbols, tests to run, and — the differentiator — **stale-doc
   detection** using existing MENTIONS edges (code changed, referencing doc
   section didn't). Natural pre-commit/PR hook surface.
4. **Git history as an extractor.** **Delivered (M14).** Co-change coupling catches impact
   relationships static analysis misses (templates, migrations, config);
   blended into impact analysis and change context. Churn hotspots and
   observed ownership shipped too; commit-message mining deferred.
5. **Memory verification.** **Delivered (M15).** Anchor memory entries to graph nodes and validate
   on reindex ("decision references `create_user`, which no longer exists →
   flag stale"). Turns append-only memory into a validated knowledge base and
   feeds the M11 brief ("2 assumptions invalidated since last session").
6. **Distribution.** **Delivered.** `uvx repobrain` packaging plus `repobrain install-agent`
   writing `.mcp.json`, the CLAUDE.md snippet, and hooks in one step. Adoption
   friction matters more than a ninth language.

Deliberate non-goals for now: embeddings and multi-repo support — keep the
deterministic-first stance (D-series) until the delivery loop above proves out.

### Engineering follow-ups (carried over)

1. **Framework/runtime adapters. Delivered (M16).** Precise Flask-style and
   Express handlers plus conservative exact-model SQLAlchemy table flow.
2. **Protocol-level MCP integration tests. Delivered.** `tests/test_mcp_
   transport.py` already launches the real stdio server with the official
   MCP client (initialize/discovery/reads/error envelopes) alongside direct
   tool tests; confirmed present while reviewing this list, not net-new.
3. **Profile indexing and traversal on repositories above 1,000 files.
   Delivered (scale hardening).** See D30 and `docs/SCALE_BENCHMARKS.md`.

All three prior engineering follow-ups are now delivered. Next up, per the
still-open M3 question below: Go/Java internal import resolution. See
`docs/NEXT_SESSION_PROMPT.md`.

## Source-Grounded Notes

- Acceptance verified: `repobrain index tests/fixtures/small_python_app` then
  `find-symbol create_user --path …` and `explain file
  app/services/user_service.py --path …` return grounded output (symbols with
  qualified names + line spans, imports/imported-by, callers at 0.9
  confidence, DATABASE_URL env read, `tests/test_users.py` via imports).
  Same for `node_api_app` with `createUser` / `src/config.js` (PORT,
  DATABASE_URL, LOG_LEVEL env reads; TestCases calling the service).
- 208/208 pytest tests pass (`.venv/bin/pytest -q`).

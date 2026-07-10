# Agent Handoff

## Project Summary

RepoBrain: a local-first second brain for AI coding agents (see `prd.md`).
All ten PRD milestones and post-MVP Milestones 11–12 are now implemented. Milestones 1–4 delivered storage,
incremental indexing, code/docs parsing, search, and documentation mapping.
Milestones 5–10 add config adapters and tracing, route/data-flow analysis,
impact analysis, MCP tools, durable agent memory, and Markdown/HTML reports.

## Delivery Status

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
- `repobrain/agent_install.py` — conservative merge of the RepoBrain
  SessionStart hook plus a marker-owned CLAUDE.md snippet.

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

See `DECISIONS.md` D23 for the M12 session.

## Assumptions

- One repository per database; paths relative to the indexed root.
- The scanned-file set passed to `begin_run` is the complete universe for
  import resolution (files excluded by ignore rules are "external").
- Cross-file CALLS precision: a name-only match must be globally unique.
- EnvVar nodes with `path=""` are never removed by path-based cleanup; a
  reader's deletion removes only its READS_ENV edges.

## Open Questions

- Should module-level JS route callbacks become Route/Endpoint nodes in M6 so
  CALLS sources are more precise than the Module node?
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

## Suggested Next Steps

A ready-to-use prompt for the next session (scoped to M13) is in
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
3. **M13 — Diff-aware change context.** The agent's unit of work is a change,
   not a lookup. `repobrain change-context` over the working diff/branch:
   impacted symbols, tests to run, and — the differentiator — **stale-doc
   detection** using existing MENTIONS edges (code changed, referencing doc
   section didn't). Natural pre-commit/PR hook surface.
4. **Git history as an extractor.** Co-change coupling catches impact
   relationships static analysis misses (templates, migrations, config);
   blend it into impact analysis. Also churn hotspots, ownership, and
   commit-message mining. Local and deterministic — on-vision.
5. **Memory verification.** Anchor memory entries to graph nodes and validate
   on reindex ("decision references `create_user`, which no longer exists →
   flag stale"). Turns append-only memory into a validated knowledge base and
   feeds the M11 brief ("2 assumptions invalidated since last session").
6. **Distribution.** `uvx repobrain` packaging plus `repobrain install-agent`
   writing `.mcp.json`, the CLAUDE.md snippet, and hooks in one step. Adoption
   friction matters more than a ninth language.

Deliberate non-goals for now: embeddings and multi-repo support — keep the
deterministic-first stance (D-series) until the delivery loop above proves out.

### Engineering follow-ups (carried over)

1. Add framework adapters for dynamic receiver calls and richer ORM/table flow.
2. Profile indexing and traversal on repositories above 1,000 files.
3. Add protocol-level MCP integration tests in addition to direct tool tests.

## Source-Grounded Notes

- Acceptance verified: `repobrain index tests/fixtures/small_python_app` then
  `find-symbol create_user --path …` and `explain file
  app/services/user_service.py --path …` return grounded output (symbols with
  qualified names + line spans, imports/imported-by, callers at 0.9
  confidence, DATABASE_URL env read, `tests/test_users.py` via imports).
  Same for `node_api_app` with `createUser` / `src/config.js` (PORT,
  DATABASE_URL, LOG_LEVEL env reads; TestCases calling the service).
- 63/63 pytest tests pass (`.venv/bin/pytest -q`).

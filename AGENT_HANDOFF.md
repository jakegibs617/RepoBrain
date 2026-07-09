# Agent Handoff

## Project Summary

RepoBrain: a local-first second brain for AI coding agents (see `prd.md`).
Milestones 1–2 delivered the package skeleton, SQLite graph storage, scanner,
incremental indexing, Markdown parser, FTS5 search, and the `init` / `index` /
`status` / `search` CLI. **This session delivered Milestone 3**: tree-sitter
code symbol parsing, import/call/env-read edges, test-file detection, the
`find-symbol` and `explain file` commands, a reusable graph-query module, and
the `node_api_app` fixture.

## Current Architecture Understanding

- `repobrain/graph/schema.py` — full NodeType/EdgeType enums, `Node`/`Edge`/
  `FtsRow` dataclasses, deterministic sha1 id helpers (D2).
- `repobrain/graph/store.py` — `GraphStore`: WAL SQLite, tables `nodes`,
  `edges`, `files`, `index_runs`, `meta` (repo-root pin), FTS5
  `content_fts(path, name, content, node_id UNINDEXED)`.
- `repobrain/graph/queries.py` — **new**: reusable traversals returning plain
  dicts (`find_symbol`, `explain_file`, `resolve_file_path`). The CLI is a
  thin renderer over these; M8 MCP tools should call them directly.
- `repobrain/indexing/indexer.py` — scan → diff → parse → single-transaction
  store. Two optional parser hooks (duck-typed via `getattr`):
  `begin_run(known_paths)` before parsing (import resolution needs the full
  scanned file set) and `finish_run(store) -> list[Edge]` inside the
  transaction after upserts (cross-file name-match CALLS), before the orphan
  edge sweep — which also removes any deterministically-computed edge targets
  that turned out not to exist.
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

## Important Files

- `prd.md` — full spec; milestone plan in section 26.
- `DECISIONS.md` — D1–D19. D14–D19 cover the M3 choices (DEFINES vs CONTAINS,
  import resolution, CALLS confidence ladder, repo-global EnvVars, TestFile/
  TestCase shape, language tiers). Read D14–D16 before touching edges.
- `tests/fixtures/small_python_app/`, `node_api_app/` (**new**, PRD 27.2:
  Express-style routes/service/config + `.env.example` + jest-style test +
  package.json), `markdown_docs_app/`.

## Recent Changes

- Added `tree-sitter` + `tree-sitter-language-pack` dependencies.
- New code parser + indexer hooks as described above.
- `tests/test_code_parser.py` (extraction, imports, calls, env convergence,
  JS/PHP/Bash, broken-file degradation) and `tests/test_queries.py`
  (find_symbol, explain_file); `node_app` fixture in conftest. 63 tests total.

## Decisions

See `DECISIONS.md` D14–D19 for this session.

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

## Suggested Next Steps

1. **Milestone 4**: doc-to-code purpose mapping — the Markdown parser already
   stores links/code blocks in section metadata; emit MENTIONS edges and the
   `explain file` "Referencing docs" section will light up automatically.
2. **Milestone 5**: YAML/config adapters + `trace config` — EnvVar nodes are
   already repo-global, so `.env.example`/compose definitions can attach
   SETS_ENV/DECLARES_CONFIG edges to the same nodes the code parser reads.
3. Consider a `trace_symbol`-style query in `graph/queries.py` (callers +
   callees + tests at depth N) ahead of the M8 MCP tools.

## Source-Grounded Notes

- Acceptance verified: `repobrain index tests/fixtures/small_python_app` then
  `find-symbol create_user --path …` and `explain file
  app/services/user_service.py --path …` return grounded output (symbols with
  qualified names + line spans, imports/imported-by, callers at 0.9
  confidence, DATABASE_URL env read, `tests/test_users.py` via imports).
  Same for `node_api_app` with `createUser` / `src/config.js` (PORT,
  DATABASE_URL, LOG_LEVEL env reads; TestCases calling the service).
- 63/63 pytest tests pass (`.venv/bin/pytest -q`).

# Agent Handoff

## Project Summary

RepoBrain: a local-first second brain for AI coding agents (see `prd.md`).
This session delivered **Milestones 1 and 2**: package skeleton, SQLite graph
storage, file scanner, hashing + incremental indexing, generic file parser,
Markdown parser, FTS5 keyword search, and the `init` / `index` / `status` /
`search` CLI.

## Current Architecture Understanding

- `repobrain/graph/schema.py` — full NodeType/EdgeType enums (all PRD 11.1/11.2
  values), `Node`/`Edge`/`FtsRow` dataclasses, deterministic sha1 id helpers.
- `repobrain/graph/store.py` — `GraphStore`: WAL SQLite, tables `nodes`,
  `edges`, `files`, `index_runs`, FTS5 `content_fts(path, name, content)`.
  Batch upserts via executemany; `delete_paths` wipes nodes/edges/FTS by
  provenance path.
- `repobrain/indexing/scanner.py` — walk + ignore rules (fnmatch gitignore
  subset, PRD 17 defaults), language detection, binary sniff, 2 MB cap.
- `repobrain/indexing/hasher.py` — sha256. `incremental.py` — diffs scan
  against the `files` table into added/changed/unchanged/deleted.
- `repobrain/indexing/indexer.py` — scan → diff → parse → single-transaction
  store; stamps commit hash (`git rev-parse HEAD`) on all rows; records
  `index_runs`; sweeps dead Directory nodes and orphan edges.
- `repobrain/parsers/base.py` — `Parser` interface, `ParseResult(nodes, edges,
  warnings, fts_rows)`, `GenericFileParser`, registry. All matching parsers
  run per file (a .md file gets both File and MarkdownDocument nodes).
- `repobrain/parsers/markdown_parser.py` — markdown-it-py based; nested
  sections with line spans, links/code-blocks in metadata, TODO/FIXME Task
  nodes, per-section FTS rows.
- `repobrain/retrieval/keyword.py` — bm25 + exact-name (+100) / partial-name
  (+25) / path (+10) boosts; results carry path, lines, type, snippet, score,
  reasons.
- `repobrain/cli.py` — click CLI; `--json` on status/search.

## Important Files

- `prd.md` — the full product spec; milestone plan in section 26.
- `DECISIONS.md` — D1–D9 explain the non-obvious choices (IDs, FTS join,
  directory cleanup). Read before changing storage or search.
- `tests/fixtures/small_python_app/`, `tests/fixtures/markdown_docs_app/` —
  fixture repos; tests copy them to tmp dirs (see `tests/conftest.py`).

## Recent Changes

Initial implementation — everything listed above is new in this slice.

## Decisions

See `DECISIONS.md` (D1–D9).

## Assumptions

- One repository per database; paths stored relative to the indexed root.
- `repobrain` CLI runs from the directory that owns `.repobrain/` (the index
  target can be elsewhere).
- Observed facts get confidence 1.0; nothing inferred yet, so no edge has
  `is_inferred=1` (the columns and dataclass fields exist and are tested).

## Open Questions

- Should FTS gain an UNINDEXED `node_id` column instead of the `(path, name)`
  join back to nodes? (Deviation was avoided; join is heuristic.)
- Should Directory nodes survive with `last_seen` semantics instead of being
  swept when their last file disappears?
- When tree-sitter lands (M3), should `Module` nodes replace or complement the
  generic `File` node for source files?

## Known Pitfalls

- Gitignore matcher skips `!negation` lines silently and reads only the scan
  root's `.gitignore` — nested gitignores are ignored.
- `content_fts` rows for a file whose basename equals another node's name
  resolve by type preference (File first) — see keyword.py comment.
- Whole-file re-index reads every file into memory one at a time; fine for
  ~1k files, revisit streaming for huge repos.
- pytest is configured with `norecursedirs = ["fixtures"]` so the fixture
  app's own tests are not collected — don't remove that.

## Suggested Next Steps

1. **Milestone 3**: tree-sitter code parsing (Function/Class/Module nodes,
   DEFINES/IMPORTS edges), `find-symbol` CLI, symbol-aware search boost.
2. **Milestone 4**: doc-to-code matching using the link/code-block/backtick
   data the Markdown parser already stores in section metadata.
3. YAML parser + ConfigKey/EnvVar extraction (Milestone 5) — fixture
   `small_python_app` already reads `DATABASE_URL` for `trace config`.

## Source-Grounded Notes

- Acceptance verified: `repobrain init && repobrain index
  tests/fixtures/small_python_app && repobrain status` works; `repobrain
  search "database"` returns README section + `app/db/config.py` with
  snippets; 24/24 pytest tests pass.

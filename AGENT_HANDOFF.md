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
  `edges`, `files`, `index_runs`, `meta` (repo-root pin), FTS5
  `content_fts(path, name, content, node_id UNINDEXED)`. Batch upserts via
  executemany; `delete_paths` wipes nodes/edges/FTS by provenance path.
- `repobrain/indexing/scanner.py` — walk + ignore rules (fnmatch gitignore
  subset, PRD 17 defaults), language detection, binary sniff, 2 MB cap.
- `repobrain/indexing/hasher.py` — sha256. `incremental.py` — diffs scan
  against the `files` table into added/changed/unchanged/deleted with a
  size+mtime shortcut (unchanged stat → no read/hash); reads content only for
  files needing parsing; unreadable files become warnings, never deletions.
- `repobrain/indexing/indexer.py` — scan → diff → parse → single-transaction
  store; pins the DB to its repo root (`RepoRootMismatchError` on mismatch);
  stamps commit hash (`git rev-parse HEAD`) on all rows; records
  `index_runs`; sweeps dead Directory nodes and orphan edges.
- `repobrain/parsers/base.py` — `Parser` interface, `ParseResult(nodes, edges,
  warnings, fts_rows)`, `GenericFileParser`, registry. All matching parsers
  run per file (a .md file gets both File and MarkdownDocument nodes).
- `repobrain/parsers/markdown_parser.py` — markdown-it-py based; nested
  sections with line spans, links/code-blocks in metadata, TODO/FIXME Task
  nodes, per-section FTS rows.
- `repobrain/retrieval/keyword.py` — bm25 + exact-name (+100) / partial-name
  (+25, LIKE with `%`/`_` escaped) / path (+10) boosts, each applied at most
  once per result; results are keyed by node id via `content_fts.node_id` and
  carry path, lines, type, snippet, score, reasons.
- `repobrain/cli.py` — click CLI. All commands operate on the target repo's
  own `.repobrain/`: `init [PATH]`, `index [PATH]`, `status/search --path`.
  `--json` on status/search.

## Important Files

- `prd.md` — the full product spec; milestone plan in section 26.
- `DECISIONS.md` — D1–D13 explain the non-obvious choices (IDs, FTS design,
  root pinning, stat shortcut). Read before changing storage or search.
- `tests/fixtures/small_python_app/`, `tests/fixtures/markdown_docs_app/` —
  fixture repos; tests copy them to tmp dirs (see `tests/conftest.py`).

## Recent Changes

- Initial implementation of Milestones 1–2 (everything listed above).
- Code review fixes (same session): database pinned to its repo root via a
  `meta` table (fixes a graph-purge bug when indexing a second root);
  `content_fts` gained `node_id UNINDEXED`; size+mtime shortcut before
  hashing; unreadable files skipped with warnings; anchored dir ignore
  patterns honored; name boost deduped per result; LIKE wildcards escaped.

## Decisions

See `DECISIONS.md` (D1–D13; D8 is superseded by D10, D4 amended by D11).

## Assumptions

- One repository per database; paths stored relative to the indexed root
  (enforced via the `meta` root pin, not just assumed).
- Observed facts get confidence 1.0; nothing inferred yet, so no edge has
  `is_inferred=1` (the columns and dataclass fields exist and are tested).
- If a file's size and mtime both match the stored row, its content is
  unchanged (same trust model as git's index).

## Open Questions

- Should Directory nodes survive with `last_seen` semantics instead of being
  swept when their last file disappears?
- When tree-sitter lands (M3), should `Module` nodes replace or complement the
  generic `File` node for source files?
- Should `delete_paths` switch to node_id-targeted FTS deletes once parsers
  emit sub-file nodes that can change independently?

## Known Pitfalls

- Gitignore matcher skips `!negation` lines silently, reads only the scan
  root's `.gitignore`, and fnmatch's `*` crosses `/` (so `docs/*` also
  matches `docs/a/b.md`) — nested gitignores are ignored.
- Pre-review databases (content_fts without node_id) are dropped/recreated on
  open; run `repobrain index --no-incremental` afterwards to repopulate FTS.
- The stat shortcut misses a same-length edit whose mtime is also restored;
  `--no-incremental` forces a full re-hash.
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

- Acceptance verified after review fixes: `repobrain init && repobrain index
  tests/fixtures/small_python_app && repobrain status --path ... && repobrain
  search "database" --path ...` works (database now lives inside the fixture
  root); indexing a subdirectory creates its own database and leaves the
  root's untouched; 32/32 pytest tests pass.

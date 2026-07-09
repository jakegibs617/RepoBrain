# RepoBrain

RepoBrain is a **local-first "second brain" for AI coding agents**. It indexes a
software project into a durable, queryable SQLite graph spanning source code,
Markdown documentation, and (eventually) config files and runtime wiring — so
an agent can re-enter a repository and immediately know what it is, where
things live, and what connects to what.

Everything runs offline. No API keys, no network calls.

## Current status (Milestones 1–2)

Implemented:

- Python package, CLI, and SQLite storage (WAL mode) under `.repobrain/`
- Graph schema with the full node/edge type vocabulary from the PRD,
  deterministic sha1 node/edge IDs, and provenance on every row
  (path, span, extractor, confidence, commit hash, timestamps)
- File scanner with `.gitignore` / `.repobrainignore` support (fnmatch-based
  subset), default excludes, binary sniffing, and a 2 MB size cap
- Incremental indexing: sha256 content hashes; unchanged files are never
  re-parsed; changed/deleted files have their nodes, edges, and FTS rows
  removed before re-adding
- Generic file parser (File + Directory nodes, CONTAINS edges, full-text rows)
- Markdown parser (markdown-it-py): MarkdownDocument and nested
  MarkdownSection nodes with line spans, links, fenced code blocks, and
  TODO/FIXME list items as Task nodes
- Keyword search: FTS5 bm25 ranking combined with exact-name and path boosts

Not yet implemented: code symbol parsing (tree-sitter), YAML/config adapters,
data-flow and impact analysis, the MCP server, agent memory, and reports.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv .venv
uv pip install -p .venv/bin/python -e ".[dev]"
```

## Usage

```bash
# create .repobrain/ with a default config inside PATH (default: cwd)
.venv/bin/repobrain init
.venv/bin/repobrain init tests/fixtures/small_python_app

# index a repository (incremental by default; --no-incremental to force).
# The database lives inside the indexed root: PATH/.repobrain/repobrain.sqlite
.venv/bin/repobrain index .
.venv/bin/repobrain index tests/fixtures/small_python_app

# last run stats plus node/edge counts by type (--json for machine output);
# --path picks which repository's database to inspect (default: cwd)
.venv/bin/repobrain status
.venv/bin/repobrain status --path tests/fixtures/small_python_app

# full-text + name search (--path DIR, --limit N, --type NodeType, --json)
.venv/bin/repobrain search "database" --path tests/fixtures/small_python_app
.venv/bin/repobrain search "users" --type File --json
```

Each database is pinned to the repository root it indexes (stored in a `meta`
table). Asking a database to index a different root fails with a clear error
instead of silently purging the previous graph.

Example search output:

```text
1. README.md:12-17  [MarkdownSection]  score=101.55
   name: Database   reason: full-text match, exact name match
   ## [Database] The [database] connection is configured in `app/db/config.py`…
```

## How it works

- `repobrain index PATH` scans `PATH` and diffs against the `files` table.
  Files whose size and mtime match the stored row are trusted without being
  read; otherwise the file is sha256-hashed, and only added/changed files are
  re-parsed. Stale nodes/edges/FTS rows are deleted first, so re-runs are
  idempotent. Unreadable files are skipped with a warning, never fatally.
- Every parser returns nodes, edges, warnings, and full-text rows. Node IDs
  are sha1 over `(type, qualified_name, path)`, so re-indexing an unchanged
  entity converges on the same row.
- Search queries the `content_fts` FTS5 table with bm25 ranking and layers
  exact-name (+100), partial-name (+25), and path-substring (+10) boosts on
  top, so source-grounded exact matches outrank vague content matches.

## Configuration

`.repobrain/config.json` (created by `repobrain init`):

```json
{
  "db_path": ".repobrain/repobrain.sqlite",
  "include_patterns": [],
  "exclude_patterns": [],
  "max_file_size_bytes": 2097152
}
```

## Tests

```bash
.venv/bin/pytest
```

Tests copy the fixture repos in `tests/fixtures/` into temp directories, so
they never mutate the checked-in fixtures.

## Limitations

- Gitignore support is a simple fnmatch-based subset: no `!negation`, no
  nested `.gitignore` files in subdirectories, and no true gitignore `*`/`**`
  semantics — fnmatch's `*` crosses `/`, so a pattern like `docs/*`
  over-matches nested paths such as `docs/a/b.md` (real gitignore would only
  match direct children).
- Paths are stored relative to the indexed root; one repository per database
  (enforced: the database is pinned to its root and refuses other roots).
- Incremental change detection trusts size+mtime: a same-length edit that
  also restores the file's mtime is missed until a `--no-incremental` run
  (the same trade-off git's index makes).
- Only Markdown gets structural parsing so far; other text files are indexed
  whole-file for full-text search.
- Empty directories produce no Directory nodes (directories are derived from
  file paths).

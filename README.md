# RepoBrain

RepoBrain is a **local-first "second brain" for AI coding agents**. It indexes a
software project into a durable, queryable SQLite graph spanning source code,
Markdown documentation, and (eventually) config files and runtime wiring — so
an agent can re-enter a repository and immediately know what it is, where
things live, and what connects to what.

Everything runs offline. No API keys, no network calls.

## Current status (Milestones 1–10 complete)

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
- Tree-sitter code parser: Module/Function/Class/Method/Variable nodes with
  qualified names and line spans, DEFINES/CONTAINS/IMPORTS/CALLS/READS_ENV
  edges, TestFile/TestCase detection, and symbol names in full-text search
- `find-symbol` and `explain file` CLI commands backed by reusable graph
  queries (`repobrain/graph/queries.py`)
- Markdown-to-code purpose mapping: local links and backticked file/symbol
  references become source-grounded `MENTIONS` edges; ambiguous symbol names
  are deliberately skipped
- Bidirectional `docs-for-code` and `code-for-docs` queries, also surfaced in
  the "Referencing docs" section of `explain file`
- YAML and dotenv parsing with GitHub Actions, Docker Compose, and Kubernetes
  adapters; config definitions connect to code-level environment reads
- HTTP route extraction, grounded route-to-handler edges, data-flow tracing,
  and confidence-bucketed impact analysis
- A local FastMCP server exposing all 13 core tools
- Append-only structured agent memory mirrored into Markdown handoff files
- Grounded project overviews and Markdown/HTML graph reports

The ten-milestone MVP is implemented. Dynamic dispatch and framework-specific
runtime wiring remain intentionally conservative; see Limitations.

### Supported languages (code parsing)

| Language   | Symbols | Internal import resolution | Calls | Env reads |
|------------|---------|----------------------------|-------|-----------|
| Python     | yes     | yes (dotted path → file, incl. relative imports) | same-file, self.method, import-qualified, name-match | `os.environ[...]`, `os.environ.get`, `os.getenv` |
| JavaScript | yes     | yes (relative `import`/`require`, extension + `index.*` inference) | same-file, this.method, import-qualified, name-match | `process.env.X`, `process.env["X"]` |
| TypeScript | yes (+interfaces/enums as Class) | same as JavaScript | same as JavaScript | same as JavaScript |
| PHP        | yes     | `require`/`include` with literal relative paths | same-file, `$this->method` | `getenv('X')` |
| Bash       | functions + top-level variables | no | same-file function invocations | no |
| Go         | yes (structs/types as Class) | no (imports recorded as metadata) | same-file | no |
| Java       | yes (interfaces/enums as Class) | no (imports recorded as metadata) | within-class | no |
| Ruby       | yes (modules as Class) | `require_relative` | same-file, within-class, name-match | no |

Unresolvable or third-party imports are stored as `external_imports` metadata
on the module node — never as dangling graph nodes.

## Install

Prefer a visual walkthrough? Open [`setup/index.html`](setup/index.html) in your browser for the interactive setup guide.

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

# source-grounded session orientation (plain text or JSON)
# --budget uses the deterministic ceil(characters / 4) token estimate
.venv/bin/repobrain brief --budget 2000
.venv/bin/repobrain brief --budget 800 --json

# every read-only query auto-reindexes small stale diffs before serving facts;
# --no-auto-index performs a non-mutating check and refuses stale reads
.venv/bin/repobrain search "database" --no-auto-index

# install an idempotent Claude Code SessionStart hook and CLAUDE.md snippet
.venv/bin/repobrain install-agent .
.venv/bin/repobrain install-agent . --git-hooks
.venv/bin/repobrain uninstall-agent .

# full-text + name search (--path DIR, --limit N, --type NodeType, --json)
.venv/bin/repobrain search "database" --path tests/fixtures/small_python_app
.venv/bin/repobrain search "users" --type File --json

# find code symbols by name (--exact, --limit N, --json)
.venv/bin/repobrain find-symbol create_user --path tests/fixtures/small_python_app

# explain a file: symbols, imports/imported-by, callers/callees, env vars,
# related tests, referencing docs (--json for machine output)
.venv/bin/repobrain explain file app/services/user_service.py --path tests/fixtures/small_python_app

# navigate between documentation and implementation
.venv/bin/repobrain docs-for-code app/services/user_service.py --path tests/fixtures/small_python_app
.venv/bin/repobrain docs-for-code create_user --path tests/fixtures/small_python_app
.venv/bin/repobrain code-for-docs README.md --heading Architecture --path tests/fixtures/small_python_app

# trace config and runtime flow, then estimate change impact
.venv/bin/repobrain trace config DATABASE_URL --path tests/fixtures/small_python_app
.venv/bin/repobrain trace data-flow "POST /api/users" --path tests/fixtures/small_python_app
.venv/bin/repobrain impact app/services/user_service.py --path tests/fixtures/small_python_app

# grounded overview and human-readable reports
.venv/bin/repobrain explain project --json
.venv/bin/repobrain report

# durable agent memory
.venv/bin/repobrain memory write --summary "Implemented auth flow" --next-step "Add expiry tests"
.venv/bin/repobrain memory read --topic auth

# MCP (install the optional extra first)
uv pip install -p .venv/bin/python -e ".[mcp]"
.venv/bin/repobrain mcp --path .
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

Example `find-symbol` output:

```text
1. create_user  [Function]  app/services/user_service.py:7-10
   app.services.user_service.create_user   def create_user(payload):
2. create_user_route  [Function]  app/api/routes.py:7-8
   app.api.routes.register_routes.create_user_route   def create_user_route():
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
- Code files are parsed with tree-sitter (query objects compiled once per
  language and reused). Facts observed directly (definitions, imports,
  same-file calls) get confidence 0.9–1.0; cross-file calls matched only by a
  globally-unique name are marked `is_inferred` with confidence 0.7 and
  `inference_reason="name-match"`. A file that fails to parse degrades to a
  warning — it keeps its generic File node and full-text row.

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

### Dogfooding RepoBrain

RepoBrain's integration suite indexes this repository into a temporary
database and verifies that it can find its own symbols, explain internal
dependencies, connect architecture docs to implementation, and complete a
no-change incremental run without rewriting graph facts:

```bash
.venv/bin/pytest tests/test_self_hosting.py -v
```

For interactive inspection, build the gitignored local graph and query it:

```bash
.venv/bin/repobrain index .
.venv/bin/repobrain find-symbol MarkdownMentionReconciler --exact
.venv/bin/repobrain explain file repobrain/indexing/doc_references.py
.venv/bin/repobrain docs-for-code repobrain/indexing/doc_references.py
.venv/bin/repobrain code-for-docs AGENT_HANDOFF.md
```

The broader capability, adversarial, and whole-system evaluation approach is
documented in [`docs/EVALUATION_STRATEGY.md`](docs/EVALUATION_STRATEGY.md).

Export the current local graph for the interactive companion page:

```bash
.venv/bin/python scripts/export_graph_html.py
open setup/graph.html
```

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
- Markdown and the eight code languages above get structural parsing; other
  text files are indexed whole-file for full-text search.
- Call-graph extraction prefers precision over recall: method calls on
  dynamic receivers (anything other than `self`/`this`) are skipped, and
  cross-file name-only matches require the name to be globally unique.
- Incremental runs only re-parse changed files, so a new function in file A
  will not gain inferred CALLS edges from an unchanged caller in file B until
  B changes (or a `--no-incremental` run).
- Markdown mention matching is intentionally strict: exact local paths and
  exact unique symbol names are linked; fuzzy text, ambiguous symbols,
  external URLs, and route literals without a Route node are skipped.
- Go/Java imports are recorded as module metadata only (resolving them needs
  module/package roots, deferred).
- Empty directories produce no Directory nodes (directories are derived from
  file paths).

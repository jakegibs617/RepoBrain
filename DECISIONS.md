# Decisions

Design decisions made while implementing RepoBrain, per PRD sections 30/31.
Newest entries at the bottom.

## 2026-07-09 — Milestones 1–2 (skeleton, storage, search)

### D1: SQLite tables only, no graph library abstraction

The graph is plain `nodes` / `edges` tables in SQLite (WAL mode) with indexes
on `path`, `type`, `name`, and edge endpoints. No networkx/graph-DB layer.
Rationale: PRD recommends SQLite-only for MVP; every planned query (neighbors,
path lookup, type counts) is a simple indexed SQL query, and provenance lives
naturally in columns.

### D2: Deterministic content-addressed IDs

- Node id = `sha1(type \x00 qualified_name-or-name \x00 path)`
- Edge id = `sha1(type \x00 source_id \x00 target_id \x00 path \x00 start_line)`

Rationale: incremental reconciliation becomes idempotent upserts — re-parsing
an unchanged file regenerates identical IDs, so `INSERT ... ON CONFLICT`
converges instead of duplicating. `created_at` is preserved on conflict;
`updated_at`/`last_seen_at` refresh. Trade-off: renaming a heading or moving a
file produces a new ID (old row is deleted via path-based cleanup), so node
identity does not survive renames. Acceptable for MVP.

### D3: fnmatch-based gitignore subset

`.gitignore` and `.repobrainignore` are parsed with a small fnmatch-based
matcher: comments, blank lines, trailing-`/` directory patterns, leading-`/`
anchoring, and `*`/`?`/`[...]` globs. Negation (`!`), nested per-directory
`.gitignore` files, and true `**` semantics are NOT supported (lines starting
with `!` are skipped). Rationale: avoids a new dependency; the PRD's default
excludes cover the common noise. Revisit with `pathspec` if fidelity becomes a
problem.

### D4: FTS5 design — one contentful table, rows per file AND per section

*(Amended by D11: the table now carries a `node_id UNINDEXED` column.)*

`content_fts` is a regular (contentful) FTS5 table. The generic file parser
inserts one row per text file (name = basename, content = whole file); the
Markdown parser additionally inserts one row per section (name = heading,
content = section text). Sync strategy: on re-index, all FTS rows for
changed/deleted paths are deleted before re-inserting, inside the same
transaction as node/edge updates.

### D5: Parsers may stack; ParseResult carries FTS rows

The indexer runs every registered parser whose `can_parse` returns true, so a
Markdown file gets both the generic File node and the Markdown document/section
nodes. `ParseResult` gained a fourth field, `fts_rows`, beyond the PRD's
`(nodes, edges, warnings)` — parsers are the only place that knows which text
belongs in full-text search (e.g. per-section content), and the indexer just
flushes them.

### D6: Directory nodes derived from file paths; cleanup by liveness

Directory nodes/edges are emitted by the generic file parser from each file's
ancestor chain. Edge provenance paths point at the child (dir→file edge has
the file's path), so deleting a file removes exactly its edges. After
deletions, Directory nodes whose path no longer prefixes any active file are
swept, then orphan edges (dangling endpoints) are deleted globally.

### D7: Search scoring = -bm25 + additive boosts

FTS relevance is `-bm25()` (higher is better, typically 0–3), with additive
boosts: exact name match +100, partial name match +25, path substring +10.
Rationale: PRD 19 says source-grounded exact matches should outrank vague
content matches; the large exact-name boost guarantees that ordering without
tuning.

### D8: Store paths relative to the indexed root; db lives in CWD

*(SUPERSEDED by D10 — the CWD-based location allowed one database to receive
two different roots, whose colliding relative paths purged the graph.)*

### D9: Tasks limited to TODO/FIXME list items

Markdown Task nodes are created only for list items whose text (after
stripping a `[ ]`/`[x]` checkbox prefix) contains the word TODO or FIXME.
Plain checkboxes without TODO/FIXME are not tasks yet — prefer precision over
recall at first (PRD 30 #8).

## 2026-07-09 — Code review fixes

### D10: Database is pinned to the repository root it indexes (replaces D8)

`repobrain index PATH` opens/creates `PATH/.repobrain/repobrain.sqlite`;
`init`, `status --path`, and `search --path` locate the database the same
way. The resolved absolute root is recorded in a `meta` table on first index;
any later attempt to index a different root through the same database raises
`RepoRootMismatchError` instead of proceeding. Rationale: the previous
CWD-based scheme let one database receive scans of two different roots —
relative paths collided and `compute_diff` marked the whole previous graph as
deleted files, purging it. One repository per database is now enforced, not
just assumed. `.repobrain/` remains a gitignored local cache; Markdown memory
files stay committed.

### D11: content_fts carries `node_id UNINDEXED` (amends D4)

The FTS table is now `content_fts(path, name, content, node_id UNINDEXED)`.
Each FTS row records the id of the graph node its text came from, so search
attributes node type and line spans by primary-key lookup instead of the old
heuristic `(path, name)` join (which was ambiguous when a File and a
MarkdownDocument shared a basename), and future deletions can target a single
node's row. Adding the column now avoids an FTS rebuild when tree-sitter
symbol rows land in M3. Pre-release databases without the column are
dropped/recreated on open (a full re-index repopulates them).

### D12: Incremental diff trusts size+mtime before hashing

`compute_diff` skips reading/hashing any file whose size AND mtime match the
stored `files` row; content is read once, and only for files that may need
parsing. When a file's stat moves but its hash is unchanged, the stored stat
is refreshed so the shortcut works on the next run. Trade-off (documented in
README): a same-length edit that also restores mtime is missed until a
`--no-incremental` run — the same trade-off git's index makes. Unreadable
files are skipped with a warning and kept out of the deletion set, so a
transient I/O error can never purge a file's existing graph rows.

### D13: Anchored ignore patterns honored for directories; LIKE escaping

`/dist/` now only ignores a root-level `dist` directory, not `src/dist`
(previously anchoring was ignored for directory patterns). Known remaining
gap, documented rather than fixed: fnmatch's `*` crosses `/`, so `docs/*`
over-matches `docs/a/b.md`. Separately, `%`/`_` in search queries are escaped
in the partial-name LIKE clause so they match literally.

## 2026-07-09 — Milestone 3 (tree-sitter code symbols)

### D14: DEFINES for symbol definitions, CONTAINS for structural nesting

`File DEFINES Module`, `Module DEFINES Function/Class/Variable/TestCase`, and
`Class DEFINES Method` express "this scope declares this symbol". CONTAINS is
reserved for structural nesting that is not a scope-level declaration: a
function defined inside another function (`register_routes CONTAINS
create_user_route`), directories containing files (D6), Markdown section
nesting, and `TestFile CONTAINS TestCase`. Rationale: `DEFINES` answers "what
symbols does X declare?" cleanly, while CONTAINS remains a purely structural
axis; queries can traverse either without conflating them.

### D15: Import resolution against the scanned file set; externals are metadata

The code parser receives every scanned repo path via a `begin_run(known_files)`
hook the indexer calls before parsing (parsers without the hook are
unaffected). Python imports resolve dotted paths to `x/y.py` or
`x/y/__init__.py` (relative imports supported); JS/TS resolve relative
`import`/`require` specifiers with extension inference (`.js/.jsx/.ts/.tsx/
.mjs/.cjs`) including `index.*` files; Ruby resolves `require_relative`; PHP
resolves literal relative `require`/`include` paths. Resolved imports become
`Module IMPORTS Module` edges whose target id is computed deterministically
(D2) — no placeholder node needed. Anything unresolvable (stdlib, npm
packages, dynamic paths) lands in the module node's `external_imports`
metadata list, never as a dangling node. Go/Java imports are metadata-only for
now (resolution needs go.mod / package roots).

### D16: CALLS = observed 0.9, name-match inferred 0.7; Module may be a caller

Call edges favor precision over recall:

- Same-file bare calls, `self.method()` / `$this->method()` / `this.method()`
  (resolved strictly within the enclosing class), and calls resolved through
  an explicit import binding get confidence 0.9, `is_inferred=0`.
- Remaining bare-name calls are queued and resolved after the run's nodes are
  stored (indexer `finish_run(store)` hook): if exactly one Function/Method in
  the whole graph has that name, an edge with confidence 0.7, `is_inferred=1`,
  `inference_reason="name-match"` is created; ambiguous names create nothing.
- Method calls on dynamic receivers are skipped entirely.
- The PRD says "Function CALLS Function", but module-level calls (Express
  route callbacks, script bodies) are common, so the source may also be the
  Module node — the callee must still resolve to a known Function/Method.
- Import-qualified targets are computed as deterministic ids without checking
  existence; if the target symbol doesn't actually exist, the edge dangles and
  the existing orphan-edge sweep removes it in the same transaction.

Known incremental limitation (documented in README): inferred edges are only
(re)computed for files parsed in the current run.

### D17: EnvVar identity is repo-global (path = "")

`EnvVar` node ids are keyed on `("EnvVar", name, "")`, so reads of the same
variable from any number of files converge on one node. The node's `path`
column is the empty string — it is deliberately outside path-based cleanup
(`delete_paths`), so deleting one reader never destroys the shared node.
Provenance lives on the `READS_ENV` edges (file + line per observation); the
node keeps one example observation in metadata. Trade-off: an EnvVar whose
last reader disappears lingers as an edgeless node until a future sweep —
harmless, and M5 config parsing will make EnvVars long-lived anyway.

### D18: TestFile complements File; test functions become TestCase nodes

Files matching test conventions (`test_*.py`, `*_test.py`, `*.test.js/ts`,
`*.spec.js/ts`, or living under `tests/`, `test/`, `__tests__/`, `spec/`) get
a `TestFile` node in addition to the generic `File` node (same path, distinct
type — ids don't collide) plus `is_test_file: true` on their Module node.
Test functions (`test*` defs in test files; `it(...)`/`test(...)`
registrations in JS) become `TestCase` nodes instead of Function nodes, with
`TestFile CONTAINS TestCase`. JS test callbacks are attributed to their
TestCase, so `TestCase CALLS Function` edges ground "which test exercises
this?" queries. `explain file` finds related tests via TestFiles whose module
imports the target module, falling back to filename-stem matching.

### D19: Language wiring — first-class vs generic

Python, JavaScript, TypeScript, PHP, and Bash are first-class (per PRD 30 #3).
Go, Java, and Ruby reuse the same extraction pipeline with reduced scope (see
README table). Structs/interfaces/enums/traits/Ruby modules are emitted as
`Class` nodes with a `kind` metadata field rather than new node types, keeping
`find-symbol` simple; revisit if Interface/Type nodes earn their own queries.
Tree-sitter Query objects and parsers are compiled once per language
(lru_cache) and reused across files; grammars come from
`tree-sitter-language-pack` (no compilation step, no network at runtime).

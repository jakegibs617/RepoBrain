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
metadata list, never as a dangling node. Go/Java internal imports resolve too,
per the same `begin_run(known_files)` pattern — see D31.

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

## 2026-07-09 — Milestone 4 (Markdown purpose mapping)

### D20: Parse reference candidates locally; reconcile MENTIONS globally

`MarkdownParser` extracts links and inline-code spans as structured reference
candidates but does not resolve them. Resolution depends on the complete graph,
so `MarkdownMentionReconciler` runs inside the index transaction after node
upserts and rebuilds only the `MENTIONS` edges it owns. This global rebuild is
deliberate: an unchanged document must gain or lose a relationship when a code
file or symbol is added, renamed, deleted, or becomes ambiguous.

The matching ladder favors precision: exact normalized links target File nodes
(confidence 1.0), backticked paths target File nodes (0.95), exact qualified
symbols target symbol nodes (0.9), and globally unique bare symbol names are
inferred at 0.75. Fuzzy text and ambiguous names create no edge. Route literals
remain unresolved until Milestone 6 creates real Route or Endpoint nodes.

The reconciler is an explicit Indexer collaborator, while `GraphStore` remains
the persistence boundary through a narrow `delete_edges` operation. This keeps
syntax extraction, cross-file resolution, storage, and CLI rendering separate
without introducing a speculative class hierarchy or dependency-injection
framework.

## 2026-07-09 — Milestones 5–10 (MVP completion)

### D21: Deterministic adapters and reusable query functions remain the core boundary

Config, route, memory, data-flow, impact, and reporting features produce or
traverse the same SQLite node/edge model. CLI and MCP layers render plain
JSON-safe dictionaries returned by reusable functions instead of duplicating
graph logic. Route/config reconcilers resolve cross-file identities only after
nodes are persisted, preserving incremental convergence and orphan-edge safety.

Impact results are confidence-bucketed evidence, not correctness claims.
Structural containment hops do not consume data-flow depth, allowing a route
to cross handler/service/repository module boundaries while keeping each
reported relationship source-grounded.

## 2026-07-10 — Milestone 11 (session-start briefing)

### D22: Briefs are atomic grounded facts under a deterministic character budget

The project brief is assembled once in `repobrain/briefing.py`; CLI and MCP
surfaces call that shared function and do not duplicate selection logic. Its
fixed priority is purpose, subsystems, entrypoints, routes/config, assumptions,
open questions, then recent memory. Each fact carries `path:line` provenance
(memory uses its durable Markdown path), and facts are admitted atomically so
budget pressure never slices evidence mid-item.

Budgeting uses `ceil(characters / 4)`, an intentionally approximate and
tokenizer-independent heuristic suitable for offline deterministic behavior.
Freshness is a read-only scan using the same configured file universe and
size+mtime trust model as D12. M11 warns but never mutates; automatic repair is
reserved for M12. Agent installation owns only one exact SessionStart command
and a marker-delimited CLAUDE.md section, preserving unrelated JSON keys,
hooks, and human-authored Markdown.

## 2026-07-10 — Milestone 12 (freshness automation)

### D23: Queries fail closed; only bounded stale diffs auto-repair

Every read-only CLI and MCP surface calls the shared gate in
`repobrain/freshness.py` before its graph query. A stale diff auto-indexes only
when it contains at most 10 added/changed/deleted files and at most 256 KiB of
changed content. Added and changed files contribute their current full size;
deleted files contribute their last indexed size. Exact boundary values are
allowed. This is conservative, deterministic, and avoids reading content a
second time merely to estimate work.

The opt-out (`--no-auto-index` / MCP `auto_index=false`) is non-mutating and
does not mean "allow stale": it refuses the query. Threshold excess, index
failure, and post-index residual staleness likewise return/raise an actionable
freshness envelope before query code runs. This fail-closed rule is especially
important for M11 briefs, which now either repair first or return no facts.

Git lifecycle automation is optional. Installation owns a dedicated runner
and only marker-delimited blocks inside post-commit/post-merge dispatchers;
uninstall removes those exact artifacts while preserving human hooks and agent
instructions. The runner uses the interpreter that performed installation and
resolves the Git top-level at execution time.

## 2026-07-10 — Milestone 13 (diff-aware change context)

### D24: Capture Git first, repair freshness second, resolve context third

`change_context` captures immutable path/status/hunk evidence before invoking
the M12 gate. This preserves the user's unit of work even though automatic
indexing updates graph facts for that same working tree. Only after the gate
returns `can_query=true` does M13 traverse symbols, impact evidence, tests, or
MENTIONS; blocked/failed freshness returns no change facts.

Working mode compares HEAD to the combined index/working tree and adds
untracked files; branch mode compares merge-base(BASE, HEAD) to HEAD. Git
plumbing is read-only and rename-aware. Changed line ranges map by span
intersection to the refreshed graph. A deleted path cannot remain in that
graph, so its old blob is parsed deterministically and labeled with
`git:<revision>:path:line` provenance; its removed impact edges are reported as
an unknown rather than reconstructed speculatively.

Stale-document output remains a review recommendation. Live targets reuse
MENTIONS edges and retain their confidence/inference fields. Rename/delete
reconciliation necessarily removes the old target edge, so M13 has one narrow
fallback: run the same exact path normalizer over persisted structured
Markdown reference candidates. It never fuzzy-matches or resurrects ambiguous
symbol references. Multi-target impact evidence deduplicates by node, edge
kind, and edge provenance while retaining every changed-path reason.

## 2026-07-10 — Milestone 14 (Git history as a deterministic extractor)

### D25: History is bounded correlation evidence in its own tables and bucket

The extractor mines only local Git plumbing (argument arrays, read-only
commands, no shell, no hosted APIs) over a bounded recent window (default 500
commits, configurable via `history_max_commits`). Merge commits are excluded
at the log level; paths are filtered through the same ignore/include
configuration as file scanning, so vendor/generated noise and `.repobrain/`
never enter history facts.

Raw evidence lives in dedicated `git_commits` / `git_commit_files` tables, not
as graph nodes: commits are provenance rows to aggregate, not entities to
traverse, and keeping them out of `nodes` protects search and traversal
queries. The only new graph vocabulary is the `CO_CHANGED_WITH` edge between
File nodes (extractor `git-history`, `is_inferred=1`), which queries earned.
Because the window is bounded, every extraction fully rebuilds the tables and
the extractor-owned edges in one transaction — re-extracting identical history
is idempotent, and rewritten or shortened history leaves no stale rows. A
`history_params` meta stamp (window, per-commit file cap, extractor version)
plus the extracted HEAD decide staleness; bumping the extractor version forces
re-extraction on upgrade.

Co-change is scored with explicit support: a pair needs at least 2 shared
commits, each commit touching k files contributes `1/(k-1)` (broad-commit
discount), commits over `history_max_files_per_commit` (default 50) are
recorded but excluded from pairing, and the score normalizes weighted support
by the less frequently changed file's commit count. Rename continuity chains
`old -> new` aliases newest-first so pre-rename commits attribute to the
current identity. To keep graph rows bounded, each edge stores the 20 newest
supporting commit ids plus an explicit truncation flag; the full supporting
set remains preserved and derivable from `git_commit_files`.

History never impersonates static impact: edge confidence is capped at 0.55
(below the 0.6 medium boundary), `impact_analysis` and `change_context`
surface it as a separately labeled historical-evidence bucket with the
heuristic explanation and provenance stamp, and ownership output is explicitly
observed contribution history, never an authorization or CODEOWNERS claim.
The M12 gate re-extracts when HEAD or parameters changed (pure commits and
rebases move HEAD without touching files); with auto-index disabled, history
staleness fails closed for history-backed answers only, while non-Git and
shallow repositories report `unavailable` honestly without blocking static
queries.

The repository probe is a single combined `rev-parse` process because it runs
on every gated query. Bare repositories are distinguished from non-repositories
and reported as unavailable. `history_params` includes extractor version,
window limits, include/exclude patterns, and an ignore-file content digest, so
any input that can change extraction output forces a rebuild even at the same
HEAD. File identities are produced through `file_node_id(path)` to preserve
the D2 qualified-name invariant across history, queries, and reconciliation.
Extraction failures degrade the current read but are retried on the next gated
read; persisting an error solely by HEAD and parameters would turn a transient
Git timeout into permanent unavailability. Parameter-stamp construction is
inside the same degradation boundary, so an unreadable ignore file cannot
crash or block an otherwise valid static query.
Surrogateescaped Git bytes are mapped injectively into a NUL-tagged hex string
before SQLite storage. Git identities cannot contain NUL, so the malformed
namespace cannot collide with valid text; already-valid Unicode stays unchanged
to preserve exact identity with scanner-produced graph paths.

## 2026-07-14 — Milestone 15 (memory verification)

### D26: Memory verdicts are pure, evidence-bearing read annotations

Memory writes scan structured fields for syntactic path/code references and
bounded code-shaped lexical tokens, then call one shared resolver: normalized exact/unique
suffix paths first, followed by exact unambiguous symbol name or qualified
name. Resolved anchors store the expected node identity, path/span, field and
position, and resolution provenance. Ambiguous and unresolved attempts remain
in resolution evidence but never become guessed anchors; an entry with no
anchors is valid and `unanchored`.

Verification never edits Markdown or stored node metadata. The shared read path
copies each entry and annotates it deterministically: stable node id with the
same span is `verified`; a changed span or a uniquely resolved M14 rename is
`drifted`; a missing or ambiguous replacement is `invalidated`; no anchors is
`unanchored`. Git rename fallback joins an anchor's old path through
`git_commit_files.original_path` to one active current path and, for symbols,
requires the same exact type/name within that file. Missing history degrades to
explicit unavailable evidence rather than fuzzy matching.

Rename commits persist their old path in `git_commit_files.original_path` even
when they are the oldest commit in the bounded window; this M15-required raw
evidence changes history extraction output and therefore bumps
`EXTRACTOR_VERSION` to 3.

CLI and MCP verification run only after the M12 gate. `repobrain index` updates
graph and history facts but does not mutate memory verdicts; the next read
recomputes them. Briefs use the same annotations, prioritize drifted/invalidated
entries with a count line, and exclude those sessions from current memory
sections. This keeps append-first human memory intact and makes repeated reads
over an unchanged graph byte-for-byte convergent.

## 2026-07-14 — Distribution milestone

### D27: Agent installation owns one exact MCP entry and preflights conflicts

The distributable command remains `repobrain`, with MCP support in the
`repobrain[mcp]` extra so ordinary `uvx repobrain` runs do not install the MCP
SDK. The `--from` requirement preserves installed provenance: direct local
wheel/editable URLs remain direct references, while registry installs pin the
exact installed version. Persisted Claude and Git automation also launches
through that requirement rather than retaining the disposable interpreter path;
the exact pre-distribution interpreter command is migrated as an owned legacy
entry. The generated repository `.mcp.json` owns only
`mcpServers.repobrain = {command: "uvx", args: [...]}`. Its argument array
selects `repobrain[mcp]`, invokes the existing console entry point, and passes
the resolved repository root as a single value; spaces therefore need no
shell quoting and the MCP process remains pinned to the intended database.

Installation reads and validates `.claude/settings.json` and `.mcp.json`,
checks for an exact RepoBrain server conflict, and validates the Git repository
when hooks are requested before changing configuration. Malformed JSON,
invalid container shapes, or a pre-existing different `repobrain` server fail
closed. A different RepoBrain version/source is also a conflict: command shape
alone cannot prove that a user-selected fork was installer-owned. Repeated
installation from the same provenance converges. Uninstall removes
the exact generated server only when it still matches, the exact SessionStart
command, marker-delimited Markdown, and owned Git artifacts; user-modified or
unrelated configuration is preserved.

Wheel and sdist contents are exercised in the test suite, including the
console entry point and MCP extra metadata. Clean local artifacts are also
smoked through isolated `uvx`/`uv` environments. Claude SessionStart and Git
dispatchers remain documented POSIX-shell surfaces; the MCP launch itself is
cross-platform JSON with no shell interpolation.

## 2026-07-14 — Milestone 16 (framework/runtime adapters)

### D28: Persist syntax facts locally; reconcile framework meaning globally

Framework support uses one narrow `RuntimeAdapter` boundary. Parsers remain
responsible only for source-local facts: Route metadata records literal
methods, paths, receiver/callback shape, and callback spans; Module metadata
records exact import bindings plus SQLAlchemy model/operation candidates.
`RuntimeAdapterReconciler` consumes those persisted facts after parser
`finish_run` hooks and before Markdown reconciliation/orphan cleanup, inside
the existing index transaction. Each adapter replaces only facts owned by its
extractor. A version stamp backfills adapter facts on an otherwise fresh
pre-M16 database and must be bumped when reconciliation output changes.

Express inline callbacks receive deterministic Function identities from the
route literal and source span. Their observed module-level CALLS are copied to
that precise callback source; the original parser observation remains intact.
Named Express and Flask-style callbacks resolve only to one exact local
callable or one exact persisted import binding. Dynamic expressions and
multi-callback registrations emit no relationship.

The SQLAlchemy adapter creates Table nodes only from literal `__tablename__`
facts. Exact local/imported model operations produce `READS_TABLE` or
`WRITES_TABLE` at confidence 0.85 with `is_inferred=1` and
`inference_reason="sqlalchemy-convention"`: the syntax is observed, but its
runtime meaning is a framework convention. Zero or multiple table mappings
are skipped. Shared data-flow and impact traversals gained these edge types;
change context inherits them through the same impact function, with no
framework-specific CLI or MCP branch.

## 2026-07-14 — Protocol-level MCP integration hardening

### D29: Test the packaged stdio boundary without moving behavior into it

`RepoBrainTools` and the shared query/freshness functions remain the source of
truth. Protocol coverage launches the real `repobrain mcp --path ROOT` process
and uses the official MCP client for initialization, capability negotiation,
tool discovery, and calls. A second, deliberately small JSON-lines harness is
reserved for inputs the typed client cannot produce: malformed messages,
cancellation notifications, EOF shutdown, and bounded read/process cleanup.
Neither harness reimplements tool behavior.

Stdio is the only supported transport. Stdout is protocol-only, stderr carries
diagnostics, and client EOF is the clean shutdown signal. Domain outcomes stay
JSON envelopes in MCP text content (`ok`, `not_found`, `blocked`, or `error`),
while invalid arguments and unexpected exceptions use MCP tool-error results.
The existing fail-closed freshness and repository-root checks are exercised
over transport rather than weakened or duplicated.

The installed JSON argument array is smoked from a clean local wheel through
an isolated `uvx` environment with offline mode forced in the environment, so
paths containing spaces are proven without shell parsing or network access.
This smoke skips only when its optional SDK/build tool or offline dependency
cache is unavailable; package-resolution and protocol-launch failures remain
hard test failures with captured evidence.

## 2026-07-22 — Scale hardening (deterministic indexing/traversal above 1,000 files)

### D30: Fix confirmed hot paths by statement count, not wall clock; leave global reconcilers global

`repobrain/testing/synthetic_repo.py` generates a deterministic, real
(parseable) Python + Markdown corpus with known graph answers — exact
counts for CALLS (same-file observed vs. cross-file inferred), a
repo-global EnvVar convergence, and MENTIONS — so scale tests assert facts,
not just "it didn't crash". It is never committed as generated files;
`scripts/benchmark_scale.py` and `tests/test_scale.py` both regenerate it on
demand. `repobrain/testing/perf.py` counts SQL statements via
`sqlite3.Connection.set_trace_callback`, which is the hardware-independent
proxy this milestone measures proportionality with — wall-clock numbers are
reported for humans but the regression tests assert statement-count
ceilings, per the milestone's own instruction to avoid narrow wall-clock
assertions.

Profiling a 1,200-file synthetic corpus found the sharpest violation of "a
no-change run should stay proportional to changed work": `GraphStore.
touch_paths` refreshed `last_seen_at` with one `UPDATE ... WHERE path = ?`
per unchanged file (2,431 statements for a genuinely no-op reindex of 1,200
files; 12,031 at 6,000 files). Since every touched row gets the identical
timestamp in a single call, this batches losslessly into chunked `WHERE
path IN (...)` statements (500 paths/statement) — 13 and 33 statements
respectively for the same no-op runs. `CodeParser.finish_run` (D16's
cross-file name-match CALLS pass) issued one `SELECT ... WHERE name = ?`
per distinct queued callee name; batched into chunked `name IN (...)`
queries the same way. Full details, before/after numbers at two corpus
sizes, and `EXPLAIN QUERY PLAN` findings are in `docs/SCALE_BENCHMARKS.md`.

Code review on the `finish_run` batching caught a second-order regression:
fetching every candidate for a name (instead of the old `LIMIT 3`) meant a
name common across a real repo (`run`, `__init__`) would re-scan its full
candidate list once per pending call sharing that name — quadratic for
exactly the popular names most likely to appear at scale, even though the
synthetic fixture's guaranteed-unique names never exercised it. Fixed by
precomputing a per-path match count per name once, so "is there exactly one
candidate outside my own file" is an O(1) arithmetic check per pending
call (a node's id embeds its path per D2, and every pending call's caller
lives in that same path, so path-only exclusion is equivalent to the old
combined path+id filter) — the O(matches) scan to find *which* row it is
only runs in the rare case the arithmetic says a unique match remains.

Deliberately left global and undisturbed: the freshness gate's `scan()`
walks and stats every tracked file on every gated read (the only way to
know nothing changed, per D12) — this dominates a no-change run's
wall-clock cost at scale even after the SQL-statement fix, which is the
correct outcome, not a missed optimization. `RuntimeAdapterReconciler` and
`MarkdownMentionReconciler` (D28/D20) still fully rebuild their owned fact
families on any change, because an unchanged Route/Markdown file can gain
or lose a relationship when a *different* file changes; both already cost
a small, bounded number of SQL statements (a handful of `SELECT` queries
returning many rows, not one query per row), so their statement count does
not grow with corpus size even though their row-processing cost does.
`EXPLAIN QUERY PLAN` was checked for the highest-volume lookups; every one
used an existing index except the substring branch of keyword search's
name LIKE (an inherent full scan for a leading wildcard, sub-millisecond at
tested corpus sizes) — no schema index was added without corpus evidence it
would help, per the milestone's constraint against speculative indexes.

## 2026-07-22 — Go/Java internal import resolution

### D31: Go resolves against a root-level go.mod; Java resolves against one unambiguous conventional source root; both stay within the existing per-file resolution pattern

This closes the last open question carried since Milestone 3 (D15/D19): Go
and Java internal imports now become `Module IMPORTS Module` edges instead of
always landing in `external_imports` metadata, using the same
`begin_run(known_files)` / deterministic-id pattern Python/JS/Ruby/PHP
already use — no new mechanism, no filesystem access mid-parse (parsers
still only see `known_files`; the two exceptions below are bounded, one-time
reads done in `begin_run`, before any file is parsed, analogous to the
indexer's own `git rev-parse` call in `git_commit_hash`).

**Go.** `_read_go_module_prefix` reads the `module <path>` directive from
`go.mod` located *exactly* at the indexed root (block comments are stripped
first so a commented-out `module` line can't be mistaken for the real one).
A `go.mod` in an ancestor directory outside the indexed root — e.g. indexing
a subdirectory of a larger module — is not read; those imports stay external
rather than guessing a module boundary from a partial view of the tree. An
import matching the module prefix resolves to *every* non-test `.go` file in
the corresponding package directory (exact directory match, no recursion —
Go packages are one directory each): a package commonly spans multiple
files, and the existing Module-per-file model has no natural way to name
"the package" as one node, so resolving to every file-Module in that
directory is the precise choice per this codebase's precision-over-recall
stance, rather than guessing a single file. `_test.go` files are excluded as
targets (an external package cannot import another package's test files).

**Java.** `_detect_java_source_roots` scans the known-file set for a single
unambiguous `src/main/java` and/or `src/test/java` prefix, matched on whole
path segments (not a raw substring — a directory that merely ends in
`...src` immediately followed by `main/java/` must not be mistaken for a
real root). A fully-qualified import `com.example.pkg.ClassName` maps to
`<root>/com/example/pkg/ClassName.java`; wildcard imports
(`import com.example.pkg.*;`) resolve to every `.java` file directly in that
package directory, mirroring Go's multi-file handling; static imports
(`import static com.example.pkg.Class.member;`) resolve to the declaring
class. If more than one distinct `src/main/java` (or `src/test/java`) tree
exists in the scanned files — a multi-module monorepo layout — that root is
left `None` (ambiguous) rather than guessed, and every import under it stays
external. The package-declaration-content-derived fallback considered during
scoping (deriving a root by matching a file's own `package` statement against
its path) was not implemented: it would require reading file content during
`begin_run`, before any file has been parsed, which no resolver in this
codebase does — non-conventional layouts (no `src/main/java`-style prefix
anywhere) stay external metadata rather than adding that new capability for
one language's edge case.

**Efficiency.** Both directory-based resolutions (Go's exact-directory match,
Java's wildcard match) are backed by a `dict[str, list[str]]` directory index
(`_index_files_by_dir`) precomputed once in `begin_run` alongside
`_go_module_prefix`/`_java_source_roots`, not a linear scan of `known_files`
per import statement — consistent with D30's scale-hardening precedent
against repeated full scans in hot paths. Java's single-class (non-wildcard)
resolution is an exact `frozenset` membership check, the same O(1) pattern
Python's `_resolve_module` already uses.

**Convergence.** Both resolvers stay purely per-parse: an importer file's
edges are only recomputed when that file itself is reparsed (same documented
incremental limitation as D16's CALLS), but a deleted/renamed *target* whose
importer isn't reparsed still converges correctly, because the existing
orphan-edge sweep (which already ran before this milestone) removes any edge
whose target node no longer exists — no Go/Java-specific cleanup was needed.

## 2026-07-22 — INSTANTIATES edges + orphaned EnvVar sweep

### D32: INSTANTIATES mirrors CALLS' confidence ladder exactly; import-qualified resolution is deferred to a batched existence check; EnvVar orphan sweep runs after every edge mutation, every index

This activates the previously-defined-but-unused `EdgeType.INSTANTIATES`
(closing the first of the two smallest carried-over gaps from Milestone 3,
see AGENT_HANDOFF.md "Open Questions") and closes D17's documented EnvVar
lingering-node gap. Both reuse existing, already-proven machinery — no new
mechanism — and are independently testable (`tests/test_code_parser.py`'s
new INSTANTIATES section; `tests/test_graph_store.py`'s and
`test_code_parser.py`'s new EnvVar-sweep tests).

**INSTANTIATES tiers, exactly mirroring D16's CALLS ladder:**

- Same-file `ClassName()` (resolved via `classes_by_name`) → confidence 0.9,
  `is_inferred=0`. `_resolve_plain_call` checks `func_by_name` before
  `classes_by_name` at this tier (unchanged order from before this
  milestone), so a module that unusually defines both a function and a class
  of the same name deterministically resolves to `CALLS` — never both.
- Cross-file globally-unique name-match → confidence 0.7, `is_inferred=1`,
  `inference_reason="name-match"`, computed in `finish_run`'s existing
  batched pass. The batched query now selects `Function`, `Method`, and
  `Class` candidates together; the same per-path arithmetic decides
  uniqueness for each pool independently, and the `Function`/`Method` pool
  is always tried first — a pending call that resolves as `CALLS` never also
  gets checked against the `Class` pool, so a name globally unique as a
  function elsewhere never also emits an inferred `INSTANTIATES`. Ambiguous
  names (either pool, or both) create nothing, matching CALLS' precision
  stance.
- Import-qualified (`symbol_aliases`/`module_aliases`, e.g. `from mod import
  Foo; Foo()` or `alias.Foo()`) also gets confidence 0.9, `is_inferred=0` —
  but with a deliberate refinement over D16's original CALLS approach. D16
  computes the target id and lets it dangle (cleaned up by the existing
  orphan-edge sweep) if wrong, which is safe when there's only one candidate
  type. With a second candidate type (Class) in play, that approach breaks:
  if the imported module legitimately defines both a function and a class of
  the same name, *both* speculative target ids would resolve to real nodes,
  and neither edge would dangle for the orphan sweep to remove — violating
  "never double-emit" for no good reason. Instead, `_resolve_plain_call`'s
  `symbol_aliases` branch and `_resolve_module_attr_call` now queue a
  `_PendingImportCall` (caller, both candidate target ids) and
  `CodeParser.finish_run` resolves the whole run's queue with one batched
  `id IN (...)` existence check (same chunking pattern as the name-match
  pass, just keyed by id instead of name) — `CALLS` wins deterministically
  whenever the Function/Method target exists, `INSTANTIATES` only when it
  doesn't and the Class target does. This was caught by review (an earlier
  version speculatively emitted both edges unconditionally, matching D16's
  old CALLS-only precedent too literally); a regression test
  (`test_import_qualified_call_wins_over_same_named_class_in_target_module`)
  covers the exact scenario. Same-file `self.method()`/`this.method()`
  resolution (`_resolve_self_call`) is untouched — it has no class-shaped
  counterpart to disambiguate against.

**Language scope.** INSTANTIATES fires wherever `_resolve_plain_call`/
`_resolve_module_attr_call`/`finish_run`'s shared machinery already runs —
Python, JavaScript, TypeScript, PHP, and Ruby — with no per-language code.
In practice Python's `ClassName()` idiom is the primary beneficiary: it's
syntactically identical to a function call, so it was already flowing
through this exact pipeline. JS/TS, PHP, and Java's idiomatic constructor
syntax is `new ClassName(...)` (a distinct `new_expression` /
`object_creation_expression` tree-sitter node, not currently captured by any
query), and Ruby's is `ClassName.new` (a receiver call, already skipped by
Ruby's pre-existing dynamic-receiver guard) — so those languages' *real*
constructor calls get nothing from this milestone, not a guess; only a bare
call-shaped invocation of a name that happens to match a known Class would
fire, which is both rare and not a new risk (identical precision stance to
existing name-match CALLS). Extending capture patterns to cover `new`/
`object_creation_expression` per language is future work, out of scope here.
Go is the one language with a real, verified false-positive risk and is
explicitly excluded via `_GoExtractor.SUPPORTS_INSTANTIATES = False`: Go's
type-conversion syntax `T(x)` (T a named type) parses as an ordinary
`call_expression`, identical in tree-sitter shape to a real call (confirmed
by inspecting the parse tree, not assumed) — without the exclusion, a type
conversion whose type name happens to be a known Class would be misread as
an instantiation. Java is unaffected because its extractor never routes
bare/qualified calls through `_resolve_plain_call`/`_resolve_module_attr_call`
at all (only `self`/`this`-qualified calls resolve there), a pre-existing
scope limit this milestone didn't touch.

**Query surfacing.** `impact_analysis` and `trace_data_flow`
(`repobrain/graph/queries.py`) both added `INSTANTIATES` to their traversed
edge-type lists: "who constructs this class" is exactly the kind of
"safer changes" evidence `impact_analysis` exists for, and it's the same
backward-edge traversal CALLS already gets. `explain_file` gained
`instantiates`/`instantiated_by` sections mirroring `calls_out`/`called_by`
(same query shape, new `_instantiates_out`/`_instantiated_by` helpers), and
the CLI's `explain file` text output gained matching sections.

**EnvVar orphan sweep.** `GraphStore.delete_orphan_envvars()` is one bounded
`DELETE FROM nodes WHERE type='EnvVar' AND id NOT IN (SELECT
target_node_id FROM edges WHERE type='READS_ENV')` — not a per-row loop, per
D30. It runs in `Indexer.index()` immediately after `delete_orphan_edges()`,
unconditionally on every index (not gated on whether env-reading files
changed): by that point in the transaction every edge mutation for the run
— `delete_paths`, node/edge upserts, `finish_run`, the runtime-adapter and
Markdown reconcilers, and `delete_orphan_edges` itself — has already
happened, so the READS_ENV edge set it queries against is this run's true
final state. Running unconditionally rather than gating on "did an
env-reading file change" keeps the mechanism simple and matches D30's
precedent for the Markdown/runtime-adapter reconcilers: a small, fixed
number of statements regardless of corpus size, even though the row-scan
work isn't zero on a no-change run. `tests/test_scale.py`'s statement-count
ceilings still pass with the extra statement.

## 2026-07-22 — Real constructor-syntax capture

### D33: `new ClassName(...)`/`ClassName.new` get real grammar capture for JS/TS, PHP, Ruby, and Java; one shared class-only ladder, not a second one

D32 activated `EdgeType.INSTANTIATES` but only by reusing existing call-shaped
captures, so it fired only for Python's `ClassName()` idiom (syntactically
identical to a function call). This milestone adds real capture for each
language's actual constructor syntax:

- **JS/TS**: `(new_expression) @new` added to `_JS_QUERY` (shared by
  JS/TS/TSX). The grammar's `new_expression` has a `constructor` field;
  a bare `identifier` constructor (`new Foo()`) resolves, a
  `member_expression` constructor (`new pkg.Foo()`) is out of scope.
- **PHP**: `(object_creation_expression) @new` added. Verified against a
  real tree-sitter parse (not assumed, matching D32's Go-exclusion
  precedent): this node has **no named field** for the class — its first
  named child is `name` for a bare `new Foo()`, `qualified_name` for
  `new \Pkg\Bar()`, or `variable_name` for a dynamic `new $var()`. Only the
  `name` shape resolves; the other two are out of scope.
- **Ruby**: `ClassName.new` is a `call` node with a `receiver` field, and
  `_RubyExtractor._extract_calls` previously skipped every call with a
  receiver unconditionally (the dynamic-receiver precision guard). It now
  special-cases exactly one shape before that skip: a `receiver` whose type
  is the bare `constant` node (Ruby's capitalized-identifier node type) and
  whose `method` field is exactly `new`. Every other receiver — a variable,
  `self`, a method chain, a constant calling anything other than `new` —
  keeps being skipped exactly as before; this is a carved-out exception, not
  a general weakening (regression tests
  `test_ruby_variable_receiver_dot_new_stays_skipped`/
  `test_ruby_constant_receiver_non_new_method_stays_skipped` cover both
  edges of that boundary).
- **Java**: `(object_creation_expression) @new` added, resolved the same
  way. Investigated per the milestone's explicit instruction to decide, not
  guess, whether this needed Java's separate pre-existing wiring gap
  (`_JavaExtractor._extract_calls` never routes bare/qualified
  `method_invocation` calls through `_resolve_plain_call`/
  `_resolve_module_attr_call` — only `self`/`this`-qualified calls resolve)
  closed first. It doesn't: Java's `object_creation_expression` is a
  self-contained node with its own `type` field (verified by parse probe:
  `type_identifier` for a bare `new Foo()`, `scoped_type_identifier` for a
  qualified `new pkg.Bar()`, out of scope), and is wired directly to the
  shared resolution helper below without touching `method_invocation`
  handling at all. Folding Java in was therefore small and well-scoped, not
  a rewrite of Java's call resolution — the `method_invocation` gap remains
  open and undisturbed, a separate future item if ever prioritized.

**One shared ladder, not two.** The milestone's own instructions warned
against a second resolution ladder. `_resolve_plain_call` gained a
`class_only: bool = False` parameter: when `True`, it skips the
`func_by_name` tier entirely (real constructor syntax is never a plain
call, so there's nothing to check first) and every subsequent tier — same-file
`classes_by_name`, import-qualified `symbol_aliases` queued as a
`_PendingImportCall`, and the cross-file `_pending_calls`/`finish_run`
fallback — is constrained to Class candidates only. A new
`_resolve_constructor_call(name, ts)` is a one-line delegation:
`self._resolve_plain_call(name, ts, class_only=True)`. `class_only` also
guards `_resolve_plain_call` the same way `SUPPORTS_INSTANTIATES` already
did for the non-constructor path, so a constructor call on a language that
somehow had `SUPPORTS_INSTANTIATES = False` would resolve to nothing rather
than silently bypassing the flag (currently a no-op — Go is the only such
language and has no `new`-shaped capture wired — but keeps the flag a
single per-language master switch rather than something future constructor
capture could route around).

Both `_PendingCall` and `_PendingImportCall` gained a matching `class_only:
bool = False` field. `finish_run`'s per-name cross-file pass and
`_resolve_pending_import_calls`'s batched existence check both gate their
existing Function/Method-candidate branch on `not pc.class_only`/`not
pic.class_only` — a no-op for every pre-D33 (`class_only=False`) pending
call, since that condition was already unconditionally true for them — and
otherwise proceed straight to the Class-candidate branch. No new batching
mechanism: both still use the same chunked `name IN (...)` / `id IN (...)`
queries D30 established.

**Regression coverage.** Every language's test suite includes a same-file
case where a function and a class share a name: the bare call
(`Widget()`/`Widget.build`) must keep resolving via the unmodified
`func_by_name`-first path (still `CALLS`, per D16/D32 precedent), while the
`new`/`.new` call to the identical name must resolve to `INSTANTIATES`
against the class — proving the new class-only path doesn't leak into or
get leaked into by the existing bare-call ladder.

**Scope left out**, all intentional, precision over recall:

- Qualified/member constructors (`new pkg.ClassName()` in JS/Java, `new
  \Pkg\ClassName()` in PHP) — a different tree-sitter shape than the bare
  identifier this milestone captures; extending `_resolve_module_attr_call`
  to cover it is plausible future work but wasn't a "trivial reuse" so was
  left out per the milestone's own instruction not to force it.
- Java's `method_invocation` (bare/qualified plain call) resolution gap —
  unrelated to constructor capture, still open, still undisturbed.
- Go — unchanged from D32: no `new`-shaped capture, `SUPPORTS_INSTANTIATES
  = False`.
- No changes to `impact_analysis`/`trace_data_flow`/`explain_file` query
  surfacing — D32 already wired `INSTANTIATES` into all three; this
  milestone only adds more edges of a type they already traverse.

## 2026-07-22 — Java qualified call resolution

### D34: `ClassName.staticMethod()` resolves same-file and import-qualified via name-registry lookup, not grammar shape; `someVar.method()` stays out of scope for good, confirmed by parse probe, not assumed

D33 named Java's `method_invocation` gap explicitly as carried-over scope:
every other first-class-or-better language routes bare and qualified calls
through the shared `_resolve_plain_call`/`_resolve_module_attr_call`
ladder, but `_JavaExtractor._extract_calls` only ever called
`_resolve_self_call` for a bare or `this`-qualified invocation — any other
`object` (`someVar.method()`, `ClassName.staticMethod()`,
`importedAlias.method()`) produced nothing.

**The investigation, first.** A real tree-sitter parse-tree probe (not
assumption, matching D32's Go-exclusion discipline and D33's field
verification) confirmed the grammar shape before any resolution code was
written: a Java `method_invocation`'s `object` field is a bare `identifier`
node for both `someVar.method()` and `ClassName.staticMethod()` —
tree-sitter's Java grammar does **not** syntactically distinguish a
variable-qualified call from a class-qualified one. (`this.method()` is its
own `this` node type, already handled; `pkg.Class.method()` and
`obj.field.method()` are `field_access` objects; `getFoo().method()` is a
`method_invocation` object; `arr[0].method()` is `array_access` — all
distinguishable by node type, all still out of scope, see below.)

Given that, grammar shape alone cannot drive the decision — but the milestone
spec's "not distinguishable" bar was specifically about avoiding a
*capitalization/naming-convention heuristic* (`if identifier looks like a
ClassName, assume it's one` — a real guess, since Java has no rule that a
variable can't be named like a class). That heuristic was rejected. What
was implemented instead is a **name-registry check** — `classes_by_name`/
`symbol_aliases`, the exact same registries every other resolution tier of
this codebase already trusts (D16's whole CALLS ladder is itself built on
"is this name known to be X", never true type information). This is not a
new risk tier: it's the same established precision model, applied to one
more callee shape.

**What resolves.**

1. **Same-file**: `ClassName.staticMethod()` where `ClassName` is a class
   defined in the same file resolves via `classes_by_name.get(prefix)` then
   `self.methods[(class.qualified_name, name)]` — the identical target dict
   `_resolve_self_call` already uses, factored into a new shared
   `_Extractor._resolve_method_in_class(class_qname, name, ts, resolution)`
   helper so the two call sites (enclosing-class lookup vs named-class
   lookup) share one lookup-and-emit implementation, not two copies.
   Confidence 0.9, `is_inferred=0`, resolution label `"same-file"`.
2. **Import-qualified**: `import com.example.pkg.Foo;` now also binds the
   simple name `Foo` into `symbol_aliases[Foo] = (mod_qname, mod_path,
   "Foo")` — the same shape Python's `from pkg import Symbol` already
   populates, just with the imported symbol being a class. Only for a
   single-target, non-wildcard, non-static import (`len(targets) == 1`);
   wildcard imports don't bind one name, and `import static
   pkg.Class.member;` binds `member`, a different name in a different
   position, not the class. `Foo.staticMethod()` then queues a
   `_PendingImportCall` with `func_target_id = node_id(NodeType.METHOD,
   f"{mod_qname}.{symbol}.{name}", mod_path)` — a Method one level deeper
   than the Function/Class target ids `_resolve_plain_call`'s own
   symbol_aliases tier builds for a bare imported name, since here `name`
   is a member of the already-resolved class, not a top-level module
   symbol. `_resolve_pending_import_calls`'s existence check already
   accepted both `"Function"` and `"Method"` node types (pre-dates this
   milestone), so no change was needed there. Resolved through the existing
   batched `finish_run` check, same as every other import-qualified call.
3. **Bare invocation from a static context** (item 3 of the milestone
   scoping): investigated, not a bug. `_resolve_self_call`'s
   `self.methods[(class_qname, name)]` lookup doesn't distinguish static
   from instance methods, so a bare call to a static method of the
   enclosing class from another static method already resolved correctly
   before this milestone; nothing to fix.

**What stays out of scope, and why:**

- `someVar.method()` where `someVar` is a local variable, parameter, or
  field — the prefix simply isn't found in `classes_by_name` or
  `symbol_aliases`, so it silently resolves to nothing. This needs the
  variable's *declared type*, i.e. real type inference, which this
  deterministic, tree-sitter-only, no-compiler codebase does not do
  (D19/D30 precedent). Not guessed from the variable's name or its
  assigned constructor's class.
- An unresolved prefix (`SomeUnknown.method()`, a class from a package this
  run never scanned) does **not** fall through to the cross-file
  callee-name-only `_pending_calls`/`finish_run` fallback other bare calls
  use. That fallback matches purely by method name, ignoring the prefix
  entirely — queuing an unresolved-prefix qualified call into it would mean
  a variable named `x` calling `x.process()` could pick up an unrelated
  class's `process()` method anywhere in the graph just because the name is
  globally unique, which is exactly the false-precision trap the milestone
  spec warned against. Only prefix-resolved calls (same-file class or
  import-qualified symbol alias) get an edge.
- `pkg.Class.method()`/`a.b.method()` (`field_access` object),
  `getFoo().method()` (chained `method_invocation` object), `arr[0].method()`
  (`array_access` object), `super.method()`, lambda/method references
  (`Foo::method`) — all a different, distinguishable tree-sitter shape than
  the plain-`identifier` case this milestone covers, and none is a
  low-risk trivial extension of it; left alone rather than forced.
- A same-file class shadowed by a local variable of the identical simple
  name (`Foo local = ...; Foo.method();` where `Foo` is also a real
  same-file class) resolves against the class, same as every other
  name-registry-based tier in this codebase already accepts as its
  precision model — this codebase has never tracked local-variable scope to
  detect shadowing for any language, and this milestone doesn't start.

**Side effect (documented, not accidental scope creep):** binding an
imported class's simple name into `symbol_aliases` also lets D33's `new
ClassName(...)` constructor resolution — which already checks
`symbol_aliases` via `_resolve_plain_call`'s `class_only=True` path —
resolve an *imported* class, not just a same-file one, closing a gap D33
explicitly left open ("Qualified/member constructors... a different
tree-sitter shape... left out"). Covered by
`test_java_qualified_call_import_qualified_constructor_bonus`.

**Verification.** Full suite: 277 passed (272 baseline + 5 new tests),
`PYTHONPATH` pointed at the worktree checkout per the known .venv pitfall.
`/code-review` (medium effort) found no correctness bugs; two reuse
findings were fixed by factoring `_resolve_method_in_class` out of
`_resolve_self_call` and documenting why the `symbol_aliases`→
`_PendingImportCall` construction in the new
`_resolve_java_qualified_call` is a genuinely different target shape from
`_resolve_plain_call`'s own symbol_aliases tier (Method-nested-in-class vs
top-level Function/Class), not accidental duplication.

## 2026-07-25 — Release hardening (audit remediation)

These five records are a backfill. The 2026-07-25 morning audit found a
Critical secret-exfiltration defect and two structural absences, and the fix
wave that closed them (PRs #10/#11, 2,339 insertions) landed without ADRs —
which the afternoon re-audit correctly called out as a lapse exactly where
future maintainers most need rationale. D35–D38 reconstruct decisions already
implemented; D39 records a decision made while closing the re-audit's
remaining items.

### D35: Dotenv files are excluded by a mandatory matcher that no repository rule can negate, and the dotenv parser is value-free by construction — defense in depth, not one gate

The morning audit planted canary secrets in `.env`/`.env.local` and retrieved
them through `repobrain search`, `trace config --json`, the MCP
`search_project` tool, and `strings` over the SQLite file. The values were in
the index because the file was scanned and its assignments stored.

One fix would have sufficed to close the probe: stop scanning dotenv files.
That is not what was built, because the failure mode here is asymmetric — a
secret in a user's index is unrecoverable once it leaks into a shared
database, an agent transcript, or a git history, while an over-exclusion costs
only a missing config key. Two independent layers now have to fail before a
value is stored.

**Layer one — exclusion that cannot be argued with.** `MANDATORY_EXCLUDES`
(`repobrain/indexing/scanner.py:15-22`) lists `.env`, `.env.*`, `*.env`,
`*.env.*` alongside `.git/` and `.repobrain/`, and is compiled into a
*separate*, final `GitIgnoreSpec` (`scanner.py:99`) rather than being
prepended to the repository's own patterns. This is the load-bearing detail:
gitignore semantics are last-match-wins, so a pattern list is negotiable — a
repository's `!.env.local` or a user's `.repobrainignore` negation would
otherwise re-include the file and silently re-open the vulnerability. A
separate matcher consulted after the layered ones has no negation path at all.
`.env.example` is deliberately inside the exclusion, not carved out: a file
that *should* contain only placeholder keys frequently does not, and the
declarative-key benefit does not justify reasoning about which dotenv files
are safe.

**Layer two — a parser that has nothing to leak.** `EnvFileParser`
(`repobrain/parsers/config_parser.py:15-22`) is now unreachable through normal
scanning, and is kept anyway, rewritten to record key names and line numbers
only. Custom parser wiring and direct invocation are real surfaces, and a
parser that never holds a value cannot be made to disclose one by a future
caller who forgets why the scanner rule exists.

**Pinned, not trusted.** `tests/test_secret_safety.py` re-runs the original
attack — canaries planted, index built, then every retrieval surface plus raw
`strings` over the database asserted clean. The regression test is the reason
this record can be short: the policy is executable.

### D36: Ignore matching moved to `pathspec`'s `GitIgnoreSpec`, retiring D3's fnmatch subset

D3 chose "a small fnmatch-based subset" of gitignore semantics for the MVP,
which was the right call while the graph was the risk and ignore rules were
cosmetic. D35 changed the stakes: once exclusion is a security boundary, an
approximation of gitignore semantics is a liability, because the gap between
"what the user believes is ignored" and "what RepoBrain actually skips" is
where a secret gets indexed.

`pathspec>=0.12,<2` (`pyproject.toml`) is a dependency added deliberately
against this project's local-first, thin-dependency instinct. The trade
accepted: one well-maintained, pure-Python library versus hand-maintaining
directory-only patterns (`build/`), anchoring rules (`/dist` vs `dist`),
`**` semantics, and last-match-wins negation — each a place where a
hand-rolled subset silently under-matches. `IgnoreMatcher` keeps per-directory
layers (`_IgnoreLayer`, `scanner.py:108`) so nested `.gitignore` files resolve
relative to their own base path, matching git's actual behavior rather than
flattening every pattern to the repository root.

### D37: CI is an independent adversarial runner, not a convenience — it re-derives every published number rather than trusting the repository's claims about itself

Before this milestone the project had 300+ tests and no CI. The audit's
finding was not "tests might break"; it was that *every* quality claim on the
setup site and in the docs rested on the author having run something locally
and reported the result honestly. A CI job that only runs `pytest` would have
fixed the smaller problem.

`.github/workflows/ci.yml` therefore has four kinds of step, and the last two
are the point:

1. **A 3.11/3.12/3.13 matrix** on `uv sync --locked`, so the floor of
   `requires-python` and the current release are both exercised and the
   committed `uv.lock` is the thing being tested. Dependencies gained upper
   bounds in the same change (`pathspec<2`, `mcp<2`,
   `tree-sitter-language-pack<1.14`) — an unbounded range means a green CI run
   today says nothing about an install tomorrow.
2. **Lint and types on 3.11 only.** `ruff` and `mypy` results do not vary
   usefully across the matrix; running them three times buys nothing and slows
   the signal.
3. **A coverage floor of 88%**, enforced with `--cov-fail-under` rather than
   reported. A reported number drifts down one uncovered branch at a time; a
   gate makes the drift a conversation.
4. **Scripts that recompute published claims.**
   `scripts/verify_setup_metrics.py` re-derives the test count, file count,
   and graph-fact total and fails if the site disagrees;
   `scripts/evaluate_extraction.py` re-measures extraction against labeled
   ground truth. This is the adversarial part: documentation is verified by
   execution, so the site cannot overstate the project without turning the
   build red.

### D38: The extraction harness is a labeled-corpus regression gate; it is never described as a general accuracy measurement

`scripts/evaluate_extraction.py` indexes a fixture into a throwaway database
and scores the result against a JSON specification of `expected` and
`forbidden` fact keys, exiting non-zero on a missing expected fact, a present
forbidden fact, *or* any extraction warning. The `forbidden` array is the
design decision: an extractor can trivially maximize recall by inventing
edges, so a harness that only checks expected facts measures the wrong half.
Pinning facts that must *not* appear — an unresolvable `someVar.method()`
(D34), a call that does not exist — is what makes the gate meaningful.

The scope limit is recorded here because the number the harness prints invites
misuse. Precision and recall computed over a committed corpus are properties
*of that corpus*, not of the extractor in general; a `1.0/1.0` result on a
handful of fixtures says the extractor has not regressed, and says nothing
about an unseen repository. `docs/EVALUATION_STRATEGY.md` and every other
description of this gate must therefore state the corpus scope alongside any
figure. Publishing a bare accuracy number sourced from this harness would be
the same category of overclaim the audit already found on the setup site.

### D39: Snapshot freshness is enforced by re-indexing and comparing exact structural counts, with history-derived edges excluded — a drift budget was rejected

`setup/graph-data.js` is RepoBrain's index of itself, rendered by the graph
page. It is generated by hand, so it decays silently; the re-audit found it
~10% behind HEAD, and noted that nothing in CI could catch further decay. The
morning fix had made the label honest (a dated snapshot pill instead of a
"Live" badge), which converted a misleading claim into ordinary staleness —
worth fixing properly rather than re-labeling again.

**Exact, not bounded.** The rejected alternative was a drift budget: fail only
when the snapshot trails by more than ~2% of nodes and edges. It is friendlier
— most pull requests would not need to regenerate — but it licenses permanent
rot just under the threshold, and a gate that tolerates being slightly wrong
forever is a gate that has to be re-audited. `snapshot_drift`
(`repobrain/testing/snapshot.py`) re-indexes the working tree from scratch and
compares file count, node count, and structural edge count for exact equality.
When it fails, `scripts/refresh_snapshot.py` regenerates the artifact; the
cost is one command and one commit, and the audit's failure mode becomes
impossible rather than merely bounded.

**Why history edges are excluded.** `CO_CHANGED_WITH` is derived from git
history, not from the working tree, so it changes on every commit. Including
it would make a correct snapshot fail one commit after it was generated,
which trains maintainers to regenerate mechanically or to weaken the gate —
the two ways checks like this die. Excluding the history layer keeps the
comparison over exactly the part of the graph the snapshot claims to be a
function of.

**Provenance moved into the data.** The date and commit were hardcoded in
`setup/graph.html` in two places, a second drift source the freshness check
could not see. The exporter now writes `commit` and `generated_at` into the
payload and `setup/graph.js` renders both from it, so the label cannot
disagree with the graph it labels.

**Regenerating indexes into a temporary database**, never the developer's
`.repobrain/`, so the published artifact is a function of the tree alone and
not of whatever local state happened to be lying around.

**The remedy is one command, deliberately.** An exact gate is only defensible
if closing it is cheap; a check that costs a scavenger hunt gets weakened the
first time it is inconvenient. The published figures were duplicated by hand in
three places — the machine-readable `data-value`, the `<strong>` a reader sees,
and one line of `AGENT_HANDOFF.md` — which is precisely where the drift the
audit found got in. `verify_setup_metrics.py` now owns the edit as well as the
check (`sync_metrics`, exposed as `--write`), and `refresh_snapshot.py` calls
it, so a failing gate costs `refresh_snapshot.py` and a commit. Writer and
checker are pinned to each other by a test that syncs a page and then reads the
values back through `_published_metric`: if they ever disagree, the gate could
never go green, and that test fails first. The handoff's unverified "N pass"
count was dropped in the same change rather than synced — nothing measured it,
so it was a published number with no gate behind it.

## 2026-07-25 — Index freshness as a display surface

### D40: `freshness` is ungated and read-only, the one read command that neither repairs nor refuses

Every other read path in RepoBrain runs through `require_fresh`
(`repobrain/freshness.py:141`), which enforces the invariant that stale facts
are never served: a small diff is auto-indexed, a large one raises and the
query is refused. That is right for a fact-serving surface and wrong for a
status display. A statusline widget polls every few seconds; under the gate it
would either write to the database on a timer that has nothing to do with the
user's work, or exit non-zero — printing `[Exit: 1]` — precisely when it has
something worth saying. Before this change there was no way to ask "is the
index current?" without doing one or the other, so the answer only ever
reached the user once, in the SessionStart brief.

**Reporting staleness is not reading through it.** The invariant `freshness.py`
protects is about facts extracted from the graph: symbols, edges, call sites,
anything whose truth depends on the index matching the tree. `freshness`
returns none of that. Its entire output is a description of the gap between
index and working tree, which is *more* accurate the staler things get. So the
gate does not apply — not as an exception carved out of it, but because the
command is not the kind of surface the gate governs.

**Always exit zero.** Missing database, off-version schema, unreadable file,
scan error: all render as `{"status": "unavailable", "reason": ...}`. A
display's caller cannot tell a crash from a report if both arrive as a
non-zero exit, and the failure mode of guessing wrong is a statusline that
shows an error string forever. The bare `except Exception` here is deliberate
and is the only one in the CLI.

**Read-only opens needed a new path in the store.** `GraphStore.__init__`
writes on every open — `mkdir`, `PRAGMA journal_mode=WAL`, an `executescript`
of the whole schema, pending migrations, `commit`. Repeating that on a timer
next to a live `repobrain index` is exactly the contention the WAL is there to
avoid, so `read_only=True` skips all of it and connects via
`file:...?mode=ro`. Writes then fail loudly with SQLite's `readonly database`
error rather than silently no-opping.

**`mode=ro` without `immutable=1`.** `immutable` is the faster flag and was
rejected: it asserts no other process can be writing and lets SQLite ignore
the WAL entirely, which would serve a torn pre-WAL snapshot in the middle of
an indexing run — the display would report a freshness number computed against
a database state that never existed. The cost of correctness is that a
read-only open still maps the `-shm` file, and creates `-shm` and a zero-length
`-wal` when they are absent. Those live inside the gitignored `.repobrain/`
and the database file itself is never modified, which the store test asserts
by hashing it across an open.

*Amended 2026-07-29.* The test shipped asserting more than this decision
promises: it compared the whole directory listing across the open, which holds
on macOS — where a clean close truncates `-wal` to zero bytes but leaves it in
place — and fails on Linux, where the close removes it and the read-only open
recreates it. Every CI leg failed on that difference alone. The assertion now
matches the guarantee as written above: database bytes and mtime unchanged,
writes still refused, and no *non-empty* log left behind. An empty log is the
proof that nothing was committed through it; a missing one was never the claim.

The same investigation found the rejection of `immutable=1` above to be too
flat. A read-only connection is not permitted to *create* the `-shm` index, so
`mode=ro` does not degrade on a WAL database whose sidecars are absent — it
fails the open outright with `unable to open database file`. Sidecars are
absent whenever nothing holds the database, which is the resting state of every
idle index, so `freshness` reported `unavailable` for a completely intact
9.4 MB graph on this repository. `_read_only_connection` now falls back to
`immutable=1` when the preferred open fails. The torn-snapshot risk that
rejected `immutable` outright does not apply on that path: the sidecars are
missing *because* no connection holds the database, and a live indexing run
necessarily has them present — in which case `mode=ro` succeeded and the
fallback was never reached. The fallback is only taken when the precondition
`immutable` asserts is what the failed open just demonstrated.

**Off-version schemas are refused rather than read.** The ordinary open path
migrates a legacy database in place; a read-only store cannot. Reading a
pre-migration schema as though it were current would return wrong answers
instead of an error, so a version mismatch in either direction raises and
surfaces as `unavailable`.

### D41: The agent skill ships in the wheel and is installed as a marked, adoptable file

`install-agent` already wrote everything an agent needs to *receive* RepoBrain
facts — a SessionStart hook, an MCP entry, a `CLAUDE.md` block — and nothing
that told it what to *do* with them. The brief orients; it does not teach. The
observed consequence, reproduced against this repository with the hook
installed and no skill present, was that agents answered graph-shaped
questions with grep fan-outs of 21 to 28 tool calls and 49k to 62k tokens,
never called `impact` before proposing a change to shared code, and recorded
session handoffs by hand-editing `AGENT_HANDOFF.md` — which is a rendered
mirror of the memory graph, so those notes gained no anchors and never came
back from `memory read`. Two of the three runs stated the same reasoning
verbatim: no `repobrain` binary was visible in the checkout, so they used grep
instead. None had checked whether one was installed.

**The skill ships with the tool rather than living in user configuration.**
The failure is a property of the tool's surface, not of any one user's setup,
and a fix that every adopter must first hear about and then hand-copy is not a
fix. Shipping it as package data under `repobrain/agent_skill/` puts it in the
wheel, in CI, and in the same install command that already earns the user's
consent to write agent configuration.

**CLI-first, MCP when connected.** The MCP server is an optional extra behind
a `.mcp.json` entry that a client may have disabled — this repository's own
checkout has it disabled — whereas the CLI is present wherever the package is.
Teaching MCP as the primary surface would make the skill's first instruction
fail in exactly the environments that most need it. The skill names the MCP
tools as a preferred alternative when that server is live.

**Binary discovery is delegated to the installed hook, not guessed.** An early
draft told agents to fall back to `uvx --from repobrain repobrain`, which
would fail: RepoBrain is not on PyPI, and `install-agent` resolves a PEP 610
direct URL per installation. The skill instead points at the SessionStart hook
command in `.claude/settings.json`, which by construction contains a working
invocation for that project.

**Ownership is a marker in the file, not a content hash.** Hashing would
require shipping a registry of every previously released version to tell "the
user edited this" from "this is last release's copy". A
`<!-- repobrain:skill:owned -->` marker collapses that to one rule readable by
the person it constrains: RepoBrain overwrites the file while the marker is
present, and deleting the marker adopts it permanently.

**An adopted skill is not a conflict.** `_prepare_mcp` refuses to proceed on a
foreign `mcpServers.repobrain` entry, because a wrong MCP entry is a broken
server. A customized skill is a *working* skill, so `_prepare_skill` returns
`{"installed": false, "reason": "user_owned"}` and lets the rest of the
installation land. Refusing the hook and the MCP entry because someone edited
their documentation would be a worse failure than the one it prevents.
Uninstall stays per-file: it removes every marker-bearing file it still owns
even if a sibling was adopted, and prunes only directories it emptied.

## 2026-07-29 — Extractor identity as the second freshness axis

### D42: The index records which extractor built it, and a change to that forces re-extraction regardless of the file diff

`check_freshness` compared file size and mtime against the stored `files`
table, which answers one question: *did the working tree move?* That is the
wrong question after RepoBrain itself changes. When a parser starts extracting
something it did not before, every affected file is byte-identical, its stat is
untouched, and the incremental fast path in `compute_diff` skips it forever.
The graph then holds facts no current parser would produce, and `freshness`
reports it current — a wrong answer served confidently, which is the exact
failure the gate exists to prevent.

This was observed, not theorized. On this repository, at `12ade8e`, `freshness
--json` reported current at 153 files with zero changed inputs while the live
`.repobrain` index held 2,064 nodes against 2,076 in a fresh index of the same
tree. The gap was the JSON-derived `ConfigFile` and eleven `ConfigKey` nodes
for `tests/fixtures/node_api_app/package.json`, a file nothing had touched
since before the structured-config parser learned to read it.

**The fingerprint covers composition automatically and behavior by hand.**
`ParserRegistry.fingerprint()` hashes `EXTRACTOR_VERSION` together with the
sorted parser names. Names catch a parser being added or removed without anyone
remembering to do anything. They cannot catch the case that actually bit us —
a parser whose name held still while its output changed — so `EXTRACTOR_VERSION`
is bumped by hand for that, on the same discipline as `RUNTIME_ADAPTER_VERSION`,
which had already established this pattern one level down for runtime adapters.

**Stored in `meta`, not in a new column or a schema bump.** The `meta` table
already carries `root` and `runtime_adapter_version`. A schema version bump
would have been the more invasive answer to the same need, and would have made
every existing database unreadable to the read-only path in D40 — turning a
recoverable staleness into a hard `schema_mismatch` for every user on upgrade.
The fingerprint is written inside the index run's single transaction, so a
failed run never leaves behind a claim that its facts are current.

**Drift bypasses the auto-index thresholds; it is not counted in files.** The
`max_changed_files`/`max_changed_bytes` policy exists to withhold trust from a
tree diff too large to have been reviewed. A parser upgrade is not that: the
tree is unchanged and re-extraction is deterministic. Counting drift as file
churn would put it above every sane threshold and leave every read surface
refusing until the user ran `repobrain index` by hand, which is a worse default
than one slow query after an upgrade. So `extractor_changed` is reported as its
own axis alongside `out_of_date_count`, and only the file count is measured
against the policy — a large tree diff arriving in the same run as a parser
upgrade still blocks, because that axis is untouched.

**Two axes, separately legible, one `is_stale`.** Both kinds of staleness make
stored facts untrustworthy, so both set `is_stale` and both are repaired the
same way. They stay separately reported because only one of them is measured in
files: saying "0 file(s) are out of date" about a graph that is entirely out of
date would be technically true and actively misleading.

## 2026-07-29 — Brief promotion is a ranking question

### D43: The brief withholds entrypoint promotion from test paths, using the `TestFile` node the graph already carries

`repobrain brief` on this repository promoted four `Route` nodes as its
Entrypoints section. All four were test fixtures —
`tests/fixtures/node_api_app/src/routes/users.js` and
`tests/fixtures/small_python_app/app/api/routes.py` — and they were not merely
ranked too highly: they were the *only* `Route` nodes in the graph, because
RepoBrain is a CLI with no routes of its own. The correct output was an absent
section, and what shipped was 100% noise in the first thing an agent reads at
session start.

**The signal was already in the graph.** `is_test_file`
(`repobrain/parsers/code_treesitter.py:181`) already classifies both files, so
each carries a `TestFile` node at the same path. `briefing.py` simply never
asked. The Entrypoints query now excludes rows with a `TestFile` sibling at
their path. No new configuration key, no second list, no parser change.

**Ranking, not filtering.** The nodes stay in the graph and stay reachable
through `search`, `explain file`, and `impact` — verified, `search "POST
/api/users"` still returns both. Only promotion is withheld. `include_patterns`
/ `exclude_patterns` were the obvious place to look and are the wrong
mechanism: they govern what is *indexed*, so expressing "indexed but not
representative" through them would delete the fixtures every fixture-based test
depends on.

**`reporting.py` deliberately keeps listing them.** `project_overview`
(`repobrain/reporting.py:18`) promotes `nodes("Route")` under "Detected
Routes". That surface is a full inventory of what extraction found, not a
description of what the project *is*; suppressing rows there would make the
report lie about the graph. The inconsistency between the two surfaces is the
point — they answer different questions.

**Known limitation: `examples/` and `fixtures/` are not covered.**
`is_test_file` matches the path segments `{tests, test, __tests__, spec}`, so
this repository is fixed only because its fixtures live under `tests/`. A user
whose sample application sits in a top-level `examples/` directory still gets
its routes promoted. Extending `_TEST_DIRS` was rejected as the fix here: that
set feeds `TestFile` classification globally, including `recommended_tests` in
`impact_analysis` (`repobrain/graph/queries.py:896`), and changing it is an
extraction change that would have to move the extractor fingerprint. Widening
what counts as non-representative is its own decision and should not ride along
inside a brief-ranking fix.

## 2026-07-29 — Extractor identity, made automatic

### D44: The extractor fingerprint hashes the parser sources; `EXTRACTOR_VERSION` is demoted to the axis the sources cannot see

D42 built the second freshness axis and named its own weakness.
`ParserRegistry.fingerprint()` hashed `EXTRACTOR_VERSION` with the sorted
parser names, which detects composition — a parser added or removed —
automatically, and detects the case D42 was written for only if a human
remembers to bump the constant. So the shipped design caught the class of bug
that has never bitten this project and relied on memory for the one that has:
the stale index D42 diagnoses was produced by exactly that discipline failing.

**Hash the sources.** `parser_source_digest()` folds a sha256 over the bytes of
every module in `repobrain/parsers/`, sorted by name, with the names hashed
alongside the bytes so a rename counts even when the corpus is identical. No
discipline required, and it needed no new plumbing: `freshness.py` and
`indexer.py` already compare `fingerprint()` against `meta`, so drift arrives
through D42's existing two-axis reporting.

**The rejected objection was cost, and the cost was measured.** A comment-only
or refactor-only parser edit now forces a full re-extraction that gains
nothing. The prompt that queued this work estimated that at ~25 s. Measured on
this repository — `git archive HEAD` into a clean tree, fresh `repobrain index`
— it is **0.59 s** for 153 files, 2,095 nodes, 4,541 edges. Fourteen of this
project's sixty-one commits touched `repobrain/parsers/**`, and nearly all of
them genuinely changed extraction, so the false-positive rate is low as well as
cheap.

**CI enforcement was the alternative and was rejected.** Failing a build when
`repobrain/parsers/**` moves without `EXTRACTOR_VERSION` moving keeps
re-indexing precise, but it binds only contributors who go through CI — an
editable local install is the environment where a stale index actually hurts —
and it puts CI in a process-policing role D37 explicitly rejects in favour of
re-deriving facts. Correctness that depends on a gate the affected user never
passes through is not correctness.

**No cache, deliberately.** `fingerprint()` runs on every `check_freshness`,
including a statusline polling `freshness` on a timer under D40. Caching the
digest for the process lifetime would hold a stale answer across exactly the
edit this exists to notice, which is the failure mode being fixed rather than a
smaller version of it. At 0.27 ms per digest the cache buys nothing worth that.

**`EXTRACTOR_VERSION` stays, demoted.** The sources are the automatic axis;
the constant is now for extraction changes that live *outside* the parsers
package. The concrete one is `repobrain.indexing.scanner.detect_language`,
which decides which parsers run at all and can therefore change every
extraction result without a single byte moving under `parsers/`. Deleting the
constant would have left that ungated; leaving its docstring describing the old
contract would have been worse than deleting it.

**One test was narrowed rather than deleted.**
`test_no_change_fast_path_does_not_read_file_bodies` patched `Path.read_bytes`
globally, so fingerprinting tripped it. The invariant it protects is that an
unchanged incremental run does not read the bodies of *the files it is
indexing*; RepoBrain reading its own installed sources is a different act. The
patch is now scoped to the indexed root. That the narrowed guard still catches
the original regression was verified by mutation — forcing `compute_diff` to
read each scanned file's bytes, and confirming the test fails.

## 2026-07-29 — Change context sized for the session that reads it

### D45: `change-context` pays for facts, not for restating them; the budget trims lowest-priority evidence and always reports it

`change-context --json` exists so an agent can understand a diff, and on this
repository's own two-commit diff it emitted 1,845,848 characters — about
461,000 tokens. That is not "large"; it is unusable by an order of magnitude.

**The suspected cause was measured and cleared first.** `setup/graph-data.js`
is a generated, minified, single-line ~2 MB artifact that is nonetheless a
first-class graph citizen, and it entered the diff in exactly that range. A
generated snapshot of the graph inflating the graph's own impact analysis would
have been a more interesting bug than the token count. It is not what happened:
it contributes **0 impact items and 278 characters**. Recorded so nobody
re-investigates it.

**The cost was attribution, and it was quadratic in the wrong thing.**

| key | chars | shape |
| --- | ---: | --- |
| `impact` | 834,715 | 958 rows over **497 distinct nodes** |
| — `changed_reasons` | 441,930 | 3,175 fully re-serialized dicts |
| `tests_to_run` | 94,704 | 82,878 of it reason dicts, for 32 tests |
| `text` | 157,802 | the structured payload again, in prose |
| `changes` | 225,410 | incl. 33,745 of per-symbol `changed_because` |

Four lossless changes, in the order they were found:

- **Reasons are interned.** A top-level `reasons` table; items cite it through
  `reason_ids`. The table is bounded by the diff rather than by how many nodes
  cite it.
- **Impact is one row per node.** Each row carries an `evidence` list of the
  edges that put it in the blast radius. A node reached at two traversal depths
  was two rows under the old key, because the depth-decayed confidence differed;
  it is now one node holding its strongest edge. A node also no longer appears
  in two confidence buckets at once.
- **`text` is rendered for the human surface only.** `--json` and MCP pass
  `include_text=False`.
- **`changed_because` is gone from symbol records.** It restated the enclosing
  change record's `status`/`old_path`/`new_path` on every one of 395 symbols.

Together: **461,000 → 142,848 tokens, with nothing removed.**

**Then a budget, because 142,848 is still not a session.** `--budget` copies
`brief`'s shape — the deterministic `ceil(chars / 4)` estimate, a minimum, and
a default. Trimming runs lowest-priority first: historical co-change, docs,
impact by ascending confidence, tests, and the diff itself last.

**Truncation is reported, never silent.** A caller that cannot distinguish a
trimmed impact set from a complete one will treat it as complete — the
confidently-wrong answer the freshness gate exists to prevent. Two honesty
details the implementation forced:

- Trimming an item takes its citations with it, so `reasons` is pruned to what
  survivors cite and renumbered. Otherwise a budgeted payload spends its
  allowance on attribution for facts it no longer carries.
- Every trimmable list can be emptied and the payload still costs what its own
  scaffolding costs, around 430 tokens. A budget under that floor is unmeetable,
  so `truncation.within_budget` says so rather than implying it was met.

**The trim estimate must resynchronise.** Decrementing a running total by each
popped item is what keeps trimming linear, but it cannot see that dropping an
item also releases the reasons only it cited. Against a 60,000-character limit
and a 65,302-character reason table, the estimate never converged and every
list was emptied — including the diff — while reporting itself within budget at
451 tokens. It now re-measures exactly every 25 pops and whenever a container is
exhausted. The existing fixture is two orders of magnitude too small to have
caught this; the regression test builds a diff wide enough for the table to be a
large share of the payload, and was mutation-checked against the un-resynced
version.

**Known consequence: on a very wide diff the default budget buys the diff and
nothing derived from it.** `changes` is now the dominant key — about 59,000
tokens for 84 files — so at the 15,000 default this diff yields 27 changed
files and zero impact, and needs `--budget 60000` before impact and tests
reappear. That follows from ranking the diff above evidence derived from it,
which is the right default for a working diff of a handful of files and
arguably the wrong one at this width. It is left as-is deliberately: the
truncation report names exactly what went, so the failure is legible and the
remedy is one flag. Whether wide diffs should instead degrade `changes` to
paths-only and keep the impact set is a real question and is not answered here.

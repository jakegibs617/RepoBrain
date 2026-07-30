# RepoBrain

RepoBrain is a **local-first "second brain" for AI coding agents**. It indexes a
software project into a durable, queryable SQLite graph spanning source code,
Markdown documentation, structured configuration, and selected runtime
wiring — so an agent can re-enter a repository and immediately know what it
is, where things live, and what connects to what.

Everything runs offline. No API keys, no network calls.

## Current status (Milestones 1–16 plus distribution complete)

Implemented:

- Python package, CLI, and SQLite storage (WAL mode) under `.repobrain/`
- Graph schema declaring the full, future-compatible node/edge vocabulary
  from the PRD, with deterministic sha1 IDs and provenance on every row
  (path, span, extractor, confidence, commit hash, timestamps). Parsers and
  reports use only the subset they currently produce; reserved types such as
  `Endpoint`, `CLICommand`, `Script`, and `ADR` are not synthesized.
- File scanner with gitwildmatch-compatible root and nested `.gitignore`
  rules, `.repobrainignore`, mandatory secret/database excludes, binary
  sniffing, and a 2 MB size cap
- Incremental indexing: sha256 content hashes; unchanged files are never
  re-parsed; changed/deleted files have their nodes, edges, and FTS rows
  removed before re-adding
- Generic file parser (File + Directory nodes, CONTAINS edges, full-text rows)
- Markdown parser (markdown-it-py): MarkdownDocument and nested
  MarkdownSection nodes with line spans, links, fenced code blocks, and
  TODO/FIXME list items as Task nodes
- Keyword search: FTS5 bm25 ranking combined with exact-name and path boosts
- Tree-sitter code parser: Module/Function/Class/Method/Variable nodes with
  qualified names and line spans, DEFINES/CONTAINS/IMPORTS/CALLS/
  INSTANTIATES/READS_ENV edges, TestFile/TestCase detection, and symbol
  names in full-text search
- `find-symbol` and `explain file` CLI commands backed by reusable graph
  queries (`repobrain/graph/queries.py`)
- Markdown-to-code purpose mapping: local links and backticked file/symbol
  references become source-grounded `MENTIONS` edges; ambiguous symbol names
  are deliberately skipped
- Bidirectional `docs-for-code` and `code-for-docs` queries, also surfaced in
  the "Referencing docs" section of `explain file`
- Value-free JSON, TOML, and YAML config-key extraction, value-redacted
  Dockerfile instruction and base-image extraction, and GitHub Actions,
  Docker Compose, and Kubernetes adapters. Dotenv files are excluded by
  default; a defensive parser retains key/line provenance without values when
  invoked directly. YAML config definitions connect to code-level environment
  reads without persisting assigned values.
- Deterministic Flask-style and Express route adapters with precise named and
  inline callback identities, plus conservative SQLAlchemy table flow
- Grounded data-flow tracing and confidence-bucketed impact analysis shared by
  CLI, MCP, and change-context surfaces. Traversals carry a token budget
  (default 10,000) and report what they trimmed; `trace_symbol` defaults to one
  hop and `trace data-flow` to two, because an unbounded traversal of a hub
  symbol measured 62,000–423,000 tokens — larger than the context window of the
  agent it is built for.
- A local FastMCP server exposing 19 repository-scoped tools
- Silent-by-default structured diagnostics, opt-in via CLI `--verbose` /
  `--log-level` or `REPOBRAIN_LOG_LEVEL`; MCP logs stay on stderr and exclude
  query/config/source payloads
- Append-only structured agent memory mirrored into Markdown handoff files,
  with deterministic graph-anchor verification and drift evidence
- Grounded project overviews and Markdown/HTML graph reports
- Deterministic Git history extraction over a bounded recent window:
  file-level co-change coupling with supporting commits and broad-commit
  discounting, churn hotspots, and observed contribution history — blended
  into impact analysis and change context as a separately labeled
  historical-evidence bucket (`repobrain history …`)

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
| Go         | yes (structs/types as Class) | yes, if `go.mod` is at the indexed root (import path → every non-test `.go` file in that package directory) | same-file | no |
| Java       | yes (interfaces/enums as Class) | yes, if a single unambiguous `src/main/java`/`src/test/java` root exists (fully-qualified/wildcard/static imports → file(s) under that root) | within-class, same-file/import-qualified `ClassName.staticMethod()` | no |
| Ruby       | yes (modules as Class) | `require_relative` | same-file, within-class, name-match | no |

Unresolvable or third-party imports are stored as `external_imports` metadata
on the module node — never as dangling graph nodes.

**Go import resolution** reads the `module` directive from a `go.mod` file
located exactly at the indexed root (a single bounded text read done once per
index run, never a `go` toolchain invocation). An import matching that module
path resolves to *every* non-test `.go` file in the corresponding package
directory (Go packages are directories that commonly hold multiple files;
resolving to all of them avoids guessing which file the caller means).
`go.mod` files outside the indexed root (e.g. an ancestor directory, for a
sub-directory-as-root layout) are not read — resolution stays external in
that case, by design. Example: with `module example.com/foo` in `go.mod`,
`import "example.com/foo/util"` resolves to every `util/*.go` file
(excluding `util/*_test.go`); `import "example.com/bar/util"` (a different
module) or an import with no `go.mod` present stays `external_imports`.

**Java import resolution** looks for the scanned tree's single conventional
`src/main/java/` and/or `src/test/java/` prefix and maps a fully-qualified
import `com.example.pkg.ClassName` to
`<root>/com/example/pkg/ClassName.java`. Wildcard imports
(`import com.example.pkg.*;`) resolve to every `.java` file directly in that
package directory, mirroring Go's multi-file package handling. Static
imports (`import static com.example.pkg.Class.member;`) resolve to the
declaring class. If more than one distinct `src/main/java` (or
`src/test/java`) tree exists in the scanned files — a multi-module layout —
that root is treated as ambiguous and left undetected entirely: imports stay
external rather than guessing which tree a caller means. Non-conventional
layouts (no `src/main/java`-style prefix anywhere in the scanned tree) are
not resolved; there is no package-declaration-content-based fallback, since
that would require reading file content before any file is parsed, which
this codebase's import resolvers don't do.

**Java call resolution** additionally resolves `ClassName.staticMethod()`
calls, same-file and import-qualified, even though tree-sitter's Java
grammar gives that call the identical shape as a variable-qualified call
(`someVar.method()`) — both are a bare `identifier` in the `object` field.
Resolution goes through the same name registries every other language's
CALLS ladder already uses (`classes_by_name`/import bindings), not a
capitalization guess. A call whose object identifier isn't a known class
name (i.e. every ordinary instance method call) stays unresolved for good —
that's the type-inference boundary this codebase doesn't cross.

### Supported framework/runtime patterns

| Adapter | Exact supported syntax | Emitted evidence |
|---------|------------------------|------------------|
| Flask-style Python | `@app.route("/x", methods=["POST"])`, literal method lists, and `@app.get/post/put/patch/delete("/x")` on static receivers | Route → exact decorated Function at confidence 0.9; ordinary import-qualified CALLS continue through the handler |
| Express JS/TS | `app`/`router` literal-method registrations with one inline function or one exact local/imported identifier callback | Route → precise Function at confidence 0.9; inline callback CALLS are re-attributed from Module to a deterministic callback identity |
| SQLAlchemy convention | literal `Model.__tablename__`, `Model.query.*`, `select(Model)`, `session.get(Model, ...)`, and `session.add/merge(Model(...))` with an exact local or imported model binding | Table nodes; model `DEPENDS_ON` table at 1.0; inferred `READS_TABLE`/`WRITES_TABLE` at 0.85 with `sqlalchemy-convention` evidence |

Parsers store source-local route, import-binding, model, and operation facts.
The runtime reconciler resolves them against the complete persisted graph
inside the index transaction, before orphan cleanup. Adapter facts are fully
replaced after relevant changes, so unchanged callers converge when an exact
target is added, renamed, deleted, or becomes ambiguous.

## Try it with uvx

Prefer a visual walkthrough? Open [`setup/index.html`](setup/index.html) in your browser for the interactive setup guide.

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

**Not yet published to PyPI.** `repobrain` isn't a registered package yet, so
a bare `uvx repobrain ...` will fail (or grab an unrelated package of that
name). Until it's published, run it straight from this GitHub repo by adding
`--from git+https://github.com/jakegibs617/RepoBrain` before `repobrain` in
every command:

```bash
# one-off commands run in an isolated environment, fetched straight from GitHub
uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain --help
uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain index .

# a token-budgeted orientation pack: what this project is, entrypoints,
# open assumptions, recent memory — grounded in the graph you just built
uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain brief --budget 2000

# installs an MCP server entry in .mcp.json, a SessionStart hook in
# .claude/settings.json, an agent skill in .claude/skills/repobrain/, and a
# marker-delimited section in CLAUDE.md
# add --git-hooks to also keep the index fresh after commits and merges
uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain install-agent .

# restart the agent client, then verify that its RepoBrain MCP tools are listed
# and call explain_project (or inspect the generated argument-array config)
cat .mcp.json

# remove only RepoBrain-owned entries and marker blocks
uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain uninstall-agent .
```

`install-agent` also adds `.repobrain/` to the repository's `.gitignore`
idempotently. `uninstall-agent` intentionally leaves that safety rule in place
so an existing local graph database never becomes committable by accident.

**The agent skill.** `install-agent` writes `.claude/skills/repobrain/`, which
teaches an agent to query the graph instead of grepping the tree, to check
`impact` before editing shared code, and to tell a stale index apart from a
missing one. Both files carry a `<!-- repobrain:skill:owned -->` marker.
RepoBrain upgrades a file only while that marker is present, so **delete the
marker line to adopt the skill as your own** — later installs then leave the
whole directory alone and report `"reason": "user_owned"` rather than failing.
`uninstall-agent` removes only marker-bearing files.

Once `repobrain` is published to PyPI, the `--from git+...` prefix becomes
unnecessary and the plain `uvx repobrain ...` form works as shown further
below.

### Skip typing the GitHub URL every time

**Recommended: install it as a real command, like `code` or `subl`.**
`uv tool install` (uv's equivalent of `pipx install`) fetches the package once
and drops an actual executable shim on your `PATH` — not a wrapper, a real
`repobrain` binary you can call from anywhere:

```bash
uv tool install --from git+https://github.com/jakegibs617/RepoBrain repobrain
```

That's it — open a new terminal and `repobrain index .` just works, no prefix
needed. A few things worth knowing:

- **Refresh to the latest commit** (git installs don't auto-update):
  `uv tool install --reinstall --from git+https://github.com/jakegibs617/RepoBrain repobrain`
- **Uninstall:** `uv tool uninstall repobrain`
- If `repobrain` isn't found after installing, your shell's `PATH` doesn't yet
  include uv's tool directory — run `uv tool update-shell` and open a new
  terminal.
- Once `repobrain` is published to PyPI, re-run the same install command
  without `--from git+...` to switch to registry releases.

**Alternative: always-fresh, no persistent install.** If you're actively
tracking new commits and don't want to re-run `--reinstall` each time, use a
shell function instead — it re-fetches via `uvx` on every call. Add this to
your `~/.zshrc` (or `~/.bashrc`/`~/.bash_profile`):

```bash
repobrain() {
  uvx --from git+https://github.com/jakegibs617/RepoBrain repobrain "$@"
}
```

The generated `.mcp.json` launches the repository-scoped server as
`uvx --from <installed-repobrain[mcp]-requirement> repobrain mcp --path
<absolute-root>`. Registry installs are pinned to the installed version; local
wheel and editable installs retain their direct artifact/source URL. The JSON
stores every token as a separate argument, so repository paths containing
spaces do not depend on shell quoting. The `mcp` extra is optional for normal
CLI use and installed automatically by that MCP launch command.

**Editable installs additionally get `--with-editable <checkout>`** in both the
MCP entry and the SessionStart hook. A bare `repobrain @ file:///path`
requirement carries no version, commit, or content hash, so uv has nothing to
invalidate its build cache on and keeps launching the first wheel it built
while you edit the source — and neither `uvx --refresh` nor
`--refresh-package` dislodges it. Declaring the source editable costs about
0.02 s per launch and keeps the agent on the code you are actually writing.
Installations written by earlier versions are upgraded in place the next time
you run `install-agent`.

`install-agent` also appends a marker-delimited section to `CLAUDE.md` so the
agent knows the session brief exists and how to refresh it:

```
<!-- repobrain:brief:start -->
## RepoBrain session context

RepoBrain injects a source-grounded project brief at session start. If it reports a stale index, run `repobrain index`.
<!-- repobrain:brief:end -->
```

Reruns of `install-agent` update this block (and the `.mcp.json`/
`.claude/settings.json` entries) in place rather than duplicating them, and
`uninstall-agent .` removes only what's between the markers.

### MCP client and transport contract

RepoBrain supports one local MCP server per repository over stdio. Clients must
perform the normal `initialize` / `notifications/initialized` handshake before
tool discovery or calls, keep stdout reserved for JSON-RPC protocol messages,
and treat stderr as diagnostics. Closing the server's stdin is the supported
clean-shutdown signal; clients should still apply bounded request and process
cleanup timeouts.

Tool results are JSON envelopes in MCP text content. Domain outcomes use a
top-level `status` such as `ok`, `not_found`, `blocked`, or `error`; invalid
arguments and unexpected tool exceptions use MCP's tool-error result. Every
read tool applies the shared freshness gate before returning facts. Cancellation
notifications are accepted, but the current synchronous local query/indexing
functions may finish before cancellation can preempt their work.

HTTP/SSE transports, remote deployment, authentication, multi-repository
servers, and server-initiated resources/prompts are not supported. Repository
root confinement applies even if a client supplies an absolute path to a tool.

For development from a source checkout:

```bash
uv venv .venv
uv pip install -p .venv/bin/python -e ".[dev]"
```

To prove the package locally without publishing it:

```bash
uv build
uvx --from dist/repobrain-0.1.0-py3-none-any.whl repobrain --help
uv run --isolated --no-project \
  --with "repobrain[mcp] @ file://$PWD/dist/repobrain-0.1.0-py3-none-any.whl" \
  python -c "import mcp, repobrain"
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

# is the index current? opens the graph read-only, never indexes, and always
# exits 0 -- an unreadable index reports {"status": "unavailable"} with a
# machine-readable "reason_code" (no_index / schema_mismatch / unreadable)
# rather than failing, so status displays polling on a timer can call it safely.
# "is_stale" covers two axes: files that moved ("out_of_date_count") and an
# index built by a different set of parsers ("extractor_changed")
.venv/bin/repobrain freshness
.venv/bin/repobrain freshness --json

# source-grounded session orientation (plain text or JSON)
# --budget uses the deterministic ceil(characters / 4) token estimate
.venv/bin/repobrain brief --budget 2000
.venv/bin/repobrain brief --budget 800 --json

# every read-only query auto-reindexes small stale diffs before serving facts;
# --no-auto-index performs a non-mutating check and refuses stale reads
.venv/bin/repobrain search "database" --no-auto-index

# analyze the working change (staged + unstaged + untracked), or a branch diff
.venv/bin/repobrain change-context
.venv/bin/repobrain change-context --base main --json

# install MCP config plus an idempotent Claude SessionStart hook and CLAUDE.md snippet
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
# traversals are budgeted (default 10,000 tokens) and report what they trimmed;
# --depth defaults to 2 for data-flow (route -> handler -> service)
.venv/bin/repobrain trace config DATABASE_URL --path tests/fixtures/small_python_app
.venv/bin/repobrain trace data-flow "POST /api/users" --path tests/fixtures/small_python_app
.venv/bin/repobrain impact app/services/user_service.py --path tests/fixtures/small_python_app
.venv/bin/repobrain impact repobrain/graph/store.py --budget 4000 --json

# deterministic Git history evidence (local plumbing only; heuristic, labeled)
# co-change: files that historically change together, with supporting commits
.venv/bin/repobrain history co-change repobrain/cli.py
.venv/bin/repobrain history hotspots --limit 10
.venv/bin/repobrain history owners repobrain/history.py

# grounded overview and human-readable reports
.venv/bin/repobrain explain project --json
.venv/bin/repobrain report

# durable agent memory
.venv/bin/repobrain memory write --summary "Implemented auth flow" --next-step "Add expiry tests"
.venv/bin/repobrain memory read --topic auth
.venv/bin/repobrain memory verify
.venv/bin/repobrain memory verify --json --no-auto-index

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
  "max_file_size_bytes": 2097152,
  "history_max_commits": 500,
  "history_max_files_per_commit": 50
}
```

`history_max_commits` bounds the Git history extraction window;
`history_max_files_per_commit` marks broader commits as oversized — they are
recorded for churn/ownership but excluded from co-change pairing.

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

Regenerate the published self-index snapshot that page ships with. CI fails
when the committed snapshot no longer matches the tree, so run this after
changes that add or remove indexed files. It also resyncs the numbers the
quality page and `AGENT_HANDOFF.md` publish, so one command closes the gate:

```bash
.venv/bin/python scripts/refresh_snapshot.py
```

## Limitations

- Gitignore matching follows gitwildmatch rules, including negation and nested
  `.gitignore` files. RepoBrain's mandatory `.git/`, `.repobrain/`, and dotenv
  exclusions cannot be negated; this is an intentional safety boundary rather
  than exact parity with `git check-ignore`.
- Paths are stored relative to the indexed root; one repository per database
  (enforced: the database is pinned to its root and refuses other roots).
- Scanning is contained to the *resolved* root: a candidate whose
  `realpath` lands outside the indexed tree is skipped, so a symlink cannot
  pull in a file the ignore rules never get to see (they match names, and a
  link is reached by one name and read from another). Symlinks resolving
  inside the root are indexed as ordinary content — they cross no boundary —
  and directory symlinks are never traversed. Dangling and mutually recursive
  links are skipped rather than fatal.
- On POSIX, incremental change detection trusts size plus nanosecond mtime and
  ctime before hashing; on Windows, where ctime is creation time, unchanged
  candidates are conservatively hashed. `--no-incremental` remains the
  explicit full-reparse recovery path.
- Markdown, JSON, TOML, YAML, Dockerfiles, and the eight code languages above
  get dedicated structural parsing; other text files are indexed whole-file
  for full-text search. Structured configuration values and raw configuration
  bodies are deliberately not persisted. Dotenv files are excluded by
  default. ADR-named Markdown files are represented as grounded
  `MarkdownDocument`/`MarkdownSection` nodes rather than a synthetic `ADR`
  node.
- Call-graph extraction prefers precision over recall: method calls on
  dynamic receivers (anything other than `self`/`this`) are skipped, and
  cross-file name-only matches require the name to be globally unique.
- Constructor calls become `INSTANTIATES` edges under the exact same
  confidence ladder as `CALLS` (0.9 observed same-file/import-qualified, 0.7
  inferred cross-file-unique-name). Python's `ClassName()` idiom gets this
  via the normal bare-call resolution path (it's syntactically identical to
  a function call). Real constructor syntax is also captured directly:
  JS/TS/Java `new ClassName(...)` (`new_expression`/
  `object_creation_expression`), PHP `new ClassName(...)`
  (`object_creation_expression`), and Ruby `ClassName.new` (a bare
  `constant` receiver) all resolve through the same class-only ladder.
  Qualified/dynamic constructors (`new pkg.ClassName()`, `new $var()`, a
  non-constant Ruby receiver) are out of scope, not guessed. Go is
  explicitly excluded from constructor resolution entirely: its `T(x)`
  type-conversion syntax parses as an ordinary call expression, and would
  otherwise be misread as instantiating a same-named type. A name that
  resolves to both a Function/Method and a Class always produces `CALLS`,
  never both.
- Framework adapters intentionally skip computed route paths or methods,
  dynamic callback expressions, Express registrations with middleware or
  multiple callbacks, non-`app`/`router` Express receivers, dynamic Flask
  receivers, model aliases not grounded by an exact import, and ORM operations
  whose model maps to zero or multiple table literals. FastAPI and ORM
  relationship/join semantics are not supported yet.
- Incremental runs only re-parse changed files, so a new function in file A
  will not gain inferred CALLS edges from an unchanged caller in file B until
  B changes (or a `--no-incremental` run).
- `EnvVar` nodes are repo-global (every reader of the same variable name
  converges on one node); a bounded sweep removes an `EnvVar` node once its
  last `READS_ENV` edge disappears (the reader was deleted or edited to stop
  reading it), so it doesn't linger forever as an edgeless node.
- Markdown mention matching is intentionally strict: exact local paths and
  exact unique symbol names are linked; fuzzy text, ambiguous symbols,
  external URLs, and route literals without a Route node are skipped.
- Go imports resolve only when `go.mod` is exactly at the indexed root; a
  module's `go.mod` living in an ancestor directory outside the indexed root
  (a sub-directory-as-root layout) is not read, and those imports stay
  external metadata. Java imports resolve only when the scanned tree has one
  unambiguous `src/main/java`/`src/test/java` root; multi-module layouts with
  more than one such tree, and layouts with no conventional root at all,
  leave every import as external metadata rather than guessing.
- Empty directories produce no Directory nodes (directories are derived from
  file paths).
- Git history evidence is correlation over a bounded recent window (default
  500 commits): co-change is a labeled heuristic, never a dependency claim;
  copies are not followed (only renames); shallow clones and non-Git
  directories report history as unavailable while static queries keep
  working.
- The generated MCP launch entry uses a cross-platform JSON argument array.
  Claude SessionStart commands and optional Git hook dispatchers are shell
  strings/scripts and currently require a POSIX-compatible shell; on native
  Windows, use the MCP integration without `--git-hooks`, or run RepoBrain
  under WSL. CLI indexing and MCP repository scoping do not otherwise rely on
  shell parsing.

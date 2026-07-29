# RepoBrain command reference

Full surface. The skill covers the ten commands that carry most of the weight;
this file exists for the rest, and for the output and exit-code contracts.

## Invocation

The `repobrain` executable may be on `PATH`, in a project virtualenv
(`.venv/bin/repobrain`), or reachable only through `uvx`. RepoBrain is not
necessarily installed from PyPI, so the `--from` source varies by project:

```bash
uvx --from <source> repobrain <command>
```

The SessionStart hook command in `.claude/settings.json` contains this
project's resolved source. Copy it rather than guessing.

If a `repobrain` MCP server is connected, prefer its tools — same graph, no
subprocess, structured results. Tool names mirror the CLI: `search_project`,
`explain_file`, `find_symbol`, `impact_analysis`, `trace_config`,
`trace_data_flow`, `co_change`, `churn_hotspots`, `ownership`,
`docs_for_code`, `code_for_docs`, `project_brief`, `change_context`,
`read_agent_memory`, `write_agent_memory`, `verify_agent_memory`,
`explain_project`, `index_repo`, and `trace_symbol` (which has no CLI form).
`freshness`, `init`, `report`, `install-agent`, and `uninstall-agent` are CLI
only.

## Path argument, which is inconsistent

`init`, `index`, `install-agent`, and `uninstall-agent` take the repository
root as a **positional** argument. Every other command takes `--path DIR`.
Both default to `.`.

## Commands

### Orientation

| Command | Options | Notes |
| --- | --- | --- |
| `freshness` | `--path`, `--json` | Ungated, read-only, always exits 0. The only safe poll. |
| `brief` | `--budget INT` (2000, min 64), `--path`, `--json` | Token-budgeted orientation pack. What the SessionStart hook runs. |
| `status` | `--path`, `--json` | Last index run, node/edge counts by type, active file count. |
| `explain project` | `--path`, `--json` | Root, counts, languages, entrypoints. |
| `change-context` | `--base REF`, `--path`, `--json` | Explains the working-tree diff, or `merge-base(BASE,HEAD)..HEAD`. Impact, tests, docs. |

### Locating

| Command | Options | Notes |
| --- | --- | --- |
| `search QUERY` | `--limit INT` (10), `--type NODETYPE`, `--path`, `--json` | FTS5 bm25 with name and path boosting. `--json` emits an array. |
| `find-symbol NAME` | `--exact`, `--limit INT` (20), `--path`, `--json` | Functions, classes, methods, variables, modules. |
| `docs-for-code TARGET` | `--limit INT` (50), `--path`, `--json` | Markdown sections referencing a file or unique symbol. |
| `code-for-docs DOC_PATH` | `--heading TEXT`, `--limit INT` (50), `--path`, `--json` | Code referenced by a Markdown doc. |

### Understanding

| Command | Options | Notes |
| --- | --- | --- |
| `explain file FILEPATH` | `--path`, `--json` | Symbol tree, imports and importers, calls out and in, instantiations both ways, env vars, tests, docs. |
| `trace config NAME` | `--path`, `--json` | Where a config key or env var is defined and where it is read at runtime. |
| `trace data-flow START` | `--depth INT 0-10` (4), `--direction in\|out\|both` (both), `--path`, `--json` | Bounded traversal from a route, event, file, or symbol. |
| `impact TARGET` | `--change-type modify\|added\|deleted\|renamed` (modify), `--depth INT 1-10` (3), `--path`, `--json` | Confidence-bucketed blast radius, recommended tests, affected docs, historical co-change. |

### History

All three are additionally gated on the Git history window, so they can fail
with a `history_*` status even when the file index is current.

| Command | Options | Notes |
| --- | --- | --- |
| `history co-change FILEPATH` | `--limit INT` (20), `--path`, `--json` | Files that historically change together. A heuristic, labeled as one. |
| `history hotspots` | `--limit INT` (20), `--path`, `--json` | Churn: commit counts and line deltas per file. |
| `history owners [FILEPATH]` | `--limit INT` (10), `--path`, `--json` | Observed contribution history. Explicitly not a claim about who may approve a change. |

### Memory

| Command | Options | Notes |
| --- | --- | --- |
| `memory read` | `--topic TEXT`, `--limit INT` (10), `--path`, `--json` | Recent durable sessions. |
| `memory verify` | `--limit INT` (1000), `--path`, `--json` | Re-checks memory anchors against the current graph: verified, drifted, invalidated, unanchored. |
| `memory write` | `--summary TEXT` or `--from-file FILE`; repeatable `--decision`, `--assumption`, `--open-question`, `--changed-file`, `--next-step`; `--path` | Appends a session to the graph and mirrors it into `AGENT_HANDOFF.md`. Ungated. Always JSON. |

### Maintenance

| Command | Options | Notes |
| --- | --- | --- |
| `index [PATH]` | `--no-incremental`, `--no-history` | Incremental by default and idempotent. Roughly 0.5 s for 1,200 files, 2.6 s for 6,000; a no-change re-run is well under a second. |
| `init [PATH]` | | Creates `.repobrain/config.json` and an empty database. `index` does this implicitly. |
| `report` | `--path` | Writes `.repobrain/graph_report.md` and `.html`. No `--json`. |
| `install-agent [PATH]` | `--git-hooks` | Writes the SessionStart hook, `.mcp.json`, this skill, a `CLAUDE.md` block, and a `.gitignore` entry. |
| `uninstall-agent [PATH]` | | Removes only what RepoBrain owns. |

## Output shapes

Most commands print human text by default and JSON with `--json`. Three
exceptions: `install-agent`, `uninstall-agent`, and `memory write` always emit
JSON; `init`, `index`, and `report` always emit text.

`search`, `find-symbol`, `docs-for-code`, and `code-for-docs` emit a top-level
JSON **array**. Everything else emits an object.

When a small stale diff is auto-repaired, `Freshness: Automatically reindexed
N changed file(s).` goes to **stderr** in text mode. Under `--json` the same
fact appears in the payload's `freshness` key instead.

## Exit codes

`0` success. `2` bad usage — an unknown flag, an out-of-range `--depth`, or
`memory write` with neither `--summary` nor `--from-file`.

`1` is an error with a message on stderr, and the message distinguishes four
different situations:

| Message begins | Meaning | Do this |
| --- | --- | --- |
| `Index is stale (` | The gate refused to serve facts from a moved tree. | `repobrain index` |
| `No RepoBrain database at` | This repo was never indexed. | `repobrain index .` |
| `This database indexes '<other>'` | The database belongs to a different root. | Run from that root. |
| anything else | The target did not resolve, or the diff could not be read. | Re-query with `search` to find the right name. |

## The freshness model

Staleness has two axes, and `is_stale` is set by either.

**The tree moved.** Computed from file **size and mtime**, not content hashes,
and not the Git HEAD. A `git checkout` that restores byte-identical files still
marks them stale, because mtimes moved. The Git history sub-graph is checked
separately, since a commit or rebase moves HEAD without touching the tree.
Reported as `out_of_date_count`.

**The extractor moved.** `extractor_changed` is true when the index was built
by a different set of parsers than the ones installed now. The files are
byte-identical, so no file count expresses it — the facts derivable from them
changed, not the files. Repaired by re-extracting everything, and deliberately
exempt from the size thresholds below, since the tree is unchanged.

A stale *tree* diff of at most **10 files and 256 KiB** is repaired in place
before the query runs. Anything larger is refused. `--no-auto-index` means
*refuse instead of repairing* — it never permits a stale answer.

When `freshness` reports `"status": "unavailable"`, dispatch on `reason_code`,
never on `reason`:

| `reason_code` | Meaning | Do |
| --- | --- | --- |
| `no_index` | This repository was never indexed. | `repobrain index .` |
| `schema_mismatch` | The database was built by a different RepoBrain. | `repobrain index .` to migrate it. |
| `unreadable` | The database exists but could not be opened. | Report it; do not fall back to grep silently. |

<!-- repobrain:skill:owned -->

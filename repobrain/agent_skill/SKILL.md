---
name: repobrain
description: Use when working in a repository that has a RepoBrain index — a .repobrain/ directory, or a RepoBrain brief in the session context — and you need to know what the code is, where something lives, what calls or imports what, what a change would break, which tests cover it, or what earlier sessions decided.
---

# RepoBrain

This repository is indexed into a queryable graph of its code, documentation,
configuration, and Git history. Query the graph. One call returns what a grep
fan-out approximates in twenty, and every fact carries a `file:line`.

## Find the command first

`repobrain` is an installed package, not a file in the repository. **Its
absence from the checkout tells you nothing about whether it is available.**
Probe with a real subcommand, not `--help` — an older build on `PATH` answers
`--help` and still fails with `No such command` on anything newer:

```bash
repobrain freshness              # installed on PATH
.venv/bin/repobrain freshness    # project virtualenv
```

If neither answers, the SessionStart hook command in `.claude/settings.json`
is a known-good invocation, usually `uvx --from <source> repobrain ...`. Copy
it up to `repobrain` and append your subcommand. Do not guess at the source;
RepoBrain is often installed from a Git URL rather than PyPI.

If a `repobrain` MCP server is connected, prefer its tools — same graph, no
subprocess.

## The loop

**Orient.** `repobrain freshness --json` — ungated, read-only, always exits 0.

**Locate.** `repobrain search "<term>"` for a concept; `repobrain find-symbol
<name>` for a name you already know.

**Understand.** `repobrain explain file <path>` returns symbols, imports and
importers, callers and callees, env vars, covering tests, and related docs in
one call. `explain` has exactly two forms, `explain project` and `explain
file`, so use `find-symbol` for a symbol rather than inventing a subcommand.
`repobrain trace config <NAME>` locates where a setting is defined and read;
`repobrain trace data-flow <start>` follows a route or symbol through the
graph.

**Check before editing.** `repobrain impact <path-or-symbol>` returns the
blast radius, the tests to run, and the docs to update. Run it *before* you
change shared code, not after.

**Record.** Close the session with `repobrain memory write --summary`, plus
repeatable `--decision`, `--assumption`, `--open-question`, `--changed-file`,
and `--next-step`. Each is anchored to the code it names, so `memory verify`
can later report which notes drifted. `AGENT_HANDOFF.md` is a rendered mirror
of that graph — write through the command; notes typed into the file are never
anchored and never come back from `memory read`.

Add `--json` to any of these to parse rather than read.

## When a command exits 1

Read the message — it names one of four situations.

| Message begins | Do this |
| --- | --- |
| `Index is stale (` | Run `repobrain index`, then re-run your query. |
| `No RepoBrain database at` | Run `repobrain index .` once, then query. |
| `This database indexes '<other>'` | Run from the root it names. |
| anything else | The target did not resolve. Find the real name with `search`. |

A stale index is a refusal to serve facts from a tree that has moved, not a
malfunction. Reindexing is incremental and takes about a second. Do not fall
back to grep, and do not reach for `--no-auto-index` — it means *refuse
instead of repairing*, making a block more likely, never less.

Full command surface, output shapes, and the freshness model: `reference.md`,
next to this file.

<!-- repobrain:skill:owned -->

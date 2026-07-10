# Next Session Prompt

Copy-paste the prompt below to start the next agent session. It scopes the
session to M11 (session-start briefing), the top priority under Suggested
Next Steps in `AGENT_HANDOFF.md`.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents.

Start by reading AGENT_HANDOFF.md in full, then prd.md sections 3, 6, and 31,
and DECISIONS.md D14-D21. Pull the latest main before creating a branch.

Your milestone is M11: Session-start briefing ("push, don't pull"), the top
item under Suggested Next Steps in AGENT_HANDOFF.md. Agents don't call MCP
tools unprompted, so RepoBrain must deliver orientation to the agent at
session start with zero agent behavior change.

Implement:
1. A `repobrain brief` CLI command (and matching `project_brief` MCP tool)
   that emits a token-budgeted orientation pack built entirely from existing
   graph queries and agent memory: project purpose, main subsystems,
   entrypoints, key routes and config, active assumptions, open questions,
   and the most recent memory entries.
2. A `--budget N` option (approximate tokens; a chars/4 heuristic is fine —
   document the choice). Sections must degrade gracefully in a fixed
   priority order as the budget shrinks; never truncate mid-fact.
3. Every fact in the brief must keep provenance (path:line) — same
   source-grounding rules as all other output. --json and plain-text modes.
4. A staleness guard: if indexed state no longer matches the working tree
   (reuse the existing size+mtime diff), say so at the top of the brief and
   report how many files are out of date. Do not silently serve stale facts.
5. `repobrain install-agent` (minimal version): writes a SessionStart hook
   and CLAUDE.md snippet into the target repo so Claude Code injects the
   brief automatically. Idempotent; never overwrite human-authored content.
6. Tests: unit tests for budget degradation and staleness detection, plus
   fixture-repo and self-hosting coverage (the brief for RepoBrain itself
   must mention its own purpose and at least one real subsystem).

Constraints:
- No external hosted API requirements; everything offline and deterministic.
- Reuse repobrain/graph/queries.py — do not fork query logic into the CLI or
  MCP layers (this drift was a review finding on PR #2).
- Read the Known Pitfalls section of AGENT_HANDOFF.md before touching
  indexing or edges.

When done: run the full test suite, update AGENT_HANDOFF.md and DECISIONS.md,
and report what changed, how to run it, what tests pass, known limitations,
and the recommended next milestone (expected: M12 freshness automation).
```

## Scoping notes

- A minimal `install-agent` is pulled forward from the distribution item
  (Suggested Next Steps #6) because a brief nobody wires up delivers no
  value — distribution of the brief is part of the feature.
- The staleness *check* (not auto-reindex) is folded in from M12: serving a
  stale brief on day one would undermine trust in exactly the way the
  handoff warns about. Full freshness automation (auto-reindex-on-query,
  git hooks) remains M12.

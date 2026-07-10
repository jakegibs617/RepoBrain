# Next Session Prompt

Copy-paste the prompt below to start the next agent session. It scopes the
session to M12 (freshness automation), the highest-priority undelivered item
under Suggested Next Steps in `AGENT_HANDOFF.md`.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents.

Start by reading AGENT_HANDOFF.md in full, especially Known Pitfalls, then
prd.md sections 3, 5.6, 6, 16, and 31, and DECISIONS.md D12 and D21-D22. Pull
the latest main before creating a branch.

Your milestone is M12: Freshness automation. M11 warns when a brief is stale;
M12 must safely repair small diffs before any query serves misleading facts
and provide optional Git lifecycle hooks for larger changes.

Implement:
1. A shared query freshness gate used by every read-only CLI query and every
   read-only MCP query. It must compare the configured working tree to indexed
   state before executing the query and return/report a consistent freshness
   result. Do not duplicate the gate across CLI and MCP implementations.
2. Auto-reindex-on-query for small diffs, enabled by default. Define a
   conservative, documented threshold using changed/added/deleted file count
   and total changed bytes. Reuse the existing incremental Indexer; do not
   create a second indexing path.
3. For diffs over the threshold, do not serve stale facts silently and do not
   launch an unexpectedly expensive rebuild. Return a clear actionable result
   telling the caller to run `repobrain index`, including counts and threshold
   values. Provide an explicit per-command opt-out for automation where an
   agent needs a read-only check.
4. Ensure `repobrain brief` and `project_brief` can never return stale facts:
   small diffs are repaired first; large diffs produce only the freshness
   warning/error envelope, not the old brief sections.
5. Extend `repobrain install-agent` with optional, idempotent local git
   post-commit and post-merge hooks that run incremental indexing. Preserve
   existing hooks by using a RepoBrain-owned executable hook plus a safe
   dispatcher strategy; never overwrite human hook content. Add uninstall or
   exact removal for artifacts RepoBrain owns.
6. Tests: threshold boundaries, additions/changes/deletions, CLI/MCP parity,
   brief non-staleness, failure atomicity, hook coexistence/idempotency/removal,
   fixture repositories, and self-hosting. Assert that a failed auto-index
   never causes a query to serve stale graph facts.

Constraints:
- No external hosted API requirements; everything offline and deterministic.
- Reuse scanner, incremental diff, Indexer, graph queries, and M11 briefing;
  do not fork query or indexing logic into CLI/MCP layers.
- Preserve D12's known size+mtime tradeoff unless a measured replacement is
  explicitly documented in DECISIONS.md.
- Keep query behavior source-grounded and JSON-safe, with matching semantics
  across CLI and MCP.
- Read Known Pitfalls before changing transaction order or reconciliation.

When done: run the full test suite, update AGENT_HANDOFF.md and DECISIONS.md,
and report what changed, how to run it, what tests pass, known limitations,
and the recommended next milestone (expected: M13 diff-aware change context).
```

## Scoping notes

- M12 automates freshness only for a single already-initialized repository;
  multi-repo orchestration remains a deliberate non-goal.
- Hook installation is optional because teams may already manage hooks with a
  framework. Coexistence and exact ownership are acceptance requirements.
- The next prompt should scope M13 to working-diff/branch change context,
  impacted symbols, recommended tests, and MENTIONS-based stale-doc detection.

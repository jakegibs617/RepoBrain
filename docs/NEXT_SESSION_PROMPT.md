# Next Session Prompt

Copy-paste the prompt below to start the next agent session. It scopes the
session to M13 (diff-aware change context), the highest-priority undelivered
item under Suggested Next Steps in `AGENT_HANDOFF.md`.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents.

Start by reading AGENT_HANDOFF.md in full, especially Known Pitfalls, then
prd.md sections 3, 5.3, 6, 13.5, 20, 23, and 31, and DECISIONS.md D16, D20-D23.
Pull the latest main before creating a branch.

Your milestone is M13: Diff-aware change context. An agent's unit of work is a
change, not a lookup. RepoBrain must turn the current working diff or branch
diff into grounded impact context, recommended tests, and stale-documentation
evidence before commit or PR review.

Implement:
1. A shared `change_context` query plus `repobrain change-context` CLI command
   and matching `change_context` MCP tool. Support the working tree by default
   (staged + unstaged + untracked) and a branch/base mode such as `--base REF`.
   Use Git plumbing locally and deterministically; do not require GitHub or a
   hosted API.
2. Map changed paths and changed line ranges to indexed File and symbol nodes.
   Distinguish added, modified, renamed, and deleted paths. Preserve old/new
   path information for renames and expose unparsed/binary changes honestly.
3. Reuse existing impact analysis to report impacted symbols, confidence
   buckets, and recommended tests. Aggregate and deduplicate across all changed
   targets without losing the source edge, confidence, or changed-path reason.
4. Add stale-doc detection using existing MENTIONS edges: when changed code is
   referenced by a Markdown document/section that is not itself changed, flag
   that documentation as potentially stale. Do not claim semantic staleness;
   report evidence, changed target, referencing section, path:line provenance,
   confidence, and why the document was considered unchanged.
5. Run the M12 freshness gate before graph traversal. Account for the paradox
   that auto-indexing the working change updates graph facts: capture the Git
   diff first, then gate/index, then resolve the captured change set. Large or
   failed freshness repairs must return no stale change-context facts.
6. Plain-text and JSON output must be stable and source-grounded. Include a
   concise tests-to-run section and a docs-to-review section suitable for a
   pre-commit or PR workflow.
7. Tests: staged/unstaged/untracked changes, branch/base diffs, add/modify/
   rename/delete, changed-line-to-symbol mapping, multi-target deduplication,
   stale-doc positive and negative cases, ambiguity, CLI/MCP parity, freshness
   failure, non-Git repositories, fixture repositories, and self-hosting.

Constraints:
- No external hosted API requirements; everything offline and deterministic.
- Reuse MENTIONS, impact analysis, GraphStore, and the M12 freshness gate. Do
  not duplicate Git parsing or query logic in CLI and MCP layers.
- Preserve D16/D20 confidence and inference semantics. Stale-doc output is a
  review recommendation, never a correctness claim.
- Do not mutate the user's Git index, working tree, commits, or branches.
- Read Known Pitfalls before changing reconciliation or transaction ordering.

When done: run the full test suite, update AGENT_HANDOFF.md and DECISIONS.md,
and report what changed, how to run it, what tests pass, known limitations,
and the recommended next milestone (expected: Git history as an extractor).
```

## Scoping notes

- M13 analyzes one repository and one captured diff at a time; multi-repo
  change sets remain a deliberate non-goal.
- Stale-doc detection is intentionally evidence-based: unchanged docs that
  reference changed code are candidates for review, not automatically wrong.
- The next prompt should scope Git history extraction to deterministic local
  co-change coupling, churn hotspots, ownership evidence, and impact blending.

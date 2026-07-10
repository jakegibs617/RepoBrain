# Next Session Prompt

Copy-paste the prompt below to start the next agent session. It scopes the
session to deterministic Git history extraction, the highest-priority
undelivered item under Suggested Next Steps in `AGENT_HANDOFF.md`.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents.

Start by reading AGENT_HANDOFF.md in full, especially Known Pitfalls, then
prd.md sections 3, 5, 6, 13.5, 23, 29, and 31, and DECISIONS.md D2, D16,
D21, and D23-D24. Pull the latest main before creating a branch.

Your milestone is Git history as a deterministic extractor. Static analysis
misses relationships such as templates changing with handlers, migrations
changing with models, and config changing with deployment files. RepoBrain
must mine local commit history for grounded co-change, churn, and ownership
evidence and blend it conservatively into impact analysis.

Implement:
1. A deterministic Git history extractor driven only by local Git plumbing.
   Traverse a configurable recent window (document a default by commit count),
   honor renames, and capture commit id/time/author plus changed paths and
   additions/deletions. Never require GitHub or a hosted API.
2. Persist history evidence in the existing SQLite graph/store with stable
   identities and provenance. Add only the minimal schema vocabulary earned
   by queries (for example commit nodes and co-change edges); do not overload
   static dependency edge types. Re-indexing the same history must converge,
   and rewritten/shortened history must remove extractor-owned stale facts.
3. Compute file-level co-change coupling with explicit support counts and a
   normalized score that discounts broad commits. Exclude merge-only noise,
   generated/vendor paths, RepoBrain's own state, and configurable oversized
   commits. Preserve the commit ids supporting every relationship.
4. Expose churn hotspots (commit count plus added/deleted lines) and ownership
   evidence (author contribution counts and recency). Treat ownership as
   observed history, never as an authorization or CODEOWNERS claim. Provide
   shared queries plus CLI and MCP surfaces with stable plain/JSON output.
5. Blend co-change into `impact_analysis` and M13 `change_context` as a
   separately labeled historical-evidence bucket. Do not allow history alone
   to become high-confidence static impact; retain supporting commits, score,
   path provenance, and an explanation of the heuristic.
6. Integrate extraction with indexing through one explicit phase or command.
   Define how M12 freshness interacts with history changes (new commits,
   rebases, shallow clones). Fail honestly for non-Git/shallow/incomplete
   history and never mutate branches, refs, the index, or the working tree.
7. Tests: idempotence, rename continuity, history rewrite cleanup, broad-commit
   discounting, merge/generated exclusions, score/support math, churn,
   ownership/recency, impact blending, change-context integration, CLI/MCP
   parity, shallow/non-Git behavior, fixture Git histories, and self-hosting.

Constraints:
- No external hosted API requirements; everything offline and deterministic.
- Use subprocess argument arrays and read-only Git commands; never invoke a
  shell with repository-controlled text.
- Preserve source-grounding and confidence semantics. Co-change is correlation,
  not dependency or causation, and must be labeled accordingly.
- Reuse GraphStore, impact analysis, M12 freshness, and M13 change context;
  do not fork query logic into CLI or MCP layers.
- Read Known Pitfalls before changing indexing transaction ordering.

When done: run the full test suite, update AGENT_HANDOFF.md and DECISIONS.md,
and report what changed, how to run it, what tests pass, known limitations,
and the recommended next milestone (expected: memory verification).
```

## Scoping notes

- Analyze one repository's reachable local history; cross-repository identity
  and remote contribution data remain non-goals.
- Co-change must remain visibly heuristic and lower-confidence than observed
  imports/calls/config/doc edges.
- The next prompt should scope memory verification to anchoring entries to
  graph nodes, validating them after reindex, and surfacing invalidated
  assumptions in the session brief.

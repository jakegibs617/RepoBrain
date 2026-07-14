# Next Session Prompt

Copy-paste the prompt below to start the next agent session. This session has
**two phases**: first land the in-flight M14 refinements (the branch is not
green), then deliver the M15 memory-verification milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per iteration on a feat/ branch, auto-merge only when green (full pytest suite
passes and /code-review confirmed findings are fixed), otherwise stop rather
than merge anything questionable.

Start by reading AGENT_HANDOFF.md in full (especially Known Pitfalls), then
prd.md sections 3, 5, 6, 13.6, 18, 29, and 31, and DECISIONS.md D2, D21, D23,
D24, and D25. Read repobrain/memory.py and repobrain/briefing.py before
designing anything.

== PHASE 0 (do this first): land the in-flight M14 git-history work ==

The branch feat/m14-git-history-extractor has committed the extractor
(commit 2553fec) but carries an UNCOMMITTED, UNREVIEWED refinement pass. Run
`git status` and `git diff` to see it: a combined single-spawn rev-parse
probe, bare-repository handling, EXTRACTOR_VERSION bumped to 2, a
MAX_SUPPORTING_COMMITS_STORED cap on edge metadata, and a file_node_id
refactor across history.py, change_context.py, queries.py, schema.py,
store.py, freshness.py, cli.py, and mcp_server.py.

The full suite currently passes (164 tests). Do NOT assume the refinements are
finished just because they are green — read the whole diff critically before
trusting it. Confirm in particular: the EXTRACTOR_VERSION=2 bump actually
forces re-extraction on upgrade (a stale cache must not survive); the
supporting-commit cap preserves evidence (the full set stays derivable from
git_commit_files, not silently lost); the file_node_id refactor is consistent
everywhere the old node_id was used; and the extractor's status contract is
"extracted" (history.py:321), distinct from the freshness gate's "ok"
(history.py:637) — do not let those two drift back together.

Then run the FULL suite, run /code-review and fix confirmed findings, look for
anything half-finished in the diff, update AGENT_HANDOFF.md and DECISIONS.md
for the refinements, commit, and merge M14 when green. If you cannot get it
green and clean, stop and report — do not merge.

== PHASE 1 (main milestone): memory verification ==

Agent memory is currently append-only and never checked against reality, so
decisions and assumptions rot silently as the code moves. RepoBrain must
anchor memory entries to graph facts and tell the next session which
remembered claims no longer hold. This milestone is fully offline and
deterministic — no hosted APIs and no local/LLM models; it only checks whether
anchored facts still exist and where they moved.

Implement:
1. Anchor extraction: when a memory entry is written, resolve its referenced
   files and symbols (reuse the existing exact-path and unambiguous-symbol
   resolution rules; never fuzzy-match) into stored anchors with node ids,
   paths, and the resolution provenance. Entries with no resolvable
   references remain valid but unanchored — that is honest, not an error.
2. Validation on demand: a shared check that re-resolves every stored anchor
   against the current graph and classifies each entry as verified, drifted
   (anchor moved: same identity at a new line/path via rename continuity),
   invalidated (anchored fact no longer exists), or unanchored. Never delete
   or rewrite memory content: verification annotates, append-first stays.
3. Surfaces: `repobrain memory verify` (CLI) and a matching MCP tool, both
   behind the M12 freshness gate, with stable plain/JSON output that carries
   per-anchor evidence (what was expected, what was found, provenance).
4. Brief integration: the M11 session brief's memory section must surface
   invalidated and drifted entries first, with an explicit count line (for
   example "2 remembered assumptions no longer hold"), inside the existing
   atomic budget rules. A brief must never present an invalidated memory as
   a current fact.
5. Lifecycle: validation runs on the shared read path the same way history
   refresh does (define and document the interaction with the M12 gate and
   with `repobrain index`); results are deterministic and re-runnable, and
   re-validating an unchanged graph converges to identical output.
6. Tests: anchor resolution (paths, unique symbols, ambiguity skipped),
   verified/drifted/invalidated/unanchored classification, rename
   continuity, no mutation of Markdown or stored entries, brief surfacing
   and budget behavior, CLI/MCP parity, freshness-gate interaction, and
   self-hosting (RepoBrain's own AGENT_HANDOFF memory validates cleanly).

Constraints:
- No external hosted API requirements; everything offline and deterministic.
- Memory stays append-first and human-readable; verification adds structured
  annotations, never destructive edits.
- Preserve source-grounding: every verdict carries the evidence that produced
  it, and ambiguity is reported as ambiguity rather than guessed through.
- Reuse GraphStore, the M12 gate, existing queries, and the M11 brief
  assembly; do not fork query logic into CLI or MCP layers.
- Drift detection may reuse M14's rename continuity (git_commit_files maps old
  paths to current identities) but must degrade gracefully when history is
  unavailable.
- Read Known Pitfalls before touching indexing transaction ordering or File
  node identity.

When done: run the full test suite, run /code-review and fix confirmed
findings, update AGENT_HANDOFF.md and DECISIONS.md, and report what changed,
how to run it, what tests pass, known limitations, and the recommended next
milestone (expected: distribution — uvx packaging plus one-step
install-agent). Rewrite this file (docs/NEXT_SESSION_PROMPT.md) to scope that
next milestone.
```

## Scoping notes

- Phase 0 is a gate, not optional: M14 must be green and merged before M15
  builds on its rename continuity. If Phase 0 can't be made green, stop after
  reporting — do not start Phase 1 on an unstable base.
- Verification is per-repository and graph-grounded; no cross-repo identity,
  no semantic/LLM judgment of whether prose is still true — only whether the
  anchored facts still exist and where they are now.
- The milestone AFTER memory verification should scope distribution:
  `uvx repobrain` packaging and a single `repobrain install-agent` step
  writing `.mcp.json`, the CLAUDE.md snippet, and hooks together.
- Local-model semantic extraction (Ollama, machine-aware routing, OOM
  monitoring) is a deliberately deferred, LATER direction — not part of M15
  or distribution. Keep those milestones offline/deterministic; only pick up
  local models when a milestone explicitly calls for semantic signal.

# Next Session Prompt

Copy-paste the prompt below to start the INSTANTIATES-edges + orphaned-EnvVar-sweep milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls) and
DECISIONS.md D16 (CALLS confidence ladder) and D17 (EnvVar repo-global
identity), then repobrain/parsers/code_treesitter.py (`_add_call_edge`,
`_resolve_plain_call`/`_resolve_self_call`, `CodeParser.finish_run`'s
cross-file name-match pass, and `_extract_env`/EnvVar handling), and
repobrain/graph/store.py (`delete_orphan_edges`, and the indexer's
`_cleanup_directories` liveness-sweep pattern in
repobrain/indexing/indexer.py). Also note: `EdgeType.INSTANTIATES` already
exists in repobrain/graph/schema.py — it has never been emitted by any
extractor, so this milestone activates a reserved-but-unused vocabulary
slot rather than adding a new one.

Your milestone has two parts, both small precision/graph-hygiene gaps
carried since Milestone 3 (see AGENT_HANDOFF.md "Open Questions"):

## Part 1 — INSTANTIATES edges for constructor calls

`ClassName()` call expressions are currently indistinguishable from any
other bare call and are skipped for precision (D16). Mirror the existing
CALLS confidence ladder exactly, using the same resolution machinery
already in code_treesitter.py rather than inventing new logic:

1. When a bare call's callee name resolves to a known Class (same-file via
   `classes_by_name`, or import-qualified via `module_aliases`/
   `symbol_aliases`), emit `Function/Method/Module INSTANTIATES Class` at
   confidence 0.9, `is_inferred=0` — the same observed tier as same-file/
   import-qualified CALLS.
2. Remaining bare calls whose name matches a Class only by name (not
   resolved via same-file/import) go through `finish_run`'s existing
   post-index batched-candidate-lookup pass (the one that already resolves
   CALLS by exactly-one-global-match): extend it, or add a parallel pass
   reusing its batching pattern (chunked `name IN (...)`, not one query per
   name — see D30), so a name that resolves to exactly one Class in the
   whole graph becomes an INSTANTIATES edge at confidence 0.7,
   `is_inferred=1`, `inference_reason="name-match"`. Ambiguous names create
   nothing.
3. Do not double-emit: a callee name that resolves to both a Class and a
   Function/Method (e.g. a factory function shadowing a class name) must
   pick one deterministically and document the rule — don't guess or emit
   both.
4. Decide and document whether Python/JS/PHP/Ruby/Go/Java all get this (the
   PRD's call-resolution scope already covers all of them for CALLS) or
   whether language-specific constructor syntax differences (e.g. Go has no
   `new ClassName()` scanned distinctly — struct literals like
   `Foo{}`/`&Foo{}` are a different tree-sitter shape than a call
   expression) mean some languages stay out of scope for now; precision
   over recall — an unsupported language's constructor calls should do
   nothing, not guess.
5. Add tests mirroring the existing CALLS test shapes in
   tests/test_code_parser.py: same-file resolved INSTANTIATES at 0.9,
   cross-file unique-name-match inferred at 0.7, ambiguous name skipped,
   and incremental convergence (a class added/renamed/removed updates or
   orphans the edge via the existing orphan-edge sweep, same as CALLS/
   IMPORTS already do — no new cleanup mechanism should be needed).
6. Impact analysis / data-flow queries in repobrain/graph/queries.py may or
   may not need to surface INSTANTIATES depending on what "safer changes"
   value it adds — investigate whether `impact_analysis`/`explain file`
   should include instantiation evidence the way they include CALLS, or
   whether that's premature; make a documented call either way.

## Part 2 — Sweep orphaned EnvVar nodes

D17 documents that `EnvVar` nodes (id keyed on `("EnvVar", name, "")`) are
deliberately excluded from path-based cleanup so a single reader's deletion
never destroys the shared node — but this means an EnvVar whose *last*
`READS_ENV` edge disappears (e.g. the only file reading `STRIPE_KEY` is
deleted or edited to stop reading it) lingers forever as an edgeless node.

1. Add a bounded sweep — analogous to the existing Directory-liveness sweep
   in `Indexer._cleanup_directories` and `GraphStore.delete_orphan_edges` —
   that removes `EnvVar` nodes with zero incoming `READS_ENV` edges. Decide
   where it runs (inside the index transaction, after `delete_orphan_edges`
   so edge cleanup has already happened, is the natural spot — verify against
   Known Pitfalls' ordering constraints) and whether it needs to run on
   every index (cheap: bounded `SELECT` for EnvVar nodes with no matching
   edge, likely a handful of rows even at scale) or only when env-reading
   files changed.
2. Add tests: an EnvVar's last reader is deleted → node is swept; an
   EnvVar's last reader stops reading it (file edited) → node is swept; an
   EnvVar with multiple readers loses one → node survives; a fresh EnvVar
   with no readers yet (shouldn't be reachable via current extraction, but
   verify the sweep doesn't need one to exist first) doesn't crash anything.
3. Confirm this doesn't reintroduce a per-row query loop at scale (D30) —
   one bounded `SELECT`/`DELETE` pair covering all orphaned EnvVars in one
   run, not one query per node.

Constraints:
- No hosted API, model, embeddings, network, Docker, or external service.
- Precision over recall: an ambiguous constructor-name match must not
  create a guessed edge, matching CALLS' existing rule exactly.
- Do not weaken any existing CALLS/IMPORTS resolution, the orphan-edge
  sweep, or any D30 scale-hardening invariant (no per-row queries in
  begin_run/finish_run/the transaction where a batched approach is
  available).
- Do not push or publish without explicit user permission.
- When running the full pytest suite from a worktree, remember the primary
  repo's `.venv` is an editable install pinned to the primary repo's own
  path — run with `PYTHONPATH` pointed at the checkout under test (see
  AGENT_HANDOFF.md Known Pitfalls), or you will silently test stale code.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md (a new D-numbered entry) with what
was implemented and any documented scope decisions (which languages get
INSTANTIATES, whether impact analysis surfaces it, sweep timing), and
rewrite this file for the next highest-priority milestone. If no further
open items remain and no small carried-over gap is a clear next step, use
your judgment against the PRD's product goals (§6.2) — the deliberate
embeddings/multi-repo non-goals are flagged in AGENT_HANDOFF.md as a
candidate for a dedicated planning pass rather than this loop's next
cadence by default; revisit that framing if it still seems right, or
propose otherwise with reasoning.
```

## Scoping notes

- Both parts reuse existing, already-proven machinery (CALLS' confidence
  ladder and batched cross-file resolution; the orphan/liveness sweep
  pattern) rather than introducing new mechanisms — keep it that way.
- `EdgeType.INSTANTIATES` is already defined in `repobrain/graph/schema.py`
  and unused; this milestone is the first thing to emit it.
- Keep the two parts in one milestone/branch since both are small,
  low-risk, and in the same "close a documented D-series gap" spirit, but
  they are logically independent — implement and test them separately so a
  problem in one doesn't block landing the other.

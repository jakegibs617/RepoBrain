# Next Session Prompt

Copy-paste the prompt below to start the deterministic scale-hardening milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls), DECISIONS.md
D12, D21, D23, D25, D28-D29, repobrain/indexing/indexer.py,
repobrain/indexing/scanner.py, repobrain/graph/store.py,
repobrain/graph/queries.py, repobrain/retrieval/keyword.py, and the existing
self-hosting/incremental/query tests.

Your milestone is deterministic indexing and traversal scale hardening: prove
and improve RepoBrain behavior on repositories above 1,000 files without
trading away precision, freshness, offline operation, or test reliability.

Implement:
1. Add a deterministic synthetic large-repository fixture/generator (at least
   1,000 supported files) that is fast to create, contains known graph/query
   answers, and never enters source control as thousands of fixture files.
2. Instrument and profile full indexing, no-change incremental indexing, a
   small changed-file run, FTS search, symbol/file explanation, and bounded
   graph traversals. Report phase/query timings and relevant work counts.
3. Establish regression tests around deterministic work invariants (files
   parsed, rows rewritten, queries issued, traversal bounds, result identity)
   plus generous end-to-end safety ceilings. Avoid narrow wall-clock assertions
   that vary across machines.
4. Use the measurements to fix confirmed hot paths only. Prefer batching,
   indexes, bounded SQL, and eliminating repeated scans/queries over caches or
   new abstraction layers without evidence.
5. Prove no-change and small-change runs remain proportional to changed work,
   including global reconcilers/history phases whose full-rebuild behavior is
   intentional; label unavoidable repository-wide costs explicitly.
6. Exercise SQLite query plans for the highest-volume lookups and add schema
   indexes only when the plan and corpus demonstrate a real benefit. Preserve
   migration compatibility with existing databases.
7. Verify representative results on the large corpus match small-corpus
   semantics, including inference provenance, freshness fail-closed behavior,
   root pinning, and bounded depth/limit contracts.
8. Document the benchmark shape, reproducible command, measured baseline and
   post-change results, hardware-independent invariants, and remaining limits.

Constraints:
- No hosted API, model, embeddings, network, Docker, or external service.
- Do not weaken freshness, repository-root, history-serveability, or local-only
  contracts to make scale tests pass.
- Do not optimize by skipping supported facts, lowering precision, disabling
  global convergence, or silently truncating results outside existing limits.
- Keep the ordinary suite practical and deterministic; mark deliberately slow
  profiling commands separately if they do not belong in every test run.
- Do not push or publish without explicit user permission.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md, report the measured scale scenarios,
before/after results, and test results, and rewrite this file for the next
highest-priority milestone.
```

## Scoping notes

- The target is actionable engineering evidence, not a vanity benchmark.
- Preserve a readable unoptimized baseline before changing hot paths.
- Memory/RSS profiling is useful when locally available but must degrade
  explicitly when optional tooling is absent.
- Distributed indexing, background daemons, hosted databases, and approximate
  semantic/vector retrieval remain out of scope.

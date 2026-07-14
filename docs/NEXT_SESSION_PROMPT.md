# Next Session Prompt

Copy-paste the prompt below to start the framework/runtime adapter milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls), prd.md runtime
and impact-analysis sections, DECISIONS.md D14-D21 and D24-D25, plus
repobrain/parsers/code_treesitter.py, repobrain/parsers/route_parser.py,
repobrain/graph/queries.py, repobrain/indexing/indexer.py, and the Node/Express
fixture and its tests.

Your milestone is deterministic framework/runtime adapters: improve impact and
data-flow precision for common Python and JavaScript web/ORM idioms without
introducing speculative dynamic analysis or framework runtime dependencies.

Implement:
1. Define a narrow adapter interface or reconciler boundary that consumes
   persisted syntax facts and emits source-grounded nodes/edges. Keep parser,
   reconciliation, storage, and query responsibilities separate.
2. Make Express-style inline/module-level route callbacks resolve to precise
   Route/Endpoint-to-handler relationships instead of falling back to a Module
   source when syntax provides a better identity.
3. Add one high-value Python web-framework adapter (choose Flask or FastAPI
   based on deterministic fixture coverage) for decorators, route methods,
   handlers, and import-qualified calls.
4. Add conservative ORM/table flow for one existing fixture path (for example
   SQLAlchemy or a simple model/repository convention). Emit relationships only
   when model/table identity is exact and unambiguous; label inference and
   confidence honestly.
5. Feed the new evidence through shared data-flow, impact-analysis, and
   change-context queries without creating a framework-specific CLI/MCP fork.
6. Cover full and incremental convergence: adapter-side additions, renames,
   deletions, ambiguity, stale-edge cleanup, unchanged callers, and no-change
   idempotency. Add adversarial fixtures that prove fuzzy/dynamic receivers are
   skipped rather than guessed.
7. Document supported patterns, confidence, and explicit limitations.

Constraints:
- No imports or execution of the target application's framework/ORM.
- No hosted API, model, embeddings, or network requirement.
- Reuse deterministic node IDs and run cross-file reconciliation inside the
  index transaction before orphan-edge cleanup.
- Preserve existing Route/Endpoint, CALLS, data-flow depth, history-evidence,
  freshness, and repository-root contracts.
- Do not push or publish without explicit user permission.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md, and report supported framework
patterns, skipped ambiguous cases, test results, and known limitations. Rewrite
this file for the next highest-priority milestone.
```

## Scoping notes

- Prefer one well-tested Python framework and one existing Express path over a
  broad but shallow framework matrix.
- Dynamic receiver dispatch remains out of scope unless a framework adapter
  can ground the receiver through exact import/assignment syntax.
- Protocol-level MCP integration tests and >1,000-file profiling remain the
  next adoption-hardening follow-ups after adapter precision.

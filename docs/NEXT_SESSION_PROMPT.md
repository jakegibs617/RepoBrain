# Next Session Prompt

This loop has run out of small, well-scoped engineering gaps to close.
Recommend a **human planning checkpoint** instead of starting another
milestone automatically. Details below; skip straight to "What a human
should decide" if you're picking this up.

## Why no next milestone is queued

RepoBrain's self-paced post-MVP loop has, across its last several
milestones, worked through every carried-over engineering item that was
named as a follow-on to something already delivered:

- Scale hardening (deterministic indexing/traversal above 1,000 files).
- Go/Java internal import resolution (D31).
- INSTANTIATES edges + orphaned EnvVar sweep (D32) — D16's own named
  follow-on.
- Real `new`/`.new` constructor-syntax capture for JS/TS/PHP/Ruby/Java
  (D33) — D32's own named follow-on.
- Java `ClassName.staticMethod()` qualified-call resolution (D34) — D33's
  own named follow-on, and the last item on this chain: it explicitly
  investigated (via a real tree-sitter parse probe, not assumption)
  whether anything further was resolvable without adding real type
  inference, and concluded `someVar.method()` is out of scope for good —
  a documented boundary, not a gap.

Each milestone in that chain surfaced exactly one further named,
small-and-scoped item and closed it. D34 is the first of these that did
**not** surface a next one: its own "what stays out of scope" section
(type-inference-requiring receiver resolution) is a deliberate, permanent
boundary of this deterministic-first codebase (see D19/D30/D34), not a
task to schedule. Manufacturing a synthetic next slice here (e.g. chasing
another single-digit-percent precision improvement in call resolution, or
generalizing `field_access`-qualified Java calls) would be busywork
disconnected from the PRD's actual product goals (§6.2) — it would not
measurably move "fewer tokens rediscovering structure, safer changes,
durable knowledge" the way the M11–M16/D-series work already has.

## What a human should decide

`AGENT_HANDOFF.md`'s "Suggested Next Steps" has flagged this since the
2026-07-10 product-direction review and it's still the right framing:
every M11–M16 product milestone is now delivered, and the deliberate
non-goals — **embeddings** and **multi-repo support** — are the two
biggest levers left un-pulled. Both are the kind of decision this loop
should not make unilaterally:

- **Embeddings.** Would trade away the deterministic-first stance
  (D-series) that every decision to date has protected, in exchange for
  recall on fuzzier queries semantic/name-based matching can't reach. Needs
  a human call on whether that trade is worth it now, and if so, how to
  keep it optional/local-only rather than compromising the no-network,
  no-hosted-API invariant every existing decision assumes.
- **Multi-repo support.** The whole graph/store model (D8/D10: "database is
  pinned to the repository root it indexes") assumes one repo. Supporting
  more is an architecture decision, not an incremental extractor slice —
  needs scoping before any code gets written.
- Also worth a fresh look in the same pass: whether the CALLS/IMPORTS/
  INSTANTIATES precision-over-recall stance has hit diminishing returns
  (D16 through D34 have each closed a smaller and smaller gap — D34's own
  remaining gap needs a compiler), and whether the next unit of value for
  agent-facing "safer changes" is a different axis entirely (e.g. the
  already-existing MENTIONS/git-history/memory-verification surfaces
  getting deeper investment, vs. more call-graph precision).

If, after that human pass, a genuinely new small engineering gap is
identified, resume this loop's one-milestone-per-`feat/`-branch cadence
against it. Until then, don't self-generate the next prompt here.

## Housekeeping for whoever runs this next

- Read `AGENT_HANDOFF.md` in full (Project Summary, Delivery Status,
  Open Questions, Known Pitfalls, Suggested Next Steps) and `DECISIONS.md`
  D1–D34 before making any planning call — this file is deliberately not a
  substitute for that context.
- The primary repo's `.venv` is an editable install pinned to the primary
  repo's own path; running its pytest from inside a worktree silently
  tests the wrong (stale) code unless `PYTHONPATH` is pointed at the
  worktree checkout under test.
- No hosted API, model, embeddings, network, Docker, or external service —
  unless and until the embeddings question above is explicitly resolved by
  a human, this constraint stays in force.

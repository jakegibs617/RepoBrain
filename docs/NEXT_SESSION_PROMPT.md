# Next Session Prompt

Copy-paste the prompt below to start the `new X()`/`X.new` constructor-capture milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls) and
DECISIONS.md D16 (CALLS confidence ladder) and D32 (INSTANTIATES edges),
then repobrain/parsers/code_treesitter.py in full: the per-language Query
sources (`_QUERY_SOURCES`), `_resolve_plain_call`/`_resolve_module_attr_call`
(the shared INSTANTIATES resolution ladder D32 built), `_PendingImportCall`/
`_resolve_pending_import_calls` (the batched existence check that
deterministically picks CALLS over INSTANTIATES when a same-name Function
and Class both exist), `CodeParser.finish_run` (the cross-file name-match
pass, now covering both CALLS and INSTANTIATES), and each language
extractor's `_extract_calls` (especially `_JsExtractor`, `_PhpExtractor`,
`_RubyExtractor`, and `_JavaExtractor`).

## Background

D32 activated `EdgeType.INSTANTIATES` for constructor calls, but only by
reusing the *existing* call-shaped tree-sitter captures — it added no new
grammar patterns. That means INSTANTIATES only actually fires for Python's
`ClassName()` idiom, which happens to be syntactically identical to a
function call and was already flowing through the shared CALLS-resolution
pipeline. It does *not* fire for:

- JavaScript/TypeScript's `new ClassName(...)` — a `new_expression` node,
  not the `call_expression` the current query captures.
- PHP's `new ClassName(...)` — an `object_creation_expression` node, not the
  `function_call_expression` the current query captures.
- Ruby's `ClassName.new` — this IS a call (a `call` node with a `receiver`
  field), but `_RubyExtractor._extract_calls` unconditionally skips every
  call with a receiver (`# dynamic receiver: skip`), so `.new` sent to a
  known class never even reaches `_resolve_plain_call`.
- Java's `new ClassName(...)` — also `object_creation_expression`, not
  captured; and separately, `_JavaExtractor._extract_calls` doesn't route
  bare/qualified calls through `_resolve_plain_call`/`_resolve_module_attr_call`
  at all today (only `self`/`this`-qualified calls resolve), a pre-existing
  scope limit unrelated to D32.

This milestone closes the JS/TS, PHP, and Ruby gaps — real, idiomatic
constructor syntax in the three languages where it's a small, well-scoped,
grammar-shape addition. Treat Java as optional/stretch (see step 4) since it
also needs the separate, larger `_resolve_plain_call` wiring gap addressed
first, which is arguably its own milestone.

## Your task

1. **JS/TS `new ClassName(...)`.** Add a capture for `(new_expression) @new`
   to `_JS_QUERY` (shared by JS/TS/TSX). In `_JsExtractor`, extract the
   `constructor` field; when it's a bare `identifier`, resolve the class name
   through the *exact same* ladder `_resolve_plain_call` already uses for
   classes (same-file `classes_by_name` at 0.9, import-qualified via
   `symbol_aliases`/`module_aliases` queued through `_PendingImportCall`,
   cross-file name-match via the existing `_pending_calls`/`finish_run`
   pass) — but constrained to Class candidates only, since `new X()` is
   unambiguously a constructor, never a plain call. Do not duplicate the
   ladder; factor out a helper both `_resolve_plain_call`'s classes_by_name
   branch and the new `new_expression` handling can share, or call into
   `_resolve_plain_call`'s class-only logic directly. A qualified
   constructor (`new pkg.ClassName()`, a `member_expression` constructor
   field) is out of scope for this milestone unless it's a trivial reuse of
   `_resolve_module_attr_call` — don't force it.
2. **PHP `new ClassName(...)`.** Same idea: add `(object_creation_expression)
   @new` to the PHP query, extract the class-name field (verify the exact
   field/child shape with a quick tree-sitter parse probe — do not assume
   without checking, the same way D32's Go exclusion was verified against
   the real parse tree, not assumed), and route through the same
   class-only ladder in `_PhpExtractor`.
3. **Ruby `ClassName.new`.** In `_RubyExtractor._extract_calls`, before the
   blanket `if call.child_by_field_name("receiver") is not None: continue`,
   special-case a receiver that is a bare `constant` (Ruby's node type for a
   capitalized identifier) whose method is exactly `new`: resolve the
   constant through the class-only ladder instead of skipping. Every other
   receiver call (a variable, a method chain, anything not a bare constant)
   must keep being skipped exactly as before — do not weaken the existing
   dynamic-receiver precision stance for anything except this one exact
   shape.
4. **Java (decide, don't guess).** Investigate whether wiring
   `_JavaExtractor._extract_calls` to also resolve bare/`new`-qualified
   calls is small enough to fold in, or is a separate pre-existing gap
   deserving its own milestone (it needs `_resolve_plain_call`/
   `_resolve_module_attr_call` wiring Java currently lacks entirely, not
   just a new capture pattern). Make an explicit, documented call either
   way — do not silently skip without a rationale, and do not scope-creep
   into rewriting Java's whole call-resolution path if it turns out large.
5. Add tests mirroring D32's INSTANTIATES test shapes in
   tests/test_code_parser.py for each language you implement: same-file
   `new X()`/`.new` at 0.9 non-inferred, import-qualified at 0.9
   non-inferred, cross-file unique-name-match at 0.7 inferred, ambiguous
   name skipped, and — importantly — a regression test that a *non*-`new`/
   non-`.new` call to the same class name is unaffected (e.g. plain
   `ClassName()` in JS without `new` should still behave exactly as D32 left
   it, not be swept into the new logic twice).
6. Confirm no D30 batching invariant regresses (no per-call queries; reuse
   the existing chunked `finish_run`/`_resolve_pending_import_calls`
   passes) and that Go/Java's existing exclusions/limits are undisturbed.

Constraints:
- No hosted API, model, embeddings, network, Docker, or external service.
- Precision over recall: an unsupported/ambiguous shape does nothing, never
  guesses.
- Do not weaken any existing CALLS/INSTANTIATES/IMPORTS resolution, the
  orphan-edge sweep, the EnvVar sweep, or any D30 scale-hardening invariant.
- Do not push or publish without explicit user permission.
- When running the full pytest suite from a worktree, remember the primary
  repo's `.venv` is an editable install pinned to the primary repo's own
  path — run with `PYTHONPATH` pointed at the checkout under test (see
  AGENT_HANDOFF.md Known Pitfalls), or you will silently test stale code.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md (a new D-numbered entry) with what
was implemented (which languages/shapes got capture, the Java decision, any
scope left out) and rewrite this file for the next highest-priority
milestone. If no further small carried-over gap is a clear next step, use
your judgment against the PRD's product goals (§6.2) — the deliberate
embeddings/multi-repo non-goals are flagged in AGENT_HANDOFF.md as a
candidate for a dedicated planning pass rather than this loop's next
cadence by default; revisit that framing if it still seems right, or
propose otherwise with reasoning.
```

## Scoping notes

- This milestone is a direct, small follow-on named by D32 itself while
  scoping INSTANTIATES down to "reuse only existing captures" — it is not a
  new open question, just the next slice of already-planned work.
- JS/TS, PHP, and Ruby are the concrete, bounded targets. Java is explicitly
  a judgment call for the next session, not a requirement, because it needs
  a separate pre-existing wiring gap closed first (Java never routes bare
  calls through the shared resolution ladder at all, unrelated to capture
  patterns).
- Reuse D32's resolution machinery (`_resolve_plain_call`'s class-only path,
  `_PendingImportCall`/`_resolve_pending_import_calls`, `finish_run`'s
  batched name-match pass) rather than inventing a parallel one — the only
  new work should be grammar capture + field extraction per language, not a
  second resolution ladder.

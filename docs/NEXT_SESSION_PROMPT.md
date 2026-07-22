# Next Session Prompt

Copy-paste the prompt below to start the Java `method_invocation` call-resolution milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls) and
DECISIONS.md D16 (CALLS confidence ladder), D19 (language wiring tiers),
D31 (Go/Java internal import resolution), D32 (INSTANTIATES edges), and D33
(real constructor-syntax capture, including the Java investigation that
named this milestone). Then read repobrain/parsers/code_treesitter.py in
full: `_resolve_plain_call` (the shared class_only-parameterized ladder),
`_resolve_self_call`, `_resolve_module_attr_call`, `_PendingImportCall`/
`_resolve_pending_import_calls`, `CodeParser.finish_run`, and
`_JavaExtractor` in full (`_extract_imports`/`_resolve_java_import`,
`_extract_calls`, and the `CLASS_SCOPES`/`FUNC_SCOPES` used for scope
walking).

## Background

Every other first-class-or-better language (Python, JS/TS, PHP, Ruby) routes
its bare and qualified calls through the shared `_resolve_plain_call`/
`_resolve_module_attr_call` ladder. `_JavaExtractor._extract_calls` never
has:

    for call in captures.get("call", []):
        name_node = call.child_by_field_name("name")
        obj = call.child_by_field_name("object")
        if name_node is None:
            continue
        if obj is None or obj.type == "this":
            self._resolve_self_call(self._text(name_node), call)

A bare call (no `object` field) or a `this.method()` call resolves within
the enclosing class only; anything qualified by another object
(`someVar.method()`, `ClassName.staticMethod()`, `importedAlias.method()`)
produces nothing. D33 investigated this while adding Java's `new
ClassName(...)` constructor capture and found the two are separable: the
constructor case is a self-contained grammar node with its own `type`
field, so it didn't need this gap fixed. It made an explicit call to leave
this gap alone rather than scope-creep, and named it as the next milestone.

## The scoping challenge

This is *not* a small grammar-capture slice like D33's four language
additions. General `obj.method()` resolution in Java needs the **declared
type of `obj`** — real type inference, which this deterministic,
tree-sitter-only, no-compiler codebase does not do and should not start
doing here (see D19/D30's precedent: precision over recall, no guessing).
Do not attempt full receiver-type inference.

What's plausibly resolvable *without* type inference, by direct analogy to
how other languages already do it:

1. **`ClassName.staticMethod()`** where the `object` field is a bare
   `identifier` whose text matches a class name — not a variable. This is
   structurally identical to what `_resolve_module_attr_call` already does
   for JS's `alias.func()`/Python's `pkg.func()`, except the "alias" is a
   Java class name instead of a module alias. Two sub-cases:
   - `ClassName` is defined in the same file → resolve directly via
     `self.classes_by_name`/`self.methods[(class_qname, method_name)]`
     (same pattern `_resolve_self_call` already uses, just keyed by a
     different class than the enclosing one).
   - `ClassName` was imported (fully-qualified or wildcard, D31) → this
     needs a target id built the same way `_resolve_java_import` builds
     import edges, queued through the *existing* `_PendingImportCall`
     batched-existence-check machinery (D32/D33) rather than a new one.
2. **Cross-file name-match**: once a Java call is queued as a `_PendingCall`
   (the fallback tier `_resolve_plain_call` already has), the existing
   `finish_run` batched pass resolves it exactly like every other language
   — no new mechanism needed there.
3. **A bare invocation that isn't actually `this`-implicit** — e.g. a call
   to a `static` method of the *enclosing* class from another static
   context. Investigate whether the current `_resolve_self_call`-for-bare-
   calls behavior already covers this correctly (Java's implicit-this/
   implicit-enclosing-class-static lookup collapse onto the same
   `self.methods`/`self.classes_by_name` lookup structure, or don't) before
   assuming it's already right or wrong.

What should explicitly stay out of scope, and why (document the decision,
don't silently skip):

- `someVariable.method()` where `someVariable` is a local variable,
  parameter, or field of unknown declared type — this needs type inference.
  Skip it; do not guess based on the variable's name or its assigned
  constructor call's class (tempting, but a false-precision trap: `Foo x =
  makeThing(); x.method();` has no local syntactic type).
- Method chains (`a.b().c()`), `super.method()`, lambda/method references
  (`Foo::method`), generics-qualified calls — all out of scope unless
  investigation shows one is a trivial, low-risk extension of the
  `ClassName.staticMethod()` case above; don't force it.

## Your task

1. Investigate the exact tree-sitter shape of a Java `method_invocation`'s
   `object` field for each case above (a real parse-tree probe, not
   assumption — same discipline as D32's Go exclusion and D33's PHP/Java
   field verification) to confirm `identifier` vs other node types
   (`field_access`, `this`, etc.) and that a bare `ClassName.staticMethod()`
   really does produce `object` as a plain `identifier` node no different
   in shape from a variable reference (i.e. tree-sitter's Java grammar does
   *not* syntactically distinguish a class-qualified call from a
   variable-qualified one — confirm this before designing around it).
2. Extend `_JavaExtractor._extract_calls` to route the `ClassName.
   staticMethod()` shape (same-file and import-qualified) through the
   existing resolution machinery, reusing `_resolve_module_attr_call`
   directly if the shape fits, or adding a small Java-specific dispatch
   that still calls into the shared `_PendingImportCall`/`classes_by_name`
   primitives rather than inventing new ones.
3. Do not weaken the existing `self`/`this`-qualified resolution
   (`_resolve_self_call`) or any other language's call resolution.
4. Add tests mirroring the existing per-language CALLS test shapes in
   tests/test_code_parser.py: same-file `ClassName.staticMethod()`
   resolved, import-qualified resolved, cross-file name-match inferred,
   ambiguous skipped, and — importantly — a regression test proving a
   variable-qualified call (`someVar.method()`) still produces nothing
   (precision preserved, no accidental type-guessing).
5. Confirm no D30 batching invariant regresses (batched `finish_run`/
   `_resolve_pending_import_calls` passes only, no per-call queries).

Constraints:
- No hosted API, model, embeddings, network, Docker, or external service.
- No type inference. Precision over recall: an unresolvable receiver does
  nothing, never guesses.
- Do not weaken any existing CALLS/INSTANTIATES/IMPORTS resolution, the
  orphan-edge sweep, the EnvVar sweep, or any D30 scale-hardening invariant.
- Do not push or publish without explicit user permission.
- When running the full pytest suite from a worktree, remember the primary
  repo's `.venv` is an editable install pinned to the primary repo's own
  path — run with `PYTHONPATH` pointed at the checkout under test (see
  AGENT_HANDOFF.md Known Pitfalls), or you will silently test stale code.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md (a new D-numbered entry) with what
was implemented (which Java call shapes now resolve, which stay
unresolved and why, any scope decisions made along the way) and rewrite
this file for the next highest-priority milestone. If no further small
carried-over gap is a clear next step after this one, use your judgment
against the PRD's product goals (§6.2) — the deliberate embeddings/
multi-repo non-goals are flagged in AGENT_HANDOFF.md as a candidate for a
dedicated planning pass rather than this loop's next cadence by default;
revisit that framing if it still seems right, or propose otherwise with
reasoning. If you conclude no further small well-scoped engineering gap
remains at all (this may plausibly be the last one), say so explicitly
with reasoning instead of inventing busywork, and recommend the human
planning checkpoint instead.
```

## Scoping notes

- This is the last named, carried-over engineering gap from the D-series
  call-resolution work (D16 → D19 → D31 → D32 → D33). Unlike D33's four
  language slices, it is bounded but genuinely open-ended in a way the
  prompt above tries to pre-scope: the investigation step (confirming the
  tree-sitter shape and that no type inference is needed for the
  `ClassName.staticMethod()` case) should happen *before* committing to an
  implementation approach.
- If the investigation in step 1 reveals that even the `ClassName.
  staticMethod()` case can't be reliably distinguished from a
  variable-qualified call without heuristics this codebase's
  precision-over-recall stance would reject, the right outcome is an
  explicit documented decision to leave Java's qualified-call resolution
  out of scope for good (not a forced, guessy implementation) — mirroring
  how D32 excluded Go's `T(x)` conversion syntax. That is an acceptable
  "no further small well-scoped gap remains" outcome for this specific
  item, not a failure.
- After this milestone (delivered or explicitly declined), the remaining
  backlog is the deliberately-flagged embeddings/multi-repo non-goals,
  which AGENT_HANDOFF.md already recommends routing to a dedicated human
  planning pass rather than this loop's automatic next cadence.

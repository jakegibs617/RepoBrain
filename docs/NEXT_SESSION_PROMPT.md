# Next Session Prompt

Copy-paste the prompt below to start the Go/Java internal import resolution
milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls) and
DECISIONS.md D15, D16, D19, and D30, then repobrain/parsers/code_treesitter.py
(_GoExtractor and _JavaExtractor, and _PythonExtractor._resolve_module for
the pattern first-class languages already use), repobrain/indexing/
indexer.py (the begin_run(known_paths) hook parsers use for cross-file
resolution), and the existing Go/Java coverage in tests/test_code_parser.py.

Your milestone: resolve Go and Java internal (same-module/same-project)
imports to real Module IMPORTS Module edges, the way Python/JS/TS/Ruby/PHP
already do (D15), instead of recording every Go/Java import as
`external_imports` metadata regardless of whether it points inside the
repository. This is the last open question from Milestone 3 (see
AGENT_HANDOFF.md "Open Questions").

Implement:
1. Go: locate and parse `go.mod` (module directive) if present at or above
   the indexed root to learn the module's import path prefix. Resolve an
   import whose path starts with that prefix to the scanned .go file(s)
   under the corresponding relative directory (Go packages are
   directories, not single files — an import resolves to a package, which
   may contain multiple files; decide and document how CodeParser's
   existing Module-per-file model represents "imports this package" when
   multiple files satisfy it, favoring precision over guessing a single
   file). Without a `go.mod` (or an import outside its module path),
   continue to record the import as external metadata exactly as today —
   do not guess a fake root.
2. Java: resolve imports against the scanned file set using the standard
   `groupId`-agnostic convention that a fully-qualified import
   `com.example.pkg.ClassName` maps to `.../com/example/pkg/ClassName.java`
   relative to a source root. Detect the source root deterministically
   (e.g. the first `src/main/java`-style conventional prefix found among
   scanned .java files, falling back to package-declaration-derived
   stripping — pick one precise, documented rule; do not heuristically
   guess between multiple candidate roots when the scanned tree is
   ambiguous, matching this codebase's existing precision-over-recall
   stance). Unresolvable imports (external libraries, ambiguous roots)
   remain external metadata, exactly as today.
3. Reuse the existing `begin_run(known_files)` / resolved-import-edge
   pattern (D15) rather than inventing a new mechanism; internal resolution
   must stay a metadata-driven, deterministic id computation like Python's
   `_resolve_module`, not a filesystem probe at parse time (parsers don't
   have filesystem access mid-parse by design — check how Python/JS do it
   before reaching for `os.path`).
4. Add fixtures and tests mirroring the existing Python/JS import-resolution
   tests: resolved same-module import becomes an edge; an import outside
   the module/source root stays external; an ambiguous Java multi-root case
   is documented as unresolved rather than guessed; incremental re-indexing
   converges when a target file is added, renamed, or removed (existing
   orphan-edge sweep must still clean up dangling targets, per
   "Known Pitfalls").
5. Update the language-support table in README.md and D15/D19 in
   DECISIONS.md to reflect the new Go/Java behavior precisely (what
   resolves, what still doesn't, and why).

Constraints:
- No hosted API, model, embeddings, network, Docker, or external service.
  No `go` or `java`/`javac` toolchain invocation — resolution is path/text
  based against `go.mod`/package declarations and the scanned file set only,
  the same way every other language in this codebase resolves imports.
- Precision over recall: an ambiguous or unresolvable import must remain
  external metadata, never a guessed or dangling edge.
- Do not weaken existing Python/JS/TS/PHP/Ruby import resolution, the
  orphan-edge sweep, or any scale-hardening invariant from D30 (e.g. don't
  reintroduce a per-file query in begin_run/finish_run when a batched
  approach already exists for the equivalent Python/JS case).
- Do not push or publish without explicit user permission.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md with what resolves and what
doesn't (evidence-based, with example resolved/unresolved cases), report
test results, and rewrite this file for the next highest-priority milestone
(re-evaluate the product-direction and engineering-follow-up backlog in
AGENT_HANDOFF.md — after this, no open items remain from the original list,
so use your judgment on what serves the PRD's product goals next: candidates
include the orphaned-EnvVar-node sweep open question, INSTANTIATES edges for
`ClassName()` calls, or revisiting the deliberate embeddings/multi-repo
non-goals now that the milestone loop has substantial delivery evidence
behind it).
```

## Scoping notes

- This closes the last open question carried since Milestone 3
  (AGENT_HANDOFF.md "Open Questions": "Go/Java internal import resolution
  (needs go.mod / package-root awareness)").
- Keep the same precision-first posture as every other D15 resolution rule:
  when in doubt, leave it as external metadata rather than emit a wrong or
  guessed edge.
- Ruby's `require_relative` resolution is a much simpler existing pattern
  (relative paths only, no module/root inference) — Go/Java need a real
  root-detection step first, which is the actual new work here.

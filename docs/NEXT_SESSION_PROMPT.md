# Next Session Prompt

Copy-paste the prompt below to start the distribution milestone.

```text
You are continuing work on RepoBrain, a local-first second brain for AI
coding agents. Development runs as a self-paced milestone loop: one milestone
per feat/ branch, merge only when the full pytest suite passes and all
confirmed /code-review findings are fixed.

Start by reading AGENT_HANDOFF.md (especially Known Pitfalls), README.md,
pyproject.toml, DECISIONS.md D21, D23, D25, and D26, plus
repobrain/agent_install.py, repobrain/cli.py, and repobrain/mcp_server.py.

Your milestone is distribution: make RepoBrain genuinely one-step to try and
one-step to install into an agent workflow, without weakening its local-first
or conservative file-ownership guarantees.

Implement:
1. uvx-ready packaging. Verify a clean environment can run `uvx repobrain`
   (or the correct project command) without a source checkout. Include all
   runtime package data and keep MCP dependencies available through a clear,
   tested install path.
2. One-step agent installation. Extend `repobrain install-agent` so one
   command can install the MCP configuration, marker-owned CLAUDE.md session
   context, and optional Git hooks together. Generate `.mcp.json` in the
   repository with a command that reliably launches RepoBrain for that exact
   root.
3. Conservative ownership and uninstall. Preserve unrelated JSON keys,
   servers, hooks, and human Markdown. Repeated install must converge;
   uninstall removes only RepoBrain-owned entries and files. Detect malformed
   or conflicting user configuration and fail safely instead of overwriting.
4. Cross-platform launch details. Avoid shell-dependent quoting; use argument
   arrays and resolved paths. Cover spaces in repository paths and document
   Windows limitations or support explicitly.
5. Documentation and smoke path. README should provide a minimal sequence from
   `uvx`/installation through `index`, `install-agent`, MCP verification, and
   uninstall. Add a clean-environment packaging smoke test where practical.
6. Tests. Cover wheel/sdist contents, console entry point, MCP config merge,
   idempotency, conflict behavior, paths with spaces, combined install options,
   exact uninstall, and existing hook/CLAUDE.md preservation.

Constraints:
- No hosted API or model requirement.
- Do not broaden ownership beyond marker-delimited Markdown, the exact
  RepoBrain MCP server entry, and RepoBrain-owned hook artifacts.
- Reuse agent_install.py and the existing CLI/MCP entry points; do not fork
  installation logic into command handlers.
- Keep current per-repository database scoping and freshness behavior.
- Do not publish a package or push remotely unless the user explicitly asks;
  prove distributability locally.

When done, run the full suite, run /code-review and fix confirmed findings,
update AGENT_HANDOFF.md and DECISIONS.md, and report commands, test results,
packaging artifacts checked, and known platform limitations. Rewrite this
file for the next highest-priority milestone.
```

## Scoping notes

- Distribution is the adoption milestone after M15 memory verification.
- Local-model semantic extraction remains deferred; installing RepoBrain must
  not introduce Ollama, hosted APIs, embeddings, or model downloads.
- Publishing to PyPI and pushing Git branches are external release actions and
  require explicit user authorization; local wheel/sdist and isolated `uvx`
  smoke tests are in scope.

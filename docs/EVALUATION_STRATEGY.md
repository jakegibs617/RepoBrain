# RepoBrain Evaluation Strategy

RepoBrain is useful only when its combined graph is trustworthy during real
repository work. Passing isolated parser tests is necessary, but it is not the
quality bar. Evaluation therefore happens at three levels.

## 1. Capability evaluations

Each subsystem gets focused fixtures with explicit expected facts and explicit
non-facts. Every evaluation checks both recall and precision.

| Capability | Positive examples | Adversarial examples | Primary measures |
|---|---|---|---|
| File scanning | tracked source, docs, tests | ignored, binary, oversized, unreadable | coverage, warnings |
| Code parsing | symbols, imports, calls, env reads | syntax errors, ambiguous calls, dynamic receivers | grounded edge precision |
| Markdown mapping | paths, unique symbols, relative links | external URLs, missing paths, ambiguous symbols | precision, provenance |
| Config tracing | env, Compose, Actions, Kubernetes | aliases, overrides, secrets, duplicate keys | definition-to-use recall |
| Data flow | route to handler to service to storage | wrappers, async jobs, indirect dispatch | path completeness, confidence |
| Impact analysis | callers, tests, docs, config | cycles, shared utilities, name collisions | ranked recall, noise |
| Memory | decisions, vocabulary, pitfalls, episodes | stale, conflicting, superseded notes | relevance, authority, freshness |

## 2. Lifecycle and adversarial evaluations

The graph must converge as a repository changes. Scenarios are sequences, not
single snapshots:

1. Index a known repository.
2. Observe a grounded answer.
3. Add, modify, rename, or delete one side of a relationship.
4. Re-index incrementally.
5. Verify the answer changes and stale facts disappear.
6. Re-index without changes and verify zero graph writes.

Important adversarial cases include:

- A unique symbol becomes ambiguous, then unique again.
- A documented target is renamed while its documentation remains unchanged.
- A config value is overridden at several deployment layers.
- A dotenv canary remains unreachable through CLI/MCP reads and raw SQLite
  bytes, including after upgrading a database that previously indexed it
  (`tests/test_secret_safety.py`).
- A route and a queue consumer share similarly named handlers.
- A source file is syntactically broken during an intermediate commit.
- A decision is superseded but remains present in repository history.
- An episodic memory conflicts with current source-derived evidence.
- A high-fan-out utility creates a large, cyclic impact graph.

`tests/test_system_scenarios.py` implements the first executable lifecycle
scenario for Milestones 1–4.

## 3. Whole-system journeys

Integrated evaluation asks whether an agent can complete a realistic task with
less rediscovery and fewer unsafe assumptions.

### Journey A: Safely change a database configuration

The agent must identify definitions, environment overrides, code readers,
deployment consumers, related docs, tests, project vocabulary, and known
pitfalls before proposing a change.

### Journey B: Modify a user-creation endpoint

The agent must trace endpoint → handler → service → repository → table, find
tests and documentation, estimate impact, and preserve relevant decisions.

### Journey C: Resume an interrupted refactor

The agent must distinguish durable project decisions from a previous session's
hypotheses, recover failed approaches, verify them against current source, and
produce an updated episode without duplicating stale memory.

### Journey D: Explain an unfamiliar polyglot repository

The agent must combine Python, JavaScript, Markdown, CI, container, and config
facts into a concise, source-grounded project explanation with uncertainty
made explicit.

## Scorecard

Every scenario records:

- **Correctness:** expected entities and relationships returned.
- **Precision:** unsupported relationships are absent.
- **Provenance:** every important claim points to a file and source span.
- **Confidence calibration:** inferred results rank below observed facts.
- **Convergence:** stale facts disappear after incremental changes.
- **Resilience:** malformed or unsupported inputs degrade to warnings.
- **Performance:** elapsed time and number of files reprocessed.
- **Context quality:** the final answer is concise enough for an agent to use.

No single aggregate score should hide a precision or provenance regression.
For release, all critical expectations must pass, known limitations must be
explicit, and no-change indexing must write zero graph facts.

## Evaluation artifacts

Each complex fixture should eventually contain:

```text
fixture/
  repository files...
  expectations.json     # entities, edges, absent facts, source spans
  mutations/            # ordered patches representing repository history
  tasks.md               # whole-system questions an agent must answer
```

Golden expectations should describe externally meaningful answers rather than
private implementation details. This allows parser and storage designs to
change without rewriting the product's quality definition.

## Roadmap integration

Every milestone adds three things:

1. Focused capability tests.
2. At least one adversarial mutation to an existing lifecycle scenario.
3. New assertions in a whole-system journey.

The integrated journeys become the release gate once configuration, data flow,
impact analysis, MCP, and layered memory are available together.

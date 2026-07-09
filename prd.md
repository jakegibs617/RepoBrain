# PRD: Local-First AI Agent Second Brain for Code, Docs, Config, and Data Flow

## 1. Product Name

Working name: **RepoBrain**

RepoBrain is a local-first “second brain” for AI coding agents. It indexes a software project and builds a durable, queryable understanding of the repository across source code, Markdown documentation, YAML/config files, runtime wiring, and data-flow relationships.

The goal is to help AI agents answer questions like:

* What is this project trying to accomplish?
* Which files implement each major feature?
* Where is this environment variable defined and consumed?
* What code path handles this API route?
* What data flows from this form, endpoint, job, queue, or config into the rest of the system?
* What files are likely impacted if this function, config value, schema, or workflow changes?
* What prior agent decisions, assumptions, and implementation notes are relevant now?

The product must work without requiring an external API key. API-based LLM enrichment may be added later as an optional enhancement, but the core system must remain useful offline.

---

## 2. Background and Motivation

Existing agent memory and codebase search tools tend to fall into separate categories:

1. **Code search tools**
   Good at symbol lookup, grep, embeddings, or repo maps, but weak at project purpose, docs, config, and runtime data-flow.

2. **Graph-based memory tools**
   Good at long-term facts or conversation memory, but not specialized for real software repositories.

3. **Graphify-like project graph tools**
   Useful for visualizing repository structure, but richer file understanding may require API keys or external model setup.

4. **Agent memory frameworks**
   Useful for storing user/session/task memory, but not enough to reason deeply over source files, Markdown, YAML, CI/CD, runtime wiring, and data dependencies.

RepoBrain fills the gap by creating a local-first, source-grounded, agent-accessible project memory graph.

---

## 3. Product Vision

RepoBrain should become the persistent architecture memory layer for AI software agents.

A coding agent using RepoBrain should be able to enter an unfamiliar repository and quickly understand:

* The project’s purpose.
* The major subsystems.
* How documentation maps to implementation.
* How config maps to runtime behavior.
* How code paths connect across files.
* How data moves through services, queues, APIs, databases, and scripts.
* What previous agent sessions discovered or changed.

RepoBrain should not be a black-box summarizer. It should expose evidence. Every important answer should be traceable back to specific files and source spans.

---

## 4. Target Users

### Primary User

A developer using long-running AI coding agents to build, refactor, or maintain software projects.

### Secondary Users

* Staff engineers reviewing architecture.
* Product-minded engineers trying to connect product goals to implementation.
* AI agents that need durable context between sessions.
* Teams using Markdown handoff docs, ADRs, RFCs, and implementation notes.

---

## 5. Core Principles

### 5.1 Local-first

The system must work without OpenAI, Anthropic, Gemini, or other hosted model API keys.

External models may be optional, but they must never be required for the core index, graph, and retrieval features.

### 5.2 Deterministic facts before LLM interpretation

RepoBrain should extract structural facts using deterministic parsers where possible:

* Tree-sitter for source code.
* Language Server Protocol where useful.
* Markdown AST parsing for docs.
* YAML/JSON/TOML parsers for config.
* Schema-specific adapters for common config files.

LLMs may enrich summaries, but they should not be the only source of truth.

### 5.3 Source-grounded answers

Every graph node and edge should store provenance:

* File path.
* Source span.
* Extractor name.
* Confidence score.
* Commit hash or index version.
* Last seen timestamp.

### 5.4 Agent-first interface

The primary interface should be MCP tools that an AI agent can call during coding work.

A CLI should also exist for humans and for debugging.

### 5.5 Human-readable memory

Agent-authored memory should be stored in Markdown as well as structured graph form.

The system should support durable handoff documents such as:

* `PROJECT_MEMORY.md`
* `AGENT_HANDOFF.md`
* `ARCHITECTURE_NOTES.md`
* `DECISIONS.md`

### 5.6 Incremental indexing

The system should not re-index an entire repository on every run if only a few files changed.

It should hash files and update only changed nodes and edges.

---

## 6. Goals

### 6.1 Functional Goals

RepoBrain must:

1. Index a local repository.
2. Parse source code into symbols and relationships.
3. Parse Markdown files into structured documentation nodes.
4. Parse YAML, JSON, TOML, Dockerfiles, package files, and CI/CD files.
5. Build a graph connecting code, docs, config, tests, scripts, routes, and runtime artifacts.
6. Expose query tools through MCP.
7. Store graph state locally.
8. Support incremental re-indexing.
9. Produce source-grounded answers.
10. Let agents write durable session memory.
11. Support impact analysis for changed files, symbols, config keys, and docs.
12. Support data-flow tracing across common project patterns.

### 6.2 Product Goals

RepoBrain should help an agent:

* Spend fewer tokens rediscovering repository structure.
* Avoid repeatedly asking the user where things are.
* Make safer changes by understanding blast radius.
* Connect implementation files to product intent.
* Preserve useful discoveries across sessions.

---

## 7. Non-goals

The first version should not attempt to:

1. Replace a full IDE.
2. Replace a vector database SaaS product.
3. Support every programming language perfectly.
4. Build a perfect whole-program static analyzer.
5. Require cloud infrastructure.
6. Require hosted LLM APIs.
7. Automatically prove code correctness.
8. Automatically refactor code.
9. Support enterprise multi-user permissions.
10. Build a polished hosted web app.

---

## 8. MVP Scope

The MVP should focus on one local repository at a time.

### Required MVP Inputs

* Source code files.
* Markdown files.
* YAML files.
* JSON files.
* TOML files.
* Dockerfiles.
* Shell scripts.
* Package/config files.
* Git metadata when available.

### Required MVP Outputs

* Local graph database.
* Source span index.
* CLI query interface.
* MCP server exposing core tools.
* Markdown project memory file.
* Optional static HTML graph/debug report.

---

## 9. Suggested Initial Tech Stack

The agent may adjust if justified, but default to this stack:

### Language

**Python** for MVP.

Reasoning:

* Fast iteration.
* Strong parsing ecosystem.
* Good MCP support.
* Easy local SQLite integration.
* Easy file walking, hashing, and CLI development.

### Storage

Start with:

* SQLite
* FTS5
* sqlite-vec or local embedding extension if available
* JSON columns where useful

Avoid requiring Neo4j or external graph DB for MVP.

### Parsing

Use:

* `tree-sitter` for code parsing.
* `markdown-it-py` or equivalent for Markdown AST.
* `ruamel.yaml` or `PyYAML` for YAML.
* Python standard `json`.
* `tomllib` for TOML on modern Python.
* Custom parsers/adapters for Dockerfile, GitHub Actions, Docker Compose, Kubernetes, package files.

### CLI

Use:

* `typer` or `click`.

### MCP

Use an MCP Python SDK or a lightweight MCP server implementation.

### Testing

Use:

* `pytest`.
* Fixture repositories for integration tests.

---

## 10. Repository Structure

Create a new repository with this approximate structure:

```text
repobrain/
  README.md
  pyproject.toml
  repobrain/
    __init__.py

    cli.py
    config.py

    indexing/
      __init__.py
      scanner.py
      hasher.py
      indexer.py
      incremental.py

    parsers/
      __init__.py
      base.py
      code_treesitter.py
      markdown_parser.py
      yaml_parser.py
      json_parser.py
      toml_parser.py
      dockerfile_parser.py
      shell_parser.py
      package_parser.py

    adapters/
      __init__.py
      github_actions.py
      docker_compose.py
      kubernetes.py
      npm_package.py
      python_package.py
      env_files.py

    graph/
      __init__.py
      schema.py
      store.py
      queries.py
      provenance.py

    retrieval/
      __init__.py
      keyword.py
      semantic.py
      ranking.py
      context_builder.py

    analysis/
      __init__.py
      impact.py
      data_flow.py
      purpose_map.py
      docs_to_code.py
      config_flow.py
      test_coverage_map.py

    memory/
      __init__.py
      project_memory.py
      session_memory.py
      decisions.py

    mcp/
      __init__.py
      server.py
      tools.py

    reports/
      __init__.py
      graph_report.py
      html_report.py

  tests/
    fixtures/
      small_python_app/
      node_api_app/
      docker_compose_app/
      github_actions_app/
      markdown_docs_app/

    test_indexing.py
    test_markdown_parser.py
    test_yaml_parser.py
    test_graph_store.py
    test_mcp_tools.py
    test_data_flow.py
    test_impact.py
```

---

## 11. Core Data Model

### 11.1 Node Types

RepoBrain should support the following graph node types.

```text
Repository
Directory
File
Module
Package
Class
Function
Method
Variable
Type
Interface
Route
Endpoint
CLICommand
Script
Worker
Queue
Event
Database
Table
Migration
Schema
ConfigFile
ConfigKey
EnvVar
SecretRef
DockerImage
DockerService
KubernetesResource
GitHubWorkflow
GitHubJob
GitHubStep
TestFile
TestCase
MarkdownDocument
MarkdownSection
ADR
Decision
Concept
Task
AgentNote
Assumption
OpenQuestion
```

The MVP does not need perfect extraction for every type, but the schema should anticipate these entities.

### 11.2 Edge Types

RepoBrain should support the following relationship types.

```text
CONTAINS
DEFINES
IMPORTS
EXPORTS
CALLS
REFERENCES
IMPLEMENTS
EXTENDS
INSTANTIATES
READS
WRITES
READS_ENV
SETS_ENV
USES_CONFIG
DECLARES_CONFIG
HANDLES_ROUTE
EXPOSES_ENDPOINT
RUNS_COMMAND
INVOKES_SCRIPT
BUILDS_IMAGE
USES_IMAGE
DEPLOYS
STARTS_SERVICE
PUBLISHES_EVENT
CONSUMES_EVENT
READS_TABLE
WRITES_TABLE
MIGRATES_TABLE
DOCUMENTS
RATIONALE_FOR
MENTIONS
TESTS
COVERS
DEPENDS_ON
MAY_IMPACT
GENERATED_FROM
OBSERVED_IN
AUTHORED_BY_AGENT
```

### 11.3 Required Node Fields

Every node should store:

```text
id
type
name
qualified_name
path
start_line
end_line
language
hash
metadata_json
created_at
updated_at
last_seen_at
confidence
extractor
commit_hash
```

### 11.4 Required Edge Fields

Every edge should store:

```text
id
source_node_id
target_node_id
type
path
start_line
end_line
metadata_json
confidence
extractor
created_at
updated_at
last_seen_at
commit_hash
```

### 11.5 Provenance

Every non-trivial node and edge must be traceable to a file and source span when possible.

When an edge is inferred rather than directly observed, mark:

```text
is_inferred = true
confidence < 1.0
inference_reason = "..."
```

---

## 12. File Type Support

### 12.1 Source Code

MVP should support at least:

* Python
* JavaScript
* TypeScript
* PHP
* Ruby
* Go
* Java
* Bash

Deeper support can come later.

### 12.2 Markdown

Parse:

* Headings
* Sections
* Lists
* Code blocks
* Links
* Tables if possible
* TODOs
* ADR-like patterns
* References to files, symbols, routes, config keys, and commands

Markdown should produce `MarkdownDocument`, `MarkdownSection`, `Concept`, `Decision`, `Task`, and `OpenQuestion` nodes.

### 12.3 YAML

YAML support is critical.

The system should parse generic YAML and also support adapters for:

* GitHub Actions
* Docker Compose
* Kubernetes manifests
* Helm values
* Kustomize files
* CI/CD config files
* Application config files

The system should extract:

* Config files
* Config keys
* Env vars
* Secret references
* Services
* Jobs
* Steps
* Commands
* Image names
* Ports
* Volumes
* Dependencies

### 12.4 JSON/TOML

Parse:

* `package.json`
* `tsconfig.json`
* `composer.json`
* `pyproject.toml`
* `Cargo.toml`
* `appsettings.json`
* config files
* lock files as lower-priority metadata

### 12.5 Dockerfile

Extract:

* Base images
* Build stages
* Commands
* Exposed ports
* Environment variables
* Entrypoints
* Copy relationships

### 12.6 Shell Scripts

Extract:

* Commands
* Script dependencies
* Env var reads/writes
* Invoked files
* Service commands

---

## 13. Required Queries

RepoBrain should support these high-level questions through CLI and MCP.

### 13.1 Project Understanding

```text
What is this project?
What are the main subsystems?
What files explain the project purpose?
Which docs map to which code areas?
What are the main entrypoints?
```

### 13.2 Code Navigation

```text
Find symbol by name.
Find references to symbol.
Find callers of function.
Find callees of function.
Find files related to feature keyword.
Find tests related to file or function.
```

### 13.3 Config Understanding

```text
Where is this env var set?
Where is this env var read?
Which services use this config key?
Which workflow runs this script?
Which Docker image is used by this service?
Which Kubernetes resource deploys this component?
```

### 13.4 Data Flow

```text
Trace route to handler to service to database.
Trace config value from YAML to runtime code usage.
Trace queue publisher to consumer.
Trace GitHub Action step to script to deploy target.
Trace data model from migration to code to API.
```

### 13.5 Impact Analysis

```text
What might break if I change this file?
What might break if I change this function?
What might break if I rename this env var?
What tests should run after changing this module?
What docs should be updated after this code change?
```

### 13.6 Agent Memory

```text
What did previous agents learn?
What decisions were made?
What assumptions are currently active?
What open questions remain?
What should the next agent work on?
```

---

## 14. MCP Tool Specification

Implement the following MCP tools.

### 14.1 `index_repo`

Indexes or re-indexes the current repository.

Input:

```json
{
  "path": ".",
  "incremental": true,
  "include_patterns": [],
  "exclude_patterns": []
}
```

Output:

```json
{
  "status": "ok",
  "files_scanned": 123,
  "files_changed": 8,
  "nodes_created": 340,
  "edges_created": 901,
  "warnings": []
}
```

### 14.2 `search_project`

Searches across graph nodes, docs, symbols, and source spans.

Input:

```json
{
  "query": "authentication middleware",
  "limit": 10,
  "types": []
}
```

Output should include ranked results with file path, lines, node type, and reason.

### 14.3 `explain_project`

Returns a project overview grounded in repo files.

Input:

```json
{
  "focus": "overall"
}
```

Possible focus values:

```text
overall
architecture
runtime
testing
deployment
docs
```

### 14.4 `explain_file`

Explains a specific file and its graph relationships.

Input:

```json
{
  "path": "src/api/users.ts"
}
```

Output:

* File summary.
* Symbols defined.
* Imports.
* Exports.
* Called dependencies.
* Referencing docs.
* Related tests.
* Related config.

### 14.5 `find_symbol`

Finds code symbols.

Input:

```json
{
  "name": "createUser",
  "exact": false,
  "limit": 20
}
```

### 14.6 `trace_symbol`

Shows callers, callees, imports, references, and related tests.

Input:

```json
{
  "symbol": "createUser",
  "depth": 2
}
```

### 14.7 `trace_config`

Traces config values from definition to usage.

Input:

```json
{
  "key": "DATABASE_URL",
  "depth": 3
}
```

### 14.8 `trace_data_flow`

Traces a route, event, table, queue, file, or symbol through connected graph edges.

Input:

```json
{
  "start": "POST /api/users",
  "depth": 4,
  "direction": "both"
}
```

### 14.9 `impact_analysis`

Estimates blast radius for a change.

Input:

```json
{
  "target": "src/auth/session.ts",
  "change_type": "modify"
}
```

Output:

* Impacted files.
* Impacted symbols.
* Impacted config.
* Recommended tests.
* Docs likely needing updates.
* Confidence and evidence.

### 14.10 `docs_for_code`

Finds docs that explain a code file, symbol, service, or feature.

Input:

```json
{
  "target": "src/payments"
}
```

### 14.11 `code_for_docs`

Finds code related to a Markdown section, ADR, or product concept.

Input:

```json
{
  "doc_path": "docs/auth.md",
  "heading": "Session lifecycle"
}
```

### 14.12 `write_agent_memory`

Writes durable memory after an agent session.

Input:

```json
{
  "summary": "Implemented user session refresh flow.",
  "decisions": [
    "Session refresh remains server-side only."
  ],
  "assumptions": [
    "Redis is available in production."
  ],
  "open_questions": [
    "Need to confirm token expiry policy."
  ],
  "changed_files": [
    "src/auth/session.ts"
  ],
  "next_steps": [
    "Add integration test for expired refresh token."
  ]
}
```

This should update both structured memory and a Markdown handoff file.

### 14.13 `read_agent_memory`

Reads durable project memory.

Input:

```json
{
  "topic": "auth",
  "limit": 10
}
```

---

## 15. CLI Specification

Provide a command-line interface.

Example commands:

```bash
repobrain init
repobrain index .
repobrain status
repobrain search "auth middleware"
repobrain explain project
repobrain explain file src/auth/session.ts
repobrain trace config DATABASE_URL
repobrain trace data-flow "POST /api/users"
repobrain impact src/auth/session.ts
repobrain memory read
repobrain memory write --from-file handoff.md
repobrain report
repobrain mcp
```

---

## 16. Indexing Behavior

### 16.1 Initial Index

On first run:

1. Discover files.
2. Apply ignore rules.
3. Hash files.
4. Parse each supported file.
5. Create file nodes.
6. Create syntax/semantic nodes.
7. Create source-grounded edges.
8. Store graph.
9. Build keyword index.
10. Optionally build local embeddings.
11. Generate summary report.

### 16.2 Incremental Index

On later runs:

1. Detect changed, added, and deleted files.
2. Remove stale nodes/edges for changed/deleted files.
3. Re-parse only changed files.
4. Reconcile cross-file relationships.
5. Mark missing nodes as stale or deleted.
6. Update index version.

---

## 17. Ignore Rules

Respect:

* `.gitignore`
* `.repobrainignore`
* Common generated/vendor directories

Default excludes:

```text
.git/
node_modules/
vendor/
dist/
build/
coverage/
.cache/
.next/
.nuxt/
venv/
.venv/
__pycache__/
target/
.DS_Store
*.lock
```

Lock files may be indexed later as metadata, but exclude them from MVP unless needed.

---

## 18. Markdown Memory Files

RepoBrain should create or update:

```text
.repobrain/
  repobrain.sqlite
  graph_report.md
  agent_memory.md
  decisions.md
  open_questions.md
```

The main human-facing memory file should be:

```text
AGENT_HANDOFF.md
```

Suggested format:

```markdown
# Agent Handoff

## Project Summary

## Current Architecture Understanding

## Important Files

## Recent Changes

## Decisions

## Assumptions

## Open Questions

## Known Pitfalls

## Suggested Next Steps

## Source-Grounded Notes
```

The agent must not overwrite human-authored sections without preserving prior content.

---

## 19. Ranking and Retrieval

Search should combine:

1. Exact symbol match.
2. File path match.
3. Full-text search.
4. Graph neighborhood relevance.
5. Optional embedding similarity.
6. Recency from git/index metadata.
7. Node type priority.

For coding agents, source-grounded exact matches should usually outrank vague semantic matches.

---

## 20. Purpose Mapping

RepoBrain should attempt to connect docs to code.

Example:

A Markdown section says:

```markdown
## Session Refresh

Refresh tokens are rotated on every request to `/api/session/refresh`.
```

RepoBrain should connect:

* Markdown section: `Session Refresh`
* Route: `/api/session/refresh`
* Handler function
* Auth service
* Token model
* Relevant tests
* Config keys such as `SESSION_TTL`

MVP can implement this with heuristics:

* File path references.
* Backtick references.
* Route-like strings.
* Symbol-like strings.
* Heading keywords.
* Link targets.
* README/package names.
* Code block commands.

---

## 21. Config Flow

This is a core differentiator.

RepoBrain should trace:

```text
YAML config key
→ environment variable
→ container/service definition
→ runtime command
→ source code read
→ downstream dependency
```

Example query:

```text
Where does DATABASE_URL come from and what uses it?
```

Expected answer:

```text
DATABASE_URL is set in docker-compose.yml under service api.environment.
It is also referenced in .github/workflows/deploy.yml as a secret.
The application reads it in src/db/client.ts.
The DB client is imported by src/users/repository.ts and src/orders/repository.ts.
Changing it may affect API startup and all DB-backed routes.
```

Every statement should include file paths and line ranges.

---

## 22. Data Flow

RepoBrain should support best-effort data-flow analysis.

MVP data-flow should handle:

* API route to handler.
* Handler to service.
* Service to repository.
* Repository to database table.
* Function to function calls.
* Queue publisher to consumer by event/topic name.
* Config definition to code read.
* CI workflow to script to deploy artifact.

Do not attempt full language-level taint analysis in MVP.

---

## 23. Impact Analysis

Impact analysis should combine:

* Imports.
* Call graph.
* Config references.
* Tests.
* Docs.
* Data-flow edges.
* File path conventions.
* Git history later if available.

Output should clearly separate:

```text
High-confidence impact
Medium-confidence impact
Low-confidence possible impact
Recommended tests
Docs likely needing updates
Unknowns
```

---

## 24. Report Generation

Implement:

```bash
repobrain report
```

The report should produce:

```text
.repobrain/graph_report.md
.repobrain/graph_report.html
```

The Markdown report should include:

* Project summary.
* File counts by language/type.
* Top entrypoints.
* Main subsystems.
* Detected config files.
* Detected workflows.
* Detected services.
* Detected routes.
* Detected env vars.
* Graph stats.
* Indexing warnings.
* Unsupported files.
* Suggested next indexing improvements.

HTML can be basic in MVP.

---

## 25. Acceptance Criteria

### 25.1 MVP Acceptance Criteria

The MVP is complete when:

1. `repobrain index .` successfully indexes a fixture repository.
2. The database stores file, symbol, Markdown, and config nodes.
3. The graph stores source-grounded edges.
4. `repobrain search` returns useful ranked results.
5. `repobrain explain project` provides a grounded overview.
6. `repobrain explain file` explains a file’s role and relationships.
7. `repobrain trace config DATABASE_URL` shows config definition and code usage in a fixture repo.
8. `repobrain trace data-flow "POST /api/users"` shows route to handler to service to data layer in a fixture repo.
9. `repobrain impact <file>` returns impacted symbols, files, docs, and tests.
10. MCP server exposes equivalent tools.
11. Agent memory can be written and read.
12. No external API key is required.
13. Tests pass.
14. README explains setup, usage, and limitations.

### 25.2 Quality Acceptance Criteria

* All graph facts must include provenance when available.
* The tool must fail gracefully on unsupported languages.
* Incremental indexing must avoid reprocessing unchanged files.
* The system must be usable on a repo with at least 1,000 files.
* The CLI must produce readable output.
* MCP outputs must be concise enough for agent consumption.

---

## 26. Milestone Plan

### Milestone 1: Project Skeleton and Storage

Deliver:

* Python package setup.
* CLI skeleton.
* SQLite schema.
* File scanner.
* Hashing and incremental metadata.
* Basic tests.

Acceptance:

```bash
repobrain init
repobrain index tests/fixtures/small_python_app
repobrain status
```

works.

---

### Milestone 2: Basic File and Text Index

Deliver:

* File nodes.
* Directory nodes.
* Full-text indexing.
* Markdown parser.
* Basic search.

Acceptance:

```bash
repobrain search "database"
```

returns file paths and snippets.

---

### Milestone 3: Code Symbol Parser

Deliver:

* Tree-sitter integration.
* Function/class/module extraction.
* Import extraction.
* Basic `DEFINES`, `IMPORTS`, and `CONTAINS` edges.
* Symbol search.

Acceptance:

```bash
repobrain find-symbol create_user
repobrain explain file src/users/service.py
```

works on fixture repos.

---

### Milestone 4: Markdown Purpose Mapping

Deliver:

* Markdown section nodes.
* Heading hierarchy.
* Links and code references.
* Basic doc-to-code matching.
* `docs_for_code` and `code_for_docs`.

Acceptance:

A README or docs page referencing a route, file, or symbol gets linked to the related code.

---

### Milestone 5: YAML and Config Understanding

Deliver:

* Generic YAML parser.
* Env var extraction.
* GitHub Actions adapter.
* Docker Compose adapter.
* Kubernetes adapter.
* `trace_config`.

Acceptance:

```bash
repobrain trace config DATABASE_URL
```

returns definitions and code usages in fixture repos.

---

### Milestone 6: Data Flow V1

Deliver:

* Route detection heuristics.
* Handler/service/repository path convention heuristics.
* Function call edges where supported.
* Queue/event name matching.
* `trace_data_flow`.

Acceptance:

A fixture route can be traced from endpoint to handler to service to data layer.

---

### Milestone 7: Impact Analysis

Deliver:

* Dependency traversal.
* Config blast radius.
* Test mapping.
* Docs mapping.
* `impact_analysis`.

Acceptance:

Changing a source file returns likely affected files, tests, docs, and config relationships.

---

### Milestone 8: MCP Server

Deliver:

* MCP server.
* All core tools.
* JSON-safe structured outputs.
* README instructions for connecting to an agent.

Acceptance:

An MCP-compatible coding agent can call RepoBrain tools against a local repo.

---

### Milestone 9: Agent Memory

Deliver:

* `write_agent_memory`.
* `read_agent_memory`.
* Markdown handoff update.
* Structured memory nodes and edges.

Acceptance:

Agent can persist session summary, decisions, assumptions, open questions, and next steps.

---

### Milestone 10: Report and Polish

Deliver:

* Markdown report.
* Basic HTML graph/debug report.
* Better error messages.
* Performance improvements.
* Documentation.

Acceptance:

```bash
repobrain report
```

generates useful human-readable project documentation.

---

## 27. Fixture Repositories

Create test fixture repos.

### 27.1 `small_python_app`

Should include:

* API route.
* Handler.
* Service.
* Repository.
* DB config.
* Tests.
* README.

### 27.2 `node_api_app`

Should include:

* Express or Fastify route.
* Service layer.
* Config file.
* `.env.example`.
* Tests.
* Package scripts.

### 27.3 `docker_compose_app`

Should include:

* Docker Compose services.
* Env vars.
* Dockerfile.
* App code reading env vars.

### 27.4 `github_actions_app`

Should include:

* Workflow.
* Jobs.
* Steps.
* Build script.
* Test script.
* Deploy script.

### 27.5 `markdown_docs_app`

Should include:

* README.
* ADR.
* Implementation plan.
* Docs referencing files, routes, config, and symbols.

---

## 28. Example User Stories

### Story 1: Understand Project

As an AI coding agent, I want to understand the project purpose so that I can make changes aligned with the app’s goals.

Acceptance:

* `explain_project` returns a grounded project summary.
* It references README, package files, key routes, and major modules.

### Story 2: Trace Config

As an AI coding agent, I want to trace an environment variable so that I can safely modify deployment settings.

Acceptance:

* `trace_config("DATABASE_URL")` finds YAML definitions, `.env.example`, Docker Compose usage, and code reads.

### Story 3: Find Data Flow

As an AI coding agent, I want to trace an API endpoint through the system so that I understand what files implement it.

Acceptance:

* `trace_data_flow("POST /api/users")` returns route, handler, service, repository, DB table, and tests if available.

### Story 4: Impact Analysis

As an AI coding agent, I want to know what might break if I change a file so that I can run the right tests and update docs.

Acceptance:

* `impact_analysis("src/users/service.py")` returns callers, tests, docs, config, and dependent modules.

### Story 5: Preserve Agent Knowledge

As an AI coding agent, I want to write durable handoff memory so that the next agent session does not start from scratch.

Acceptance:

* `write_agent_memory` updates structured graph memory and Markdown handoff files.

---

## 29. Risks and Mitigations

### Risk: Static analysis is imperfect

Mitigation:

* Track confidence.
* Separate observed facts from inferred relationships.
* Always expose provenance.

### Risk: Too many false-positive edges

Mitigation:

* Rank results.
* Use confidence thresholds.
* Clearly label weak matches.
* Provide debug reports.

### Risk: Too much scope

Mitigation:

* Start with fixtures.
* Prioritize Python, JS/TS, Markdown, YAML.
* Add adapters incrementally.

### Risk: Performance issues on large repos

Mitigation:

* Incremental indexing.
* File hashing.
* Exclude generated/vendor folders.
* Batch database writes.
* Add profiling tests.

### Risk: Agent overwrites memory poorly

Mitigation:

* Append first.
* Preserve human-authored sections.
* Store structured entries separately.
* Add timestamps and source sessions.

---

## 30. Open Design Questions

The implementation agent should make a reasonable first decision and document it in `DECISIONS.md`.

Open questions:

1. Should the graph store use only SQLite tables, or should it add a graph library abstraction?
2. Should embeddings be in MVP or postponed?
3. Which languages should receive first-class Tree-sitter support first?
4. Should the MCP server run per repo or as a daemon managing multiple repos?
5. Should `.repobrain/` be committed to git or treated as local cache?
6. How should agent memory distinguish facts, assumptions, and interpretations?
7. Should reports include Mermaid diagrams in Markdown?
8. How aggressive should Markdown-to-code matching be?

Recommended defaults:

1. Use SQLite only for MVP.
2. Postpone embeddings until deterministic search works.
3. Prioritize Python, JavaScript, TypeScript, PHP, Bash.
4. Run MCP server per repo for MVP.
5. Treat `.repobrain/repobrain.sqlite` as local cache; allow Markdown memory to be committed.
6. Store memory entries with explicit type and confidence.
7. Use Mermaid if easy.
8. Prefer precision over recall at first.

---

## 31. Implementation Instructions for the Long-Running Agent

The implementation agent should work in small, verifiable milestones.

For each milestone:

1. Read this PRD.
2. Check existing files.
3. Implement the smallest useful slice.
4. Add or update tests.
5. Run the test suite.
6. Update `AGENT_HANDOFF.md`.
7. Update `DECISIONS.md` if a design choice was made.
8. Summarize what changed and what remains.

The agent should not skip tests.

The agent should not introduce external hosted API requirements.

The agent should favor boring, inspectable, local infrastructure.

---

## 32. First Agent Prompt

Use the following prompt to start the first coding session:

```text
You are building RepoBrain, a local-first second brain for AI coding agents.

Read the PRD in full. Your first milestone is to create the project skeleton, local SQLite storage, file scanner, hashing system, initial CLI, and basic tests.

Do not add external hosted API requirements.

Implement:
1. Python package structure.
2. pyproject.toml.
3. CLI commands:
   - repobrain init
   - repobrain index <path>
   - repobrain status
4. SQLite schema for repositories, files, nodes, edges, and index runs.
5. File scanner with default ignore rules.
6. File hashing.
7. Incremental detection of unchanged files.
8. Basic tests with a small fixture repo.
9. AGENT_HANDOFF.md with what was implemented and next steps.
10. DECISIONS.md documenting any design decisions.

After implementation, run tests and report:
- What changed.
- How to run it.
- What tests pass.
- Known limitations.
- Recommended next milestone.
```

---

## 33. Definition of Done for Initial Version

RepoBrain v0.1 is done when a developer can run:

```bash
repobrain index .
repobrain explain project
repobrain trace config DATABASE_URL
repobrain trace data-flow "POST /api/users"
repobrain impact src/users/service.py
repobrain mcp
```

and receive useful, source-grounded answers from a local repository without configuring an external API key.

The result does not need to be perfect. It needs to be durable, inspectable, extensible, and useful to an AI coding agent.

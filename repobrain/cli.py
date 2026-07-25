"""RepoBrain command-line interface."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import click

from .config import CONFIG_FILENAME, REPOBRAIN_DIR, RepoBrainConfig
from .agent_install import install_agent as run_install_agent
from .agent_install import uninstall_agent as run_uninstall_agent
from .briefing import DEFAULT_BUDGET, MINIMUM_BUDGET, project_brief
from .change_context import GitDiffError, change_context as run_change_context
from .diagnostics import configure_logging, log_event
from .graph.queries import explain_file as run_explain_file
from .graph.queries import CHANGE_TYPES
from .graph.queries import code_for_docs as run_code_for_docs
from .graph.queries import docs_for_code as run_docs_for_code
from .graph.queries import find_symbol as run_find_symbol
from .graph.queries import impact_analysis as run_impact_analysis
from .graph.queries import trace_data_flow as run_trace_data_flow
from .graph.queries import trace_config as run_trace_config
from .graph.store import GraphStore
from .freshness import FreshnessBlockedError, require_fresh
from .history import (
    co_change_report,
    churn_report,
    ownership_report,
    refresh_history,
)
from .indexing.indexer import Indexer, RepoRootMismatchError
from .memory import read_agent_memory, verify_agent_memory, write_agent_memory
from .retrieval.keyword import search as keyword_search
from .reporting import generate_report, project_overview


def _resolve_root(path: str) -> Path:
    return Path(path).resolve()


@contextmanager
def _open_store(root: Path, *, auto_index: bool = True, gate: bool = True):
    """Open a query-safe store, repairing small stale diffs before yielding."""
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.exists():
        raise click.ClickException(
            f"No RepoBrain database at {db_path}. Run `repobrain index {root}` first."
        )
    store = GraphStore(db_path)
    store.last_freshness = None
    try:
        if gate:
            freshness = require_fresh(root, store, auto_index=auto_index)
            store.last_freshness = freshness
            log_event(
                "freshness.checked",
                status=freshness["status"],
                can_query=freshness["can_query"],
                auto_index=auto_index,
            )
            if (freshness["status"] == "reindexed"
                    and not click.get_current_context().params.get("as_json", False)):
                click.echo(f"Freshness: {freshness['message']}", err=True)
        yield store
    except FreshnessBlockedError as exc:
        log_event(
            "freshness.blocked",
            status=exc.result["status"],
            can_query=False,
            auto_index=auto_index,
        )
        raise click.ClickException(exc.result["message"]) from exc
    finally:
        store.close()


def _freshness_option(function):
    return click.option(
        "--no-auto-index", is_flag=True,
        help="Check freshness but never mutate; stale queries are refused.",
    )(function)


@click.group()
@click.option("--verbose", is_flag=True, help="Emit structured operational logs to stderr.")
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Structured stderr log level (or set REPOBRAIN_LOG_LEVEL).",
)
@click.pass_context
def main(ctx: click.Context, verbose: bool, log_level: str | None) -> None:
    """RepoBrain: a local-first second brain for AI coding agents."""
    configure_logging(log_level or ("INFO" if verbose else None))
    log_event("cli.command.start", command=ctx.invoked_subcommand or "unknown")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False), default=".")
def init(path: str) -> None:
    """Create .repobrain/ with a default config inside PATH (default: cwd)."""
    root = _resolve_root(path)
    rb_dir = root / REPOBRAIN_DIR
    rb_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = rb_dir / CONFIG_FILENAME
    if cfg_path.exists():
        click.echo(f"Already initialized: {cfg_path}")
        return
    config = RepoBrainConfig()
    config.save(root)
    GraphStore(root / config.db_path).close()
    click.echo(f"Initialized RepoBrain in {rb_dir}")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--no-incremental", is_flag=True, help="Re-parse every file, ignoring stored hashes.")
@click.option("--no-history", is_flag=True, help="Skip the Git history extraction phase.")
def index(path: str, no_incremental: bool, no_history: bool) -> None:
    """Index PATH (default: cwd) into PATH's own .repobrain/ database.

    The database is pinned to the indexed root; indexing a different root
    always uses (or creates) that root's own database instead of purging
    this one. After files are indexed, an explicit history phase extracts
    the recent Git commit window (skip with --no-history).
    """
    root = _resolve_root(path)
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    with GraphStore(db_path) as store:
        indexer = Indexer(store, config=config)
        try:
            stats = indexer.index(root, incremental=not no_incremental)
        except RepoRootMismatchError as exc:
            raise click.ClickException(str(exc)) from exc
        history = None if no_history else refresh_history(root, store, config=config)
    log_event(
        "index.completed",
        status="ok",
        incremental=not no_incremental,
        files_scanned=stats.files_scanned,
        files_changed=stats.files_changed,
        files_deleted=stats.files_deleted,
        nodes_created=stats.nodes_created,
        edges_created=stats.edges_created,
    )
    click.echo(f"Indexed {path}")
    click.echo(f"  files scanned : {stats.files_scanned}")
    click.echo(f"  files changed : {stats.files_changed}")
    click.echo(f"  files deleted : {stats.files_deleted}")
    click.echo(f"  nodes written : {stats.nodes_created}")
    click.echo(f"  edges written : {stats.edges_created}")
    if history is not None:
        click.echo(f"  history       : {_history_summary(history)}")
    for warning in stats.warnings:
        click.echo(f"  warning: {warning}")


def _history_summary(history: dict) -> str:
    status = history["status"]
    if status == "extracted":
        return (f"extracted {history['commits']} commit(s), "
                f"{history['co_change_edges']} co-change edge(s)")
    if status == "current":
        return "up to date"
    if status == "unavailable":
        return f"unavailable ({history['reason']})"
    return f"{status} ({history.get('error') or history.get('message', '')})"


@main.command()
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to inspect.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def status(path: str, as_json: bool, no_auto_index: bool) -> None:
    """Show last index run stats and node/edge counts by type."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        run = store.last_index_run()
        node_counts = store.counts_by_type("nodes")
        edge_counts = store.counts_by_type("edges")
        file_count = store.file_count()

    if as_json:
        click.echo(json.dumps({
            "last_run": dict(run) if run else None,
            "files": file_count,
            "nodes_by_type": node_counts,
            "edges_by_type": edge_counts,
        }, indent=2))
        return

    if run is None:
        click.echo("No index runs yet. Run `repobrain index`.")
        return
    click.echo("Last index run")
    click.echo(f"  started  : {run['started_at']}")
    click.echo(f"  finished : {run['finished_at']}")
    click.echo(f"  files scanned : {run['files_scanned']}")
    click.echo(f"  files changed : {run['files_changed']}")
    click.echo(f"  nodes created : {run['nodes_created']}")
    click.echo(f"  edges created : {run['edges_created']}")
    warnings = json.loads(run["warnings_json"] or "[]")
    if warnings:
        click.echo(f"  warnings      : {len(warnings)}")
    click.echo(f"\nActive files: {file_count}")
    click.echo("\nNodes by type")
    for type_, count in node_counts.items():
        click.echo(f"  {type_:<20} {count}")
    click.echo("\nEdges by type")
    for type_, count in edge_counts.items():
        click.echo(f"  {type_:<20} {count}")


@main.command("brief")
@click.option("--budget", type=click.IntRange(min=MINIMUM_BUDGET), default=DEFAULT_BUDGET,
              show_default=True, help="Approximate token budget (ceil(chars/4)).")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def brief(budget: int, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Emit a compact, grounded orientation pack for a new agent session."""
    root = _resolve_root(path)
    with _open_store(root, gate=False) as store:
        result = project_brief(root, store, budget=budget, auto_index=not no_auto_index)
    if result["freshness"]["status"] == "reindexed" and not as_json:
        click.echo(f"Freshness: {result['freshness']['message']}", err=True)
    click.echo(json.dumps(result, indent=2) if as_json else result["text"], nl=as_json)


@main.command("change-context")
@click.option("--base", default=None, help="Compare merge-base(BASE, HEAD) to HEAD.")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose change set to inspect.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def change_context(base: str | None, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Explain a working-tree or branch diff with impact, tests, and doc review."""
    root = _resolve_root(path)
    try:
        with _open_store(root, gate=False) as store:
            result = run_change_context(root, store, base=base,
                                        auto_index=not no_auto_index)
    except GitDiffError as exc:
        raise click.ClickException(str(exc)) from exc
    if result["status"] != "ok":
        raise click.ClickException(result["freshness"]["message"])
    click.echo(json.dumps(result, indent=2) if as_json else result["text"], nl=as_json)


@main.group()
def history() -> None:
    """Query deterministic Git history evidence (co-change, churn, ownership)."""


def _emit_history_report(result: dict, as_json: bool) -> None:
    if result["status"] != "ok":
        raise click.ClickException(result["message"])
    click.echo(json.dumps(result, indent=2) if as_json else result["text"], nl=as_json)


@history.command("co-change")
@click.argument("filepath")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose history to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def history_co_change(filepath: str, limit: int, path: str,
                      as_json: bool, no_auto_index: bool) -> None:
    """Files that historically change together with FILEPATH (heuristic)."""
    root = _resolve_root(path)
    with _open_store(root, gate=False) as store:
        result = co_change_report(root, store, filepath, limit=limit,
                                  auto_index=not no_auto_index)
    _emit_history_report(result, as_json)


@history.command("hotspots")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose history to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def history_hotspots(limit: int, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Churn hotspots: commit counts and added/deleted lines per file."""
    root = _resolve_root(path)
    with _open_store(root, gate=False) as store:
        result = churn_report(root, store, limit=limit, auto_index=not no_auto_index)
    _emit_history_report(result, as_json)


@history.command("owners")
@click.argument("filepath", required=False, default=None)
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose history to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def history_owners(filepath: str | None, limit: int, path: str,
                   as_json: bool, no_auto_index: bool) -> None:
    """Observed contribution history (not an authorization claim)."""
    root = _resolve_root(path)
    with _open_store(root, gate=False) as store:
        result = ownership_report(root, store, filepath, limit=limit,
                                  auto_index=not no_auto_index)
    _emit_history_report(result, as_json)


@main.command("install-agent")
@click.argument("path", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--git-hooks", is_flag=True,
              help="Also install idempotent post-commit and post-merge indexing hooks.")
def install_agent(path: str, git_hooks: bool) -> None:
    """Install MCP and Claude context; optionally install Git indexing hooks."""
    try:
        result = run_install_agent(_resolve_root(path), git_hooks=git_hooks)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@main.command("uninstall-agent")
@click.argument("path", type=click.Path(exists=True, file_okay=False), default=".")
def uninstall_agent(path: str) -> None:
    """Remove only RepoBrain-owned MCP, Claude, and Git hook entries."""
    try:
        result = run_uninstall_agent(_resolve_root(path))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.argument("query")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to search.")
@click.option("--limit", type=int, default=10, show_default=True)
@click.option("--type", "node_type", default=None, help="Filter by node type, e.g. MarkdownSection.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def search(query: str, path: str, limit: int, node_type: str | None,
           as_json: bool, no_auto_index: bool) -> None:
    """Full-text + name search across the indexed graph."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        results = keyword_search(store, query, limit=limit, node_type=node_type)

    if as_json:
        click.echo(json.dumps([r.to_dict() for r in results], indent=2))
        return
    if not results:
        click.echo("No results.")
        return
    for i, r in enumerate(results, 1):
        lines = ""
        if r.start_line:
            lines = f":{r.start_line}" + (f"-{r.end_line}" if r.end_line else "")
        click.echo(f"{i}. {r.path}{lines}  [{r.node_type or '?'}]  score={r.score:.2f}")
        click.echo(f"   name: {r.name}   reason: {', '.join(r.reasons)}")
        if r.snippet:
            snippet = " ".join(r.snippet.split())
            click.echo(f"   {snippet[:200]}")


@main.command("find-symbol")
@click.argument("name")
@click.option("--exact", is_flag=True, help="Match the symbol name exactly.")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to search.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def find_symbol(name: str, exact: bool, limit: int, path: str,
                as_json: bool, no_auto_index: bool) -> None:
    """Find code symbols (functions, classes, methods, variables, modules)."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        results = run_find_symbol(store, name, exact=exact, limit=limit)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("No symbols found.")
        return
    for i, r in enumerate(results, 1):
        lines = ""
        if r["start_line"]:
            lines = f":{r['start_line']}"
            if r["end_line"] and r["end_line"] != r["start_line"]:
                lines += f"-{r['end_line']}"
        click.echo(f"{i}. {r['name']}  [{r['type']}]  {r['path']}{lines}")
        detail = r["qualified_name"] or ""
        if r["signature"]:
            detail += f"   {r['signature']}"
        if detail:
            click.echo(f"   {detail}")


@main.command("docs-for-code")
@click.argument("target")
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def docs_for_code(target: str, limit: int, path: str,
                  as_json: bool, no_auto_index: bool) -> None:
    """Find Markdown sections that reference a code file or unique symbol."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        results = run_docs_for_code(store, target, limit=limit)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("No documentation references found.")
        return
    for i, result in enumerate(results, 1):
        line = f":{result['start_line']}" if result["start_line"] else ""
        click.echo(
            f"{i}. {result['doc_path']}{line}  [{result['doc_type']}]  "
            f"{result['section']}"
        )
        click.echo(
            f"   {result['reference']} -> {result['target_name']}  "
            f"[{result['target_type']}]  conf={result['confidence']:.2f}"
        )


@main.command("code-for-docs")
@click.argument("doc_path")
@click.option("--heading", default=None, help="Only references in this heading.")
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def code_for_docs(
    doc_path: str, heading: str | None, limit: int, path: str,
    as_json: bool, no_auto_index: bool,
) -> None:
    """Find code and graph nodes referenced by a Markdown document."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        results = run_code_for_docs(store, doc_path, heading=heading, limit=limit)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("No related code found.")
        return
    for i, result in enumerate(results, 1):
        source_line = f":{result['start_line']}" if result["start_line"] else ""
        target_line = (
            f":{result['target_start_line']}" if result["target_start_line"] else ""
        )
        click.echo(
            f"{i}. {result['path']}{target_line}  [{result['type']}]  {result['name']}"
        )
        click.echo(
            f"   {result['source_path']}{source_line} · {result['section']} · "
            f"`{result['reference']}`  conf={result['confidence']:.2f}"
        )


@main.group()
def explain() -> None:
    """Explain parts of the indexed repository."""


@explain.command("project")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--json", "as_json", is_flag=True)
@_freshness_option
def explain_project(path: str, as_json: bool, no_auto_index: bool) -> None:
    """Show a source-grounded project overview."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        info = project_overview(store)
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    click.echo(f"Repository: {info['root']}")
    click.echo(f"Files: {info['files']}  Nodes: {sum(info['nodes_by_type'].values())}  Edges: {sum(info['edges_by_type'].values())}")
    click.echo("Languages: " + ", ".join(f"{k}={v}" for k,v in info["languages"].items()))
    click.echo("Entrypoints: " + (", ".join(x["name"] for x in info["entrypoints"]) or "none detected"))


def _echo_symbol_tree(entries: list[dict], depth: int) -> None:
    for entry in entries:
        lines = f":{entry['start_line']}" if entry["start_line"] else ""
        if entry["end_line"] and entry["end_line"] != entry["start_line"]:
            lines += f"-{entry['end_line']}"
        indent = "  " * depth
        click.echo(f"  {indent}{entry['name']}  [{entry['type']}]{lines}")
        _echo_symbol_tree(entry["children"], depth + 1)


@explain.command("file")
@click.argument("filepath")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def explain_file(filepath: str, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Explain FILEPATH: symbols, imports, callers, env vars, tests, docs."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        info = run_explain_file(store, filepath)

    if info is None:
        raise click.ClickException(
            f"'{filepath}' is not an indexed file. Run `repobrain index` first, "
            "or pass the path relative to the indexed root."
        )
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return

    header = f"{info['path']}  [{info['language'] or 'unknown'}]"
    if info["line_count"]:
        header += f"  {info['line_count']} lines"
    click.echo(header)
    if info["module"]:
        suffix = "  (test file)" if info["module"]["is_test_file"] else ""
        click.echo(f"module: {info['module']['qualified_name']}{suffix}")
    if info["node_counts"]:
        counts = ", ".join(f"{t}={c}" for t, c in info["node_counts"].items())
        click.echo(f"nodes: {counts}")

    click.echo("\nSymbols")
    if info["symbols"]:
        _echo_symbol_tree(info["symbols"], 0)
    else:
        click.echo("  none")

    click.echo("\nImports")
    for imp in info["imports"]["internal"]:
        click.echo(f"  {imp['module']}  ({imp['path']}:{imp['line']})")
    if info["imports"]["external"]:
        click.echo(f"  external: {', '.join(info['imports']['external'])}")
    if not info["imports"]["internal"] and not info["imports"]["external"]:
        click.echo("  none")

    click.echo("\nImported by")
    if info["imported_by"]:
        for imp in info["imported_by"]:
            click.echo(f"  {imp['module']}  ({imp['path']})")
    else:
        click.echo("  none")

    click.echo("\nCalls out")
    if info["calls_out"]:
        for c in info["calls_out"]:
            flag = "  (inferred)" if c["is_inferred"] else ""
            click.echo(
                f"  {c['caller']} -> {c['callee']}  "
                f"({c['callee_path']}, line {c['start_line']}, "
                f"conf {c['confidence']:.1f}){flag}"
            )
    else:
        click.echo("  none")

    click.echo("\nCalled by")
    if info["called_by"]:
        for c in info["called_by"]:
            flag = "  (inferred)" if c["is_inferred"] else ""
            click.echo(
                f"  {c['caller']} -> {c['callee']}  "
                f"({c['caller_path']}:{c['start_line']}, conf {c['confidence']:.1f}){flag}"
            )
    else:
        click.echo("  none")

    click.echo("\nInstantiates")
    if info["instantiates"]:
        for c in info["instantiates"]:
            flag = "  (inferred)" if c["is_inferred"] else ""
            click.echo(
                f"  {c['caller']} -> {c['class_']}  "
                f"({c['class_path']}, line {c['start_line']}, "
                f"conf {c['confidence']:.1f}){flag}"
            )
    else:
        click.echo("  none")

    click.echo("\nInstantiated by")
    if info["instantiated_by"]:
        for c in info["instantiated_by"]:
            flag = "  (inferred)" if c["is_inferred"] else ""
            click.echo(
                f"  {c['caller']} -> {c['class_']}  "
                f"({c['caller_path']}:{c['start_line']}, conf {c['confidence']:.1f}){flag}"
            )
    else:
        click.echo("  none")

    click.echo("\nEnvironment variables read")
    if info["env_vars"]:
        for e in info["env_vars"]:
            click.echo(f"  {e['var']}  (line {e['start_line']}, by {e['reader']})")
    else:
        click.echo("  none")

    click.echo("\nRelated tests")
    if info["tests"]:
        for t in info["tests"]:
            click.echo(f"  {t['path']}  ({t['via']})")
    else:
        click.echo("  none")

    click.echo("\nReferencing docs")
    if info["docs"]:
        for d in info["docs"]:
            click.echo(f"  {d['path']}  [{d['type']}] {d['name']}")
    else:
        click.echo("  none yet")


@main.group()
def trace() -> None:
    """Trace relationships across the indexed repository."""


@trace.command("config")
@click.argument("name")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to query.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def trace_config(name: str, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Trace config or environment variable NAME to definitions and code reads."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        result = run_trace_config(store, name)
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if not result["definitions"] and not result["usages"]:
        click.echo(f"No definitions or usages found for {name}.")
        return
    click.echo(f"Config trace: {name}")
    click.echo("\nDefinitions")
    if result["definitions"]:
        for item in result["definitions"]:
            line = f":{item['start_line']}" if item["start_line"] else ""
            click.echo(f"  {item['path']}{line}  [{item['type']}]  {item['qualified_name']}")
    else:
        click.echo("  none")
    click.echo("\nRuntime usages")
    if result["usages"]:
        for item in result["usages"]:
            line = f":{item['start_line']}" if item["start_line"] else ""
            click.echo(f"  {item['path']}{line}  [{item['type']}]  {item['qualified_name']}")
    else:
        click.echo("  none")


@trace.command("data-flow")
@click.argument("start")
@click.option("--depth", type=click.IntRange(0, 10), default=4)
@click.option("--direction", type=click.Choice(["in", "out", "both"]), default="both")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--json", "as_json", is_flag=True)
@_freshness_option
def trace_data_flow(start: str, depth: int, direction: str, path: str,
                    as_json: bool, no_auto_index: bool) -> None:
    """Trace a route, event, file, or symbol through the graph."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        result = run_trace_data_flow(store, start, depth=depth, direction=direction)
    if result is None:
        raise click.ClickException(f"No unique indexed target found for '{start}'.")
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Start: {result['start']['name']} [{result['start']['type']}]")
    by_id = {n["id"]: n for n in result["nodes"]}
    for e in result["edges"]:
        click.echo(f"  d{e['depth']} {by_id[e['source']]['name']} -{e['type']}-> {by_id[e['target']]['name']} ({e['path']}:{e['start_line'] or '?'}, conf={e['confidence']:.2f})")


@main.command("impact")
@click.argument("target")
@click.option("--change-type", type=click.Choice(CHANGE_TYPES), default="modify",
              show_default=True,
              help="Change scenario label used in the impact report.")
@click.option("--depth", type=click.IntRange(1, 10), default=3)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--json", "as_json", is_flag=True)
@_freshness_option
def impact(target: str, change_type: str, depth: int, path: str,
           as_json: bool, no_auto_index: bool) -> None:
    """Estimate likely blast radius for a file or symbol change."""
    with _open_store(_resolve_root(path), auto_index=not no_auto_index) as store:
        history = (store.last_freshness or {}).get("history")
        result = run_impact_analysis(store, target, change_type, depth,
                                     history=history)
    if result is None:
        raise click.ClickException(f"No unique indexed target found for '{target}'.")
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    for title,key in (("High-confidence impact","high_confidence"),("Medium-confidence impact","medium_confidence"),("Low-confidence possible impact","low_confidence"),("Recommended tests","recommended_tests"),("Docs likely needing updates","docs_likely_needing_updates")):
        click.echo(f"\n{title}")
        for item in result[key]:
            click.echo(f"  {item['node']['path']} [{item['node']['type']}] via {item['via']} conf={item['confidence']:.2f}")
        if not result[key]:
            click.echo("  none")
    click.echo("\nHistorical co-change (heuristic)")
    historical = result["historical_evidence"]
    for item in historical["items"]:
        click.echo(f"  {item['node']['path']} via {item['via']} "
                   f"support={item['support']} score={item['score']:.2f} "
                   f"conf={item['confidence']:.2f}")
    if not historical["items"]:
        detail = f" ({historical['explanation']})" if historical.get("status") else ""
        click.echo(f"  none{detail}")


@main.command("report")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".")
@_freshness_option
def report(path: str, no_auto_index: bool) -> None:
    """Generate Markdown and HTML graph reports."""
    root = _resolve_root(path)
    with _open_store(root, auto_index=not no_auto_index) as store:
        md, html_path = generate_report(store, root)
    click.echo(f"Generated {md}")
    click.echo(f"Generated {html_path}")


@main.group()
def memory() -> None:
    """Read and write durable agent handoff memory."""


@memory.command("read")
@click.option("--topic", default=None, help="Only sessions mentioning this topic.")
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def memory_read(topic: str | None, limit: int, path: str,
                as_json: bool, no_auto_index: bool) -> None:
    """Read recent durable agent sessions."""
    root = _resolve_root(path)
    with _open_store(root, auto_index=not no_auto_index) as store:
        result = read_agent_memory(root, topic=topic, limit=limit, store=store)
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if not result["entries"]:
        click.echo("No agent memory found.")
        return
    for entry in result["entries"]:
        click.echo(f"{entry['created_at']}  {entry['summary']}")
        for label, key in (("decisions", "decisions"), ("assumptions", "assumptions"),
                           ("questions", "open_questions"), ("next", "next_steps")):
            if entry.get(key):
                click.echo(f"  {label}: {'; '.join(entry[key])}")
        click.echo(f"  verification: {entry['verification']['verdict']}")


@memory.command("verify")
@click.option("--limit", type=click.IntRange(min=1), default=1000, show_default=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_freshness_option
def memory_verify(limit: int, path: str, as_json: bool, no_auto_index: bool) -> None:
    """Validate stored memory anchors against the current graph."""
    root = _resolve_root(path)
    with _open_store(root, auto_index=not no_auto_index) as store:
        result = verify_agent_memory(root, store, limit=limit)
    click.echo(json.dumps(result, indent=2) if as_json else result["text"], nl=as_json)


@memory.command("write")
@click.option("--from-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="JSON object or Markdown/text to use as the session summary.")
@click.option("--summary", default=None, help="Session summary.")
@click.option("--decision", "decisions", multiple=True)
@click.option("--assumption", "assumptions", multiple=True)
@click.option("--open-question", "open_questions", multiple=True)
@click.option("--changed-file", "changed_files", multiple=True)
@click.option("--next-step", "next_steps", multiple=True)
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root.")
def memory_write(from_file: str | None, summary: str | None, decisions: tuple[str, ...],
                 assumptions: tuple[str, ...], open_questions: tuple[str, ...],
                 changed_files: tuple[str, ...], next_steps: tuple[str, ...], path: str) -> None:
    """Append a structured session to graph memory and AGENT_HANDOFF.md."""
    payload: dict = {}
    if from_file:
        content = Path(from_file).read_text(encoding="utf-8")
        try:
            parsed = json.loads(content)
            payload = parsed if isinstance(parsed, dict) else {"summary": content}
        except json.JSONDecodeError:
            payload = {"summary": content.strip()}
    payload.update({key: value for key, value in {
        "summary": summary, "decisions": decisions, "assumptions": assumptions,
        "open_questions": open_questions, "changed_files": changed_files,
        "next_steps": next_steps,
    }.items() if value})
    if not payload.get("summary"):
        raise click.UsageError("Provide --summary or --from-file.")
    try:
        result = write_agent_memory(_resolve_root(path), **payload)
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root served by MCP.")
def mcp(path: str) -> None:
    """Run the local RepoBrain MCP server over stdio."""
    try:
        from .mcp_server import run_server
        run_server(_resolve_root(path))
    except ImportError as exc:
        raise click.ClickException(
            "MCP support is not installed. Run `pip install 'repobrain[mcp]'`."
        ) from exc


if __name__ == "__main__":
    main()

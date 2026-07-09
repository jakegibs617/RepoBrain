"""RepoBrain command-line interface."""
from __future__ import annotations

import json
from pathlib import Path

import click

from .config import CONFIG_FILENAME, REPOBRAIN_DIR, RepoBrainConfig
from .graph.queries import explain_file as run_explain_file
from .graph.queries import find_symbol as run_find_symbol
from .graph.store import GraphStore
from .indexing.indexer import Indexer, RepoRootMismatchError
from .retrieval.keyword import search as keyword_search


def _resolve_root(path: str) -> Path:
    return Path(path).resolve()


def _open_store(root: Path) -> GraphStore:
    """Open the database that lives inside `root`'s own .repobrain/."""
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.exists():
        raise click.ClickException(
            f"No RepoBrain database at {db_path}. Run `repobrain index {root}` first."
        )
    return GraphStore(db_path)


@click.group()
def main() -> None:
    """RepoBrain: a local-first second brain for AI coding agents."""


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
def index(path: str, no_incremental: bool) -> None:
    """Index PATH (default: cwd) into PATH's own .repobrain/ database.

    The database is pinned to the indexed root; indexing a different root
    always uses (or creates) that root's own database instead of purging
    this one.
    """
    root = _resolve_root(path)
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    with GraphStore(db_path) as store:
        indexer = Indexer(store, config=config)
        try:
            stats = indexer.index(root, incremental=not no_incremental)
        except RepoRootMismatchError as exc:
            raise click.ClickException(str(exc))
    click.echo(f"Indexed {path}")
    click.echo(f"  files scanned : {stats.files_scanned}")
    click.echo(f"  files changed : {stats.files_changed}")
    click.echo(f"  files deleted : {stats.files_deleted}")
    click.echo(f"  nodes written : {stats.nodes_created}")
    click.echo(f"  edges written : {stats.edges_created}")
    for warning in stats.warnings:
        click.echo(f"  warning: {warning}")


@main.command()
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to inspect.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def status(path: str, as_json: bool) -> None:
    """Show last index run stats and node/edge counts by type."""
    with _open_store(_resolve_root(path)) as store:
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


@main.command()
@click.argument("query")
@click.option("--path", "path", type=click.Path(exists=True, file_okay=False), default=".",
              show_default=True, help="Repository root whose database to search.")
@click.option("--limit", type=int, default=10, show_default=True)
@click.option("--type", "node_type", default=None, help="Filter by node type, e.g. MarkdownSection.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def search(query: str, path: str, limit: int, node_type: str | None, as_json: bool) -> None:
    """Full-text + name search across the indexed graph."""
    with _open_store(_resolve_root(path)) as store:
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
def find_symbol(name: str, exact: bool, limit: int, path: str, as_json: bool) -> None:
    """Find code symbols (functions, classes, methods, variables, modules)."""
    with _open_store(_resolve_root(path)) as store:
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


@main.group()
def explain() -> None:
    """Explain parts of the indexed repository."""


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
def explain_file(filepath: str, path: str, as_json: bool) -> None:
    """Explain FILEPATH: symbols, imports, callers, env vars, tests, docs."""
    with _open_store(_resolve_root(path)) as store:
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


if __name__ == "__main__":
    main()

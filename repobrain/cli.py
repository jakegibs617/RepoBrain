"""RepoBrain command-line interface."""
from __future__ import annotations

import json
from pathlib import Path

import click

from .config import CONFIG_FILENAME, REPOBRAIN_DIR, RepoBrainConfig
from .graph.store import GraphStore
from .indexing.indexer import Indexer
from .retrieval.keyword import search as keyword_search


def _repobrain_dir(root: Path) -> Path:
    return root / REPOBRAIN_DIR


def _open_store(root: Path) -> GraphStore:
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.exists():
        raise click.ClickException(
            f"No RepoBrain database at {db_path}. Run `repobrain init` and `repobrain index` first."
        )
    return GraphStore(db_path)


@click.group()
def main() -> None:
    """RepoBrain: a local-first second brain for AI coding agents."""


@main.command()
def init() -> None:
    """Create .repobrain/ with a default config in the current directory."""
    root = Path.cwd()
    rb_dir = _repobrain_dir(root)
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
    """Index PATH (default: current directory) into the local graph database."""
    root = Path.cwd()
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.parent.exists():
        raise click.ClickException("Not initialized. Run `repobrain init` first.")
    with GraphStore(db_path) as store:
        indexer = Indexer(store, config=config)
        stats = indexer.index(Path(path), incremental=not no_incremental)
    click.echo(f"Indexed {path}")
    click.echo(f"  files scanned : {stats.files_scanned}")
    click.echo(f"  files changed : {stats.files_changed}")
    click.echo(f"  files deleted : {stats.files_deleted}")
    click.echo(f"  nodes written : {stats.nodes_created}")
    click.echo(f"  edges written : {stats.edges_created}")
    for warning in stats.warnings:
        click.echo(f"  warning: {warning}")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def status(as_json: bool) -> None:
    """Show last index run stats and node/edge counts by type."""
    with _open_store(Path.cwd()) as store:
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
@click.option("--limit", type=int, default=10, show_default=True)
@click.option("--type", "node_type", default=None, help="Filter by node type, e.g. MarkdownSection.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def search(query: str, limit: int, node_type: str | None, as_json: bool) -> None:
    """Full-text + name search across the indexed graph."""
    with _open_store(Path.cwd()) as store:
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


if __name__ == "__main__":
    main()

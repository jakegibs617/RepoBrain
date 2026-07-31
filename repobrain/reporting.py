"""Grounded project overview and Markdown/HTML report generation."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .graph.store import GraphStore

def project_overview(store: GraphStore) -> dict:
    run = store.last_index_run()
    languages = {r["language"] or "unknown": r["c"] for r in store.conn.execute("SELECT language,COUNT(*) c FROM files WHERE status='active' GROUP BY language ORDER BY c DESC")}
    def nodes(*types: str, limit: int = 50):
        marks = ",".join("?" for _ in types)
        return [dict(r) for r in store.conn.execute(f"SELECT type,name,path,start_line,metadata_json FROM nodes WHERE type IN ({marks}) ORDER BY path,start_line LIMIT ?", (*types, limit))]
    return {"root": store.get_meta("root"), "files": store.file_count(), "languages": languages,
            "nodes_by_type": store.counts_by_type("nodes"), "edges_by_type": store.counts_by_type("edges"),
            "entrypoints": nodes("Route", "CLICommand"),
            "config": nodes("ConfigFile","ConfigKey","EnvVar"),
            "workflows": nodes("GitHubWorkflow","GitHubJob","GitHubStep"),
            "services": nodes("DockerService","KubernetesResource"),
            "warnings": json.loads(run["warnings_json"] or "[]") if run else []}

def generate_report(store: GraphStore, root: Path) -> tuple[Path, Path]:
    data = project_overview(store)
    def listing(items):
        return "\n".join(f"- {i['type']}: `{i['name']}` — `{i['path']}`" for i in items) or "- None detected"
    md = f"""# RepoBrain Graph Report

Generated from `{data['root']}` using deterministic, source-grounded graph facts.

## Project Summary

- Active files: {data['files']}
- Graph nodes: {sum(data['nodes_by_type'].values())}
- Graph edges: {sum(data['edges_by_type'].values())}

## Files by Language/Type

{listing([{'type':'language','name':k,'path':str(v)+' files'} for k,v in data['languages'].items()])}

## Detected Entrypoints

{listing(data['entrypoints'])}

## Detected Config and Environment

{listing(data['config'])}

## Detected Workflows

{listing(data['workflows'])}

## Detected Services

{listing(data['services'])}

## Graph Stats

{listing([{'type':'node','name':k,'path':str(v)} for k,v in data['nodes_by_type'].items()])}

{listing([{'type':'edge','name':k,'path':str(v)} for k,v in data['edges_by_type'].items()])}

## Indexing Warnings

{listing([{'type':'warning','name':w,'path':''} for w in data['warnings']])}

## Unsupported Files and Suggested Improvements

- Files classified as `unknown` receive generic full-text indexing only.
- Add a deterministic parser or adapter when an important runtime artifact is missing.
"""
    out=root/".repobrain"
    out.mkdir(parents=True,exist_ok=True)
    md_path=out/"graph_report.md"
    html_path=out/"graph_report.html"
    md_path.write_text(md,encoding="utf-8")
    html_path.write_text("<!doctype html><html><head><meta charset='utf-8'><title>RepoBrain Report</title><style>body{max-width:900px;margin:40px auto;font:16px/1.5 system-ui;padding:0 20px}pre{white-space:pre-wrap}</style></head><body><pre>"+html.escape(md)+"</pre></body></html>",encoding="utf-8")
    return md_path,html_path

"""Durable, human-readable agent session memory backed by the graph."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import RepoBrainConfig
from .graph.schema import Edge, EdgeType, FtsRow, Node, NodeType, node_id
from .graph.store import GraphStore

HANDOFF_FILENAME = "AGENT_HANDOFF.md"
MEMORY_PATH = ".repobrain/agent_memory.md"
_KINDS = {
    "decisions": NodeType.DECISION,
    "assumptions": NodeType.ASSUMPTION,
    "open_questions": NodeType.OPEN_QUESTION,
    "next_steps": NodeType.TASK,
}


def _items(values: Iterable[str] | None) -> list[str]:
    return [str(value).strip() for value in (values or []) if str(value).strip()]


def _bullet_section(title: str, values: list[str]) -> str:
    lines = [f"### {title}"]
    lines.extend(f"- {value}" for value in values)
    if not values:
        lines.append("- None recorded.")
    return "\n".join(lines)


def _render_session(entry: dict) -> str:
    sections = [
        f"## Session {entry['created_at']}",
        "### Summary\n" + entry["summary"],
        _bullet_section("Decisions", entry["decisions"]),
        _bullet_section("Assumptions", entry["assumptions"]),
        _bullet_section("Open Questions", entry["open_questions"]),
        _bullet_section("Changed Files", entry["changed_files"]),
        _bullet_section("Next Steps", entry["next_steps"]),
    ]
    return "\n\n".join(sections) + "\n"


def _append_markdown(path: Path, session: str, *, title: str) -> None:
    """Append a session without rewriting user-authored handoff content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        path.write_text(existing + separator + session, encoding="utf-8")
    else:
        path.write_text(f"# {title}\n\n{session}", encoding="utf-8")


def write_agent_memory(
    root: str | Path,
    summary: str,
    *,
    decisions: Iterable[str] | None = None,
    assumptions: Iterable[str] | None = None,
    open_questions: Iterable[str] | None = None,
    changed_files: Iterable[str] | None = None,
    next_steps: Iterable[str] | None = None,
) -> dict:
    """Persist one session to Markdown and normalized graph nodes/edges."""
    root = Path(root).resolve()
    summary = summary.strip()
    if not summary:
        raise ValueError("summary must not be empty")
    created_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "created_at": created_at,
        "summary": summary,
        "decisions": _items(decisions),
        "assumptions": _items(assumptions),
        "open_questions": _items(open_questions),
        "changed_files": _items(changed_files),
        "next_steps": _items(next_steps),
    }
    rendered = _render_session(entry)
    _append_markdown(root / HANDOFF_FILENAME, rendered, title="Agent Handoff")
    _append_markdown(root / MEMORY_PATH, rendered, title="RepoBrain Agent Memory")

    config = RepoBrainConfig.load(root)
    session_name = f"Agent session {created_at}"
    session = Node(
        type=NodeType.AGENT_NOTE, name=session_name, qualified_name=created_at,
        path=MEMORY_PATH, metadata=entry, extractor="agent_memory",
    )
    nodes = [session]
    edges: list[Edge] = []
    fts = [FtsRow(MEMORY_PATH, session_name, summary, session.id)]
    for field, type_ in _KINDS.items():
        for position, text in enumerate(entry[field], 1):
            qualified = f"{created_at}:{field}:{position}"
            child = Node(type=type_, name=text, qualified_name=qualified, path=MEMORY_PATH,
                         metadata={"created_at": created_at, "memory_kind": field},
                         extractor="agent_memory")
            nodes.append(child)
            edges.append(Edge(type=EdgeType.CONTAINS, source_node_id=session.id,
                              target_node_id=child.id, path=MEMORY_PATH,
                              metadata={"memory_kind": field}, extractor="agent_memory"))
            fts.append(FtsRow(MEMORY_PATH, text, text, child.id))
    for changed in entry["changed_files"]:
        target = node_id(NodeType.FILE, changed, changed)
        # Only create graph relationships to files that are already indexed.
        edges.append(Edge(type=EdgeType.MAY_IMPACT, source_node_id=session.id,
                          target_node_id=target, path=MEMORY_PATH,
                          metadata={"changed_file": changed}, confidence=1.0,
                          extractor="agent_memory"))
    with GraphStore(root / config.db_path) as store:
        store.upsert_nodes(nodes)
        existing_ids = {row[0] for row in store.conn.execute("SELECT id FROM nodes")}
        store.upsert_edges([e for e in edges if e.target_node_id in existing_ids])
        store.add_fts_rows(fts)
        store.commit()
    return {"status": "ok", "created_at": created_at, "memory_file": MEMORY_PATH,
            "handoff_file": HANDOFF_FILENAME, "nodes_written": len(nodes),
            "edges_written": sum(e.target_node_id in existing_ids for e in edges)}


def read_agent_memory(root: str | Path, topic: str | None = None, limit: int = 10) -> dict:
    """Read newest structured sessions, optionally matching a topic."""
    root = Path(root).resolve()
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.exists():
        return {"status": "ok", "topic": topic, "entries": []}
    sql = "SELECT id, metadata_json FROM nodes WHERE type = ? AND extractor = 'agent_memory'"
    params: list[object] = [str(NodeType.AGENT_NOTE)]
    if topic and topic.strip():
        escaped = re.escape(topic.strip())
        sql += " AND (lower(name) LIKE lower(?) OR lower(metadata_json) LIKE lower(?))"
        value = f"%{topic.strip()}%"
        params.extend([value, value])
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, limit))
    with GraphStore(db_path) as store:
        rows = store.conn.execute(sql, params).fetchall()
    entries = [json.loads(row["metadata_json"] or "{}") for row in rows]
    return {"status": "ok", "topic": topic, "entries": entries}

"""Durable, human-readable agent session memory backed by the graph."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import RepoBrainConfig
from .graph.queries import resolve_graph_reference
from .graph.schema import Edge, EdgeType, FtsRow, Node, NodeType, file_node_id
from .graph.store import GraphStore

HANDOFF_FILENAME = "AGENT_HANDOFF.md"
MEMORY_PATH = ".repobrain/agent_memory.md"
_KINDS = {
    "decisions": NodeType.DECISION,
    "assumptions": NodeType.ASSUMPTION,
    "open_questions": NodeType.OPEN_QUESTION,
    "next_steps": NodeType.TASK,
}
_EXPLICIT_REFERENCE = re.compile(
    r"`([^`\n]+)`|(?<![\w.-])((?:[\w.@+-]+/)+[\w.@+-]+|[\w.@+-]+\.[A-Za-z0-9]{1,12})"
)
_CODE_TOKEN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b"
)


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


def _entry_texts(entry: dict) -> list[tuple[str, int, str]]:
    values = [("summary", 1, entry["summary"])]
    for field in (*_KINDS, "changed_files"):
        values.extend((field, position, text)
                      for position, text in enumerate(entry[field], 1))
    return values


def _reference_candidates(entry: dict) -> list[dict]:
    """Find bounded syntactic/code-shaped references in memory text."""
    candidates: dict[tuple[str, int, str], dict] = {}
    for field, position, text in _entry_texts(entry):
        explicit_matches = list(_EXPLICIT_REFERENCE.finditer(text))
        explicit = [match.group(1) or match.group(2) for match in explicit_matches]
        masked = list(text)
        for match in explicit_matches:
            masked[match.start():match.end()] = " " * (match.end() - match.start())
        vocabulary_text = "".join(masked)
        if field == "changed_files":
            explicit.insert(0, text)
        for value in explicit:
            key = (field, position, value.strip())
            candidates[key] = {"field": field, "position": position,
                               "reference": value.strip(), "source": "syntactic"}
        for token_match in _CODE_TOKEN.finditer(vocabulary_text):
            value = token_match.group(0)
            code_shaped = ("_" in value or "." in value
                           or any(char.isupper() for char in value[1:])
                           or value.isupper())
            if code_shaped:
                key = (field, position, value)
                candidates.setdefault(key, {
                    "field": field, "position": position,
                    "reference": value, "source": "code-shaped-token",
                })
    return [candidates[key] for key in sorted(candidates)]


def _extract_anchors(store: GraphStore, entry: dict) -> tuple[list[dict], list[dict]]:
    anchors, resolutions = [], []
    seen = set()
    for candidate in _reference_candidates(entry):
        resolved = resolve_graph_reference(store, candidate["reference"])
        evidence = {**candidate, **resolved}
        resolutions.append(evidence)
        if resolved["status"] != "resolved":
            continue
        node = resolved["node"]
        key = (candidate["field"], candidate["position"], node["id"])
        if key in seen:
            continue
        seen.add(key)
        anchors.append({
            "field": candidate["field"], "position": candidate["position"],
            "reference": candidate["reference"],
            "resolution_provenance": resolved["provenance"],
            "expected": node,
        })
    return anchors, resolutions


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
    config = RepoBrainConfig.load(root)
    with GraphStore(root / config.db_path) as store:
        from .freshness import ensure_fresh

        freshness = ensure_fresh(root, store)
        if freshness["can_query"]:
            anchors, resolutions = _extract_anchors(store, entry)
        else:
            anchors = []
            resolutions = [{
                "status": "unavailable", "reference": None,
                "provenance": "freshness-gate",
                "reason": freshness.get("reason"),
                "message": freshness.get("message"),
            }]
        entry["anchor_freshness"] = {
            key: freshness.get(key) for key in ("status", "can_query", "reason")
            if freshness.get(key) is not None
        }
        entry["anchors"] = anchors
        entry["anchor_resolutions"] = resolutions
        rendered = _render_session(entry)
        _append_markdown(root / HANDOFF_FILENAME, rendered, title="Agent Handoff")
        _append_markdown(root / MEMORY_PATH, rendered, title="RepoBrain Agent Memory")

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
                child_anchors = [anchor for anchor in anchors
                                 if anchor["field"] == field
                                 and anchor["position"] == position]
                child = Node(
                    type=type_, name=text, qualified_name=qualified, path=MEMORY_PATH,
                    metadata={"created_at": created_at, "memory_kind": field,
                              "anchors": child_anchors}, extractor="agent_memory",
                )
                nodes.append(child)
                edges.append(Edge(type=EdgeType.CONTAINS, source_node_id=session.id,
                                  target_node_id=child.id, path=MEMORY_PATH,
                                  metadata={"memory_kind": field}, extractor="agent_memory"))
                fts.append(FtsRow(MEMORY_PATH, text, text, child.id))
        for changed in entry["changed_files"]:
            target = file_node_id(changed)
            edges.append(Edge(type=EdgeType.MAY_IMPACT, source_node_id=session.id,
                              target_node_id=target, path=MEMORY_PATH,
                              metadata={"changed_file": changed}, confidence=1.0,
                              extractor="agent_memory"))
        store.upsert_nodes(nodes)
        existing_ids = {row[0] for row in store.conn.execute("SELECT id FROM nodes")}
        store.upsert_edges([e for e in edges if e.target_node_id in existing_ids])
        store.add_fts_rows(fts)
        store.commit()
    return {"status": "ok", "created_at": created_at, "memory_file": MEMORY_PATH,
            "handoff_file": HANDOFF_FILENAME, "nodes_written": len(nodes),
            "edges_written": sum(e.target_node_id in existing_ids for e in edges),
            "anchors_written": len(anchors),
            "ambiguous_references": sum(r["status"] == "ambiguous" for r in resolutions),
            "anchor_freshness": entry["anchor_freshness"]}


def _node_evidence(row) -> dict:
    return {key: row[key] for key in (
        "id", "type", "name", "qualified_name", "path", "start_line", "end_line"
    )}


def _renamed_anchor_target(store: GraphStore, expected: dict) -> dict:
    paths = store.conn.execute(
        "SELECT DISTINCT g.path FROM git_commit_files g "
        "JOIN files f ON f.path=g.path AND f.status='active' "
        "WHERE g.original_path=? ORDER BY g.path", (expected.get("path"),),
    ).fetchall()
    matches = []
    for path_row in paths:
        if expected["type"] == str(NodeType.FILE):
            rows = store.conn.execute(
                "SELECT id,type,name,qualified_name,path,start_line,end_line FROM nodes "
                "WHERE type='File' AND path=?", (path_row["path"],),
            ).fetchall()
        else:
            rows = store.conn.execute(
                "SELECT id,type,name,qualified_name,path,start_line,end_line FROM nodes "
                "WHERE type=? AND name=? AND path=?",
                (expected["type"], expected["name"], path_row["path"]),
            ).fetchall()
        matches.extend(_node_evidence(row) for row in rows)
    unique = {row["id"]: row for row in matches}
    if len(unique) == 1:
        return {"status": "resolved", "provenance": "git-rename-continuity",
                "node": next(iter(unique.values()))}
    if len(unique) > 1:
        return {"status": "ambiguous", "provenance": "git-rename-continuity",
                "candidates": list(unique.values())}
    return {"status": "unresolved", "provenance": "git-rename-continuity-unavailable"}


def _verify_anchor(store: GraphStore, anchor: dict) -> dict:
    expected = anchor["expected"]
    row = store.conn.execute(
        "SELECT id,type,name,qualified_name,path,start_line,end_line FROM nodes WHERE id=?",
        (expected["id"],),
    ).fetchone()
    resolution = ({"status": "resolved", "provenance": "stable-node-id",
                   "node": _node_evidence(row)} if row is not None
                  else _renamed_anchor_target(store, expected))
    found = resolution.get("node")
    if found is None:
        verdict = "invalidated"
    elif any(found.get(key) != expected.get(key)
             for key in ("path", "start_line", "end_line")):
        verdict = "drifted"
    else:
        verdict = "verified"
    return {
        **anchor, "verdict": verdict,
        "evidence": {"expected": expected, "found": found,
                     "provenance": resolution["provenance"],
                     "resolution_status": resolution["status"],
                     "candidates": resolution.get("candidates", [])},
    }


def verify_memory_entries(store: GraphStore, entries: list[dict]) -> list[dict]:
    """Annotate memory entries without mutating their stored metadata."""
    verified = []
    for original in entries:
        entry = json.loads(json.dumps(original))
        anchors = [_verify_anchor(store, anchor) for anchor in entry.get("anchors", [])]
        verdicts = {anchor["verdict"] for anchor in anchors}
        verdict = ("invalidated" if "invalidated" in verdicts else
                   "drifted" if "drifted" in verdicts else
                   "verified" if anchors else "unanchored")
        entry["verification"] = {
            "verdict": verdict, "anchors": anchors,
            "resolution_evidence": entry.get("anchor_resolutions", []),
        }
        verified.append(entry)
    return verified


def read_agent_memory(root: str | Path, topic: str | None = None, limit: int | None = 10,
                      *, store: GraphStore | None = None) -> dict:
    """Read newest structured sessions, optionally matching a topic."""
    root = Path(root).resolve()
    config = RepoBrainConfig.load(root)
    db_path = root / config.db_path
    if not db_path.exists():
        return {"status": "ok", "topic": topic, "entries": []}
    sql = "SELECT id, metadata_json FROM nodes WHERE type = ? AND extractor = 'agent_memory'"
    params: list[object] = [str(NodeType.AGENT_NOTE)]
    if topic and topic.strip():
        sql += " AND (lower(name) LIKE lower(?) OR lower(metadata_json) LIKE lower(?))"
        value = f"%{topic.strip()}%"
        params.extend([value, value])
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, limit))
    owns_store = store is None
    active_store = store or GraphStore(db_path)
    try:
        rows = active_store.conn.execute(sql, params).fetchall()
        entries = verify_memory_entries(
            active_store, [json.loads(row["metadata_json"] or "{}") for row in rows]
        )
    finally:
        if owns_store:
            active_store.close()
    return {"status": "ok", "topic": topic, "entries": entries}


def verify_agent_memory(root: str | Path, store: GraphStore, limit: int = 1000) -> dict:
    result = read_agent_memory(root, limit=limit, store=store)
    counts = {name: 0 for name in ("verified", "drifted", "invalidated", "unanchored")}
    for entry in result["entries"]:
        counts[entry["verification"]["verdict"]] += 1
    report = {"status": "ok", "counts": counts, "entries": result["entries"]}
    report["text"] = render_memory_verification(report)
    return report


def render_memory_verification(report: dict) -> str:
    counts = report["counts"]
    lines = [
        "RepoBrain memory verification",
        "Counts: " + ", ".join(f"{name}={counts[name]}" for name in
                                ("verified", "drifted", "invalidated", "unanchored")),
    ]
    for entry in report["entries"]:
        verification = entry["verification"]
        lines.append(f"- [{verification['verdict']}] {entry.get('created_at', 'unknown')} "
                     f"{entry.get('summary', '')}")
        for anchor in verification["anchors"]:
            evidence = anchor["evidence"]
            expected = evidence["expected"]
            found = evidence["found"]
            expected_at = f"{expected.get('path')}:{expected.get('start_line') or 1}"
            found_at = (f"{found.get('path')}:{found.get('start_line') or 1}"
                        if found else "not found")
            lines.append(f"  - {anchor['reference']}: {anchor['verdict']} "
                         f"expected {expected_at}; found {found_at}; "
                         f"via {evidence['provenance']}")
    return "\n".join(lines) + "\n"

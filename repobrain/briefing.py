"""Token-budgeted, source-grounded project orientation for coding agents."""
from __future__ import annotations

import re
from pathlib import Path

from .freshness import require_fresh
from .graph.store import GraphStore
from .memory import read_agent_memory

DEFAULT_BUDGET = 2000
MINIMUM_BUDGET = 64


def _source(row) -> str:
    path = row["path"] or ".repobrain/agent_memory.md"
    return f"{path}:{row['start_line']}" if row["start_line"] else path


def _node_facts(store: GraphStore, types: tuple[str, ...], limit: int,
                *, exclude_test_paths: bool = False) -> list[dict]:
    marks = ",".join("?" for _ in types)
    # A file the code parser classified as a test carries a TestFile node at the
    # same path. That is already in the graph, so "indexed but not
    # representative" needs no second mechanism to express.
    test_path_clause = (
        "AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.type='TestFile' AND t.path=nodes.path) "
        if exclude_test_paths else ""
    )
    rows = store.conn.execute(
        f"SELECT type,name,qualified_name,path,start_line,metadata_json FROM nodes "
        f"WHERE type IN ({marks}) AND start_line IS NOT NULL {test_path_clause}"
        f"ORDER BY length(path),path,start_line,name LIMIT ?",
        (*types, limit),
    ).fetchall()
    return [
        {"text": row["qualified_name"] or row["name"], "type": row["type"],
         "source": _source(row)}
        for row in rows
    ]


def _purpose_facts(store: GraphStore, limit: int = 3) -> list[dict]:
    rows = store.conn.execute(
        "SELECT n.name,n.path,n.start_line,f.content FROM nodes n "
        "LEFT JOIN content_fts f ON f.node_id=n.id "
        "WHERE n.type IN ('MarkdownDocument','MarkdownSection') "
        "AND (lower(n.name) IN ('readme','project summary','product vision','purpose') "
        "OR lower(n.path)='readme.md') "
        "ORDER BY CASE WHEN lower(n.path)='readme.md' THEN 0 ELSE 1 END,n.start_line LIMIT ?",
        (limit,),
    ).fetchall()
    facts = []
    for row in rows:
        raw = row["content"] or row["name"]
        paragraphs = [" ".join(block.split()) for block in re.split(r"\n\s*\n", raw)
                      if block.strip() and not block.lstrip().startswith("#")]
        content = paragraphs[0] if paragraphs else " ".join(raw.split())
        if content and content.casefold() != row["name"].casefold():
            facts.append({"text": content, "type": "Purpose", "source": _source(row)})
    return facts


def _memory_sections(root: Path, store: GraphStore, limit: int = 5) -> tuple:
    entries = read_agent_memory(root, limit=None, store=store)["entries"]
    memory_path = root / ".repobrain" / "agent_memory.md"
    memory_lines = memory_path.read_text(encoding="utf-8").splitlines() if memory_path.exists() else []

    def source_for(entry: dict, text: str) -> str:
        session_heading = f"## Session {entry.get('created_at', '')}"
        start = next((index for index, value in enumerate(memory_lines)
                      if value == session_heading), 0)
        end = next((index for index in range(start + 1, len(memory_lines))
                    if memory_lines[index].startswith("## Session ")), len(memory_lines))
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
        relative = next((index for index in range(start, end)
                         if first_line in memory_lines[index]), start)
        line = relative + 1
        return f".repobrain/agent_memory.md:{line}"

    alerts: list[dict] = []
    recent: list[dict] = []
    assumptions: list[dict] = []
    questions: list[dict] = []
    counts = {"invalidated": 0, "drifted": 0}
    current_entries = 0
    for entry in entries:
        verdict = entry["verification"]["verdict"]
        if verdict in counts:
            counts[verdict] += 1
            anchors = entry["verification"]["anchors"]
            detail = next((anchor for anchor in anchors
                           if anchor["verdict"] == verdict), None)
            suffix = ""
            if detail is not None:
                found = detail["evidence"]["found"]
                location = (f"{found['path']}:{found.get('start_line') or 1}"
                            if found else "not found")
                suffix = f" Anchor `{detail['reference']}`: {location}."
            alerts.append({
                "text": f"{entry.get('summary', '')}{suffix}",
                "type": "MemoryInvalidated" if verdict == "invalidated" else "MemoryDrifted",
                "source": source_for(entry, entry.get("summary", "")),
            })
            continue
        if current_entries >= limit:
            continue
        current_entries += 1
        if entry.get("summary"):
            recent.append({"text": entry["summary"], "type": "AgentNote",
                           "source": source_for(entry, entry["summary"])})
        assumptions.extend({"text": item, "type": "Assumption", "source": source_for(entry, item)}
                           for item in entry.get("assumptions", []))
        questions.extend({"text": item, "type": "OpenQuestion", "source": source_for(entry, item)}
                         for item in entry.get("open_questions", []))
    return alerts, recent, assumptions, questions, counts


def _fact_line(fact: dict) -> str:
    return f"- {fact['text']} [{fact['type']}] ({fact['source']})"


def _render(staleness: dict, sections: list[dict], memory_counts: dict) -> str:
    lines = ["RepoBrain project brief"]
    if staleness["is_stale"]:
        lines.append(
            f"STALE INDEX: {staleness['out_of_date_count']} file(s) are out of date; "
            "run `repobrain index` before relying on these facts."
        )
    else:
        lines.append("Index freshness: current.")
    if memory_counts["invalidated"] or memory_counts["drifted"]:
        lines.append(
            "Memory verification: "
            f"{memory_counts['invalidated']} invalidated, "
            f"{memory_counts['drifted']} drifted remembered entries need attention."
        )
    for section in sections:
        lines.extend(["", section["title"]])
        lines.extend(_fact_line(fact) for fact in section["facts"])
    return "\n".join(lines) + "\n"


def project_brief(
    root: str | Path,
    store: GraphStore,
    budget: int = DEFAULT_BUDGET,
    *,
    auto_index: bool = True,
) -> dict:
    """Build a brief whose approximate token count never exceeds ``budget``.

    Tokens use the documented deterministic chars/4 heuristic. Facts are added
    atomically in fixed section priority, so no source-grounded fact is cut.
    """
    if budget < MINIMUM_BUDGET:
        raise ValueError(f"budget must be at least {MINIMUM_BUDGET} tokens")
    root = Path(root).resolve()
    freshness_gate = require_fresh(root, store, auto_index=auto_index)
    freshness = freshness_gate.get("after") or freshness_gate["before"]
    alerts, recent, assumptions, questions, memory_counts = _memory_sections(root, store)
    candidates = [
        ("Memory requiring attention", alerts),
        ("Purpose", _purpose_facts(store)),
        ("Subsystems", _node_facts(store, ("Directory", "Module"), 12)),
        ("Entrypoints", _node_facts(store, ("Route",), 12, exclude_test_paths=True)),
        ("Configuration", _node_facts(store, ("ConfigFile", "ConfigKey", "EnvVar"), 12)),
        ("Active assumptions", assumptions),
        ("Open questions", questions),
        ("Recent memory", recent),
    ]
    selected: list[dict] = []
    for title, facts in candidates:
        kept: list[dict] = []
        for fact in facts:
            trial = selected + [{"title": title, "facts": kept + [fact]}]
            if len(_render(freshness, trial, memory_counts)) <= budget * 4:
                kept.append(fact)
        if kept:
            selected.append({"title": title, "facts": kept})
    text = _render(freshness, selected, memory_counts)
    return {
        "status": "ok",
        "budget": budget,
        "token_estimate": (len(text) + 3) // 4,
        "token_heuristic": "ceil(characters / 4)",
        "freshness": freshness_gate,
        "staleness": freshness,
        "memory_verification": memory_counts,
        "sections": selected,
        "text": text,
    }

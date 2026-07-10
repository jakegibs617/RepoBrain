"""Token-budgeted, source-grounded project orientation for coding agents."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .freshness import check_freshness
from .graph.store import GraphStore
from .memory import read_agent_memory

DEFAULT_BUDGET = 2000
MINIMUM_BUDGET = 64


def _source(row) -> str:
    path = row["path"] or ".repobrain/agent_memory.md"
    return f"{path}:{row['start_line']}" if row["start_line"] else path


def _node_facts(store: GraphStore, types: tuple[str, ...], limit: int) -> list[dict]:
    marks = ",".join("?" for _ in types)
    rows = store.conn.execute(
        f"SELECT type,name,qualified_name,path,start_line,metadata_json FROM nodes "
        f"WHERE type IN ({marks}) AND start_line IS NOT NULL "
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


def _memory_sections(root: Path, limit: int = 5) -> tuple[list[dict], list[dict], list[dict]]:
    entries = read_agent_memory(root, limit=limit)["entries"]
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

    recent, assumptions, questions = [], [], []
    for entry in entries:
        if entry.get("summary"):
            recent.append({"text": entry["summary"], "type": "AgentNote",
                           "source": source_for(entry, entry["summary"])})
        assumptions.extend({"text": item, "type": "Assumption", "source": source_for(entry, item)}
                           for item in entry.get("assumptions", []))
        questions.extend({"text": item, "type": "OpenQuestion", "source": source_for(entry, item)}
                         for item in entry.get("open_questions", []))
    return recent, assumptions, questions


def _fact_line(fact: dict) -> str:
    return f"- {fact['text']} [{fact['type']}] ({fact['source']})"


def _render(staleness: dict, sections: list[dict]) -> str:
    lines = ["RepoBrain project brief"]
    if staleness["is_stale"]:
        lines.append(
            f"STALE INDEX: {staleness['out_of_date_count']} file(s) are out of date; "
            "run `repobrain index` before relying on these facts."
        )
    else:
        lines.append("Index freshness: current.")
    for section in sections:
        lines.extend(["", section["title"]])
        lines.extend(_fact_line(fact) for fact in section["facts"])
    return "\n".join(lines) + "\n"


def project_brief(root: str | Path, store: GraphStore, budget: int = DEFAULT_BUDGET) -> dict:
    """Build a brief whose approximate token count never exceeds ``budget``.

    Tokens use the documented deterministic chars/4 heuristic. Facts are added
    atomically in fixed section priority, so no source-grounded fact is cut.
    """
    if budget < MINIMUM_BUDGET:
        raise ValueError(f"budget must be at least {MINIMUM_BUDGET} tokens")
    root = Path(root).resolve()
    freshness = check_freshness(root, store)
    recent, assumptions, questions = _memory_sections(root)
    candidates = [
        ("Purpose", _purpose_facts(store)),
        ("Subsystems", _node_facts(store, ("Directory", "Module", "Package"), 12)),
        ("Entrypoints", _node_facts(store, ("CLICommand", "Script"), 12)),
        ("Routes and config", _node_facts(store, ("Route", "Endpoint", "ConfigKey", "EnvVar"), 12)),
        ("Active assumptions", assumptions),
        ("Open questions", questions),
        ("Recent memory", recent),
    ]
    selected: list[dict] = []
    for title, facts in candidates:
        kept = []
        for fact in facts:
            trial = selected + [{"title": title, "facts": kept + [fact]}]
            if len(_render(freshness, trial)) <= budget * 4:
                kept.append(fact)
        if kept:
            selected.append({"title": title, "facts": kept})
    text = _render(freshness, selected)
    return {
        "status": "ok",
        "budget": budget,
        "token_estimate": (len(text) + 3) // 4,
        "token_heuristic": "ceil(characters / 4)",
        "staleness": freshness,
        "sections": selected,
        "text": text,
    }

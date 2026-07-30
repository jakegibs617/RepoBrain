"""Token-budgeted, source-grounded project orientation for coding agents."""
from __future__ import annotations

import re
from pathlib import Path

from .freshness import require_fresh
from .graph.store import GraphStore
from .memory import read_agent_memory

DEFAULT_BUDGET = 2000
MINIMUM_BUDGET = 64

#: Directory segments whose contents are indexed but do not describe the
#: project that ships them. Deliberately *not* the parser's `_TEST_DIRS`: that
#: set decides `TestFile` classification for the whole graph — including which
#: tests `impact` recommends running — and an `examples/` directory is not a
#: test. This one is local to promotion and costs no re-extraction.
_UNREPRESENTATIVE_DIRS = ("examples", "example", "fixtures", "fixture",
                          "samples", "sample")

#: Directory segments that *document* the project rather than constitute it.
#: Deliberately not folded into `_UNREPRESENTATIVE_DIRS`: documentation does
#: describe the project — that is its entire purpose — so it stays eligible
#: wherever prose is what the section wants. A `ConfigFile` under `docs/`,
#: though, illustrates configuration rather than performing it, and this
#: repository's own `docs/evaluation/*-facts.json` took ten of the twelve
#: Configuration slots on the strength of being `.json`.
_DOCUMENTATION_DIRS = ("docs", "doc")


def _source(row) -> str:
    keys = row.keys()
    if not row["path"] and "observed_path" in keys and row["observed_path"]:
        # D17 keys `EnvVar` on ``("EnvVar", name, "")`` so every reader converges
        # on one repo-global node. Pathless is not unlocatable: extraction
        # records where the node was observed, and that is the honest citation.
        line = row["observed_line"]
        return f"{row['observed_path']}:{line}" if line else row["observed_path"]
    path = row["path"] or ".repobrain/agent_memory.md"
    return f"{path}:{row['start_line']}" if row["start_line"] else path


#: Edges that say a node is *made of* something rather than *wired to*
#: something. Counting them ranks by symbol count, which is file size wearing
#: a graph costume — the biggest test file wins.
_STRUCTURAL_EDGES = ("DEFINES", "CONTAINS")

#: How connected a node is: the edges it is either end of, containment aside.
#: At module granularity this is almost entirely IMPORTS, i.e. how much of the
#: project is wired to it. Both endpoints are index-served.
_DEGREE = (
    "(SELECT count(*) FROM edges e "
    " WHERE (e.source_node_id=nodes.id OR e.target_node_id=nodes.id) "
    f" AND e.type NOT IN ({','.join('?' for _ in _STRUCTURAL_EDGES)}))"
)

#: Where extraction saw a repo-global node. `EnvVar` and `DockerImage` are keyed
#: on an empty path by D17 so that every reader converges on one node; the
#: observation is the only place their location survives.
_OBSERVED_PATH = "json_extract(metadata_json,'$.observation.path')"

#: The path a fact would be cited from — the node's own, or the observation's.
#: Every promotion predicate keys on this rather than on `nodes.path`, so that
#: withholding promotion from unrepresentative paths (D43, D46) reaches nodes
#: whose path lives one level down.
_LOCATION = f"COALESCE(NULLIF(nodes.path,''),{_OBSERVED_PATH})"

#: A block that opens with a list marker is an enumeration, not a description.
#: Structural on purpose: recognising a lead-in must not require a word list or
#: any heuristic about English, both of which would be wrong in some language
#: this indexer already parses.
_LIST_ITEM = re.compile(r"[-*+]\s|\d+[.)]\s")

#: A paragraph with no sentence terminator is not making a statement. This is
#: what separates `Implemented:` from the prose beneath it without measuring
#: length, which would be a threshold somebody has to tune.
_STATEMENT = re.compile(r"[.!?]")


def _node_facts(store: GraphStore, types: tuple[str, ...], limit: int,
                *, by_degree: bool = False, by_type: bool = False,
                withhold: tuple[str, ...] = _UNREPRESENTATIVE_DIRS) -> list[dict]:
    """Promote up to ``limit`` facts of ``types``, best first.

    ``by_type`` ranks by the order ``types`` is written in. Degree is the right
    relevance signal for code (D46) and the wrong one for configuration: 390 of
    the 419 edges touching this repository's config nodes are structural, so
    counting them ranks a file by how many keys it declares — file size wearing
    a graph costume, the very thing `_STRUCTURAL_EDGES` exists to refuse.
    """
    marks = ",".join("?" for _ in types)
    # A file the code parser classified as a test carries a TestFile node at the
    # same path. That is already in the graph, so "is a test" needs no second
    # mechanism to express. "Not representative" is the wider question and is
    # answered here, at promotion time, by path segment.
    # The trailing equality catches the directory node itself: `docs/*` does not
    # match `docs`, and a `Directory` node's path is the bare segment.
    globs = " OR ".join(
        f"{_LOCATION} GLOB ? OR {_LOCATION} GLOB ? OR {_LOCATION} = ?"
        for _ in withhold
    )
    patterns = [pattern for segment in withhold
                for pattern in (f"{segment}/*", f"*/{segment}/*", segment)]
    # Path length is a tie-break, never the ranking: it says nothing about
    # relevance, and on its own it hands the twelve promoted slots to whichever
    # modules happen to sit nearest the repository root.
    degree = f"{_DEGREE} DESC," if by_degree else ""
    # Rank by the order `types` is written in, so the section's own signature
    # states the priority: what must be set to run anything, then what governs
    # how it runs, then the individual keys inside those.
    priority = ("CASE type " + " ".join(
        f"WHEN '{type_}' THEN {index}" for index, type_ in enumerate(types)
    ) + " END,") if by_type else ""
    rows = store.conn.execute(
        f"SELECT type,name,qualified_name,path,start_line,metadata_json,"
        f"       {_OBSERVED_PATH} AS observed_path,"
        f"       json_extract(metadata_json,'$.observation.line') AS observed_line "
        f"FROM nodes "
        # Eligibility asks whether the brief can cite a source, which is not the
        # same question as whether the node has a line. A repo-global node has
        # neither path nor line and is still perfectly citable (D49).
        f"WHERE type IN ({marks}) AND {_LOCATION} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.type='TestFile' AND t.path={_LOCATION}) "
        f"AND NOT ({globs}) "
        # `start_line IS NULL` first: SQLite sorts NULLs ahead of values, which
        # handed the tie-break to exactly the facts that cannot cite a line.
        f"ORDER BY {priority}{degree} length({_LOCATION}),{_LOCATION},"
        f"start_line IS NULL,start_line,name LIMIT ?",
        (*types, *patterns, *(_STRUCTURAL_EDGES if by_degree else ()), limit),
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
        blocks = [block for block in re.split(r"\n\s*\n", raw)
                  if block.strip() and not block.lstrip().startswith("#")]
        paragraphs = [" ".join(block.split()) for block in blocks
                      if not _LIST_ITEM.match(block.lstrip())]
        # Prefer a paragraph that states something. A section whose body opens
        # with a bare lead-in — `Implemented:` — promoted the lead-in, and the
        # brief's whole claim is that everything in it is a fact.
        content = next((text for text in paragraphs if _STATEMENT.search(text)),
                       # Substance decides *which* paragraph, never whether the
                       # section exists: an absent `Purpose` is a worse outcome
                       # than a thin one, and a terse README is not a defect.
                       next(iter(paragraphs), " ".join(raw.split())))
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


def _render(staleness: dict, sections: list[dict], memory_counts: dict,
            truncation: dict | None = None) -> str:
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
    # Rendered here rather than appended by the caller, so that the trial
    # measurement during selection and the text finally emitted are produced by
    # the same code. Any separate estimate of the footer's cost is a second
    # number that can drift from the first.
    if truncation and truncation["applied"]:
        lines.append(f"\nTruncated to fit {truncation['budget']} tokens")
        lines.extend(f"- {title}: {count} fact(s) not shown"
                     for title, count in sorted(truncation["dropped"].items()))
        if not truncation.get("within_budget", True):
            lines.append("- Still over budget; nothing further can be dropped.")
    return "\n".join(lines) + "\n"


def _widest_footer(budget: int, candidates: list[tuple]) -> int:
    """Characters the omission report can cost at its very worst.

    Every section that has a candidate could lose all of them, and each count is
    at most the number of facts offered, so this is an exact upper bound and it
    is known before a single fact is selected.
    """
    worst = {title: len(facts) for title, facts in candidates if facts}
    truncation = {"applied": True, "budget": budget,
                  "within_budget": False, "dropped": worst}
    empty = _render({"is_stale": False}, [], {"invalidated": 0, "drifted": 0})
    footer = _render({"is_stale": False}, [], {"invalidated": 0, "drifted": 0},
                     truncation)
    return len(footer) - len(empty)


def _select(staleness: dict, candidates: list[tuple], memory_counts: dict,
            budget: int, reserve: int) -> tuple[list[dict], dict[str, int]]:
    """Keep every fact that fits alongside ``reserve``, and count the rest.

    Facts are offered one at a time and declined whole, so a fact that appears
    is never a fragment of itself. Declining is not trimming — nothing is cut
    after the fact — but the consequence for the caller is identical, which is
    why it reports through the same key D48 and D53 established.
    """
    selected: list[dict] = []
    dropped: dict[str, int] = {}
    for title, facts in candidates:
        kept: list[dict] = []
        for fact in facts:
            trial = selected + [{"title": title, "facts": kept + [fact]}]
            length = len(_render(staleness, trial, memory_counts)) + reserve
            if length <= budget * 4:
                kept.append(fact)
        # Only a shortfall is reported. A section with no candidates at all —
        # this repository's own `Entrypoints`, which has no `Route` node outside
        # test fixtures — is empty, not truncated, and saying otherwise would
        # invent a shortfall the graph does not have.
        if len(kept) < len(facts):
            dropped[title] = len(facts) - len(kept)
        if kept:
            selected.append({"title": title, "facts": kept})
    return selected, dropped


def project_brief(
    root: str | Path,
    store: GraphStore,
    budget: int = DEFAULT_BUDGET,
    *,
    auto_index: bool = True,
) -> dict:
    """Build a brief whose approximate token count never exceeds ``budget``.

    Tokens use the documented deterministic chars/4 heuristic. Facts are added
    atomically in fixed section priority, so no fact is ever shown in part —
    and the ones that did not fit are counted and reported, on both surfaces.
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
        ("Subsystems", _node_facts(
            store, ("Directory", "Module"), 12, by_degree=True,
            withhold=_UNREPRESENTATIVE_DIRS + _DOCUMENTATION_DIRS)),
        ("Entrypoints", _node_facts(store, ("Route",), 12)),
        ("Configuration", _node_facts(
            store, ("EnvVar", "ConfigFile", "ConfigKey"), 12, by_type=True,
            withhold=_UNREPRESENTATIVE_DIRS + _DOCUMENTATION_DIRS)),
        ("Active assumptions", assumptions),
        ("Open questions", questions),
        ("Recent memory", recent),
    ]
    # The report of the omissions has to be inside the budget rather than
    # appended past it (D48, D53): at budget 300 this repository's untruncated
    # brief already costs 297 tokens, so a footer added afterwards lands over.
    #
    # Two passes, never more. The first reserves nothing, and where everything
    # fits — the common case, including this repository at the default budget —
    # it is exact and final. Only when facts were declined does the second pass
    # run, and it reserves the *widest* footer the omissions could produce
    # rather than the one the first pass happened to produce. Re-running
    # selection against its own last footer looks like the obvious fixed point
    # and does not converge: declining an early fact frees room a later section
    # then claims, which changes the footer again. Measured, it oscillates
    # rather than settling, and an unsettled loop emits a brief whose text was
    # never the text that was measured.
    selected, dropped = _select(freshness, candidates, memory_counts, budget, 0)
    if dropped:
        selected, dropped = _select(freshness, candidates, memory_counts, budget,
                                    _widest_footer(budget, candidates))
    truncation = {"applied": bool(dropped), "budget": budget,
                  "within_budget": True, "dropped": dropped}
    text = _render(freshness, selected, memory_counts, truncation)
    token_estimate = (len(text) + 3) // 4
    # Every fact can be declined and the brief still costs what its own header
    # and its report of the omissions cost. A budget under that floor is
    # unmeetable, and saying so beats implying it was met. Measured before the
    # line is added, because a line that only appears when already over budget
    # cannot be what pushed it over.
    truncation["within_budget"] = token_estimate <= budget
    if not truncation["within_budget"]:
        text = _render(freshness, selected, memory_counts, truncation)
        token_estimate = (len(text) + 3) // 4
    return {
        "status": "ok",
        "budget": budget,
        "token_estimate": token_estimate,
        "token_heuristic": "ceil(characters / 4)",
        "truncation": truncation,
        "freshness": freshness_gate,
        "staleness": freshness,
        "memory_verification": memory_counts,
        "sections": selected,
        "text": text,
    }

"""Git-diff-aware, source-grounded change context for agents."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .config import RepoBrainConfig
from .freshness import ensure_fresh
from .graph.queries import historical_co_change, impact_analysis
from .graph.schema import file_node_id
from .history import history_serveable, history_status_message
from .graph.store import GraphStore
from .indexing.doc_references import MarkdownMentionReconciler
from .indexing.scanner import detect_language
from .parsers.base import default_registry

#: Approximate token ceiling for a change context, on ``brief``'s deterministic
#: ceil(chars / 4) heuristic. Chosen so an ordinary working diff is never
#: touched while the pathological case stays inside what a session can spend:
#: the two-commit diff that motivated this emitted ~461,000 tokens.
DEFAULT_CHANGE_BUDGET = 15_000
MINIMUM_CHANGE_BUDGET = 256

#: Lowest-priority first. Trimming starts at the top of this list, so the diff
#: itself is the last thing to go and the evidence derived from it goes first.
_TRIM_ORDER: tuple[tuple[str, str | None], ...] = (
    ("historical_impact", "items"),
    ("docs_to_review", None),
    ("impact", "low_confidence"),
    ("impact", "medium_confidence"),
    ("impact", "high_confidence"),
    ("tests_to_run", None),
    ("changes", None),
)

#: Pops between exact re-measurements while trimming. Small enough that the
#: running estimate never drifts far, large enough to keep the number of full
#: serializations proportional to the overshoot rather than to the item count.
_RESYNC_EVERY = 25

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_SYMBOL_TYPES = {
    "Function", "Method", "Class", "Variable", "TestCase", "Route", "Endpoint",
    "CLICommand", "Table", "ConfigKey", "EnvVar", "GitHubJob", "GitHubStep",
    "DockerService",
}


class GitDiffError(RuntimeError):
    """Raised when change context cannot capture a trustworthy Git diff."""


def change_context(
    root: str | Path,
    store: GraphStore,
    *,
    base: str | None = None,
    auto_index: bool = True,
    include_text: bool = True,
    budget: int | None = None,
) -> dict:
    """Capture a Git change set, repair freshness, then resolve grounded context.

    ``include_text`` renders the human report into the payload. Machine-readable
    surfaces turn it off: it restates the whole structured result in prose, so a
    caller that parses the structure pays for it twice.

    ``budget`` caps the approximate token count on ``brief``'s deterministic
    ceil(chars / 4) heuristic, trimming lowest-priority evidence first and
    always reporting what it removed. ``None`` emits everything.
    """
    if budget is not None and budget < MINIMUM_CHANGE_BUDGET:
        raise ValueError(f"budget must be at least {MINIMUM_CHANGE_BUDGET} tokens")
    root = Path(root).resolve()
    captured = capture_git_changes(root, base=base)
    freshness = ensure_fresh(root, store, auto_index=auto_index)
    if not freshness["can_query"]:
        return {
            "status": freshness["status"],
            "mode": captured["mode"],
            "base": captured["base"],
            "freshness": freshness,
        }

    changes = []
    changed_paths = {
        path
        for item in captured["changes"]
        for path in (item.get("old_path"), item.get("new_path"))
        if path
    }
    for item in captured["changes"]:
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        file_node, symbols = _map_change(store, item)
        public["file_node"] = file_node
        public["symbols"] = symbols
        public["mapping_status"] = (
            "mapped" if file_node or symbols else
            "binary_unparsed" if item["binary"] else "unmapped"
        )
        if not file_node and not symbols and item.get("special_file"):
            public["mapping_status"] = f"{item['special_file']}_unparsed"
        changes.append(public)

    reasons = _ReasonTable()
    impacts, tests, impact_unknowns = _aggregate_impacts(store, changes, reasons)
    docs = _stale_doc_candidates(store, changes, changed_paths)
    result = {
        "status": "ok",
        "mode": captured["mode"],
        "base": captured["base"],
        "head": captured["head"],
        "freshness": freshness,
        "changes": changes,
        "reasons": reasons.reasons,
        "impact": impacts,
        "historical_impact": _historical_impact(
            store, freshness, changes, mode=captured["mode"],
        ),
        "tests_to_run": tests,
        "docs_to_review": docs,
        "unknowns": impact_unknowns,
    }
    if budget is not None:
        _apply_budget(result, budget)
    if include_text:
        result["text"] = render_change_context(result)
    return result


def capture_git_changes(root: str | Path, *, base: str | None = None) -> dict:
    """Capture working-tree or branch changes without mutating Git state."""
    root = Path(root).resolve()
    _git(root, "rev-parse", "--is-inside-work-tree")
    head = _git(root, "rev-parse", "--verify", "HEAD").strip()
    if base:
        base_commit = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
        merge_base = _git(root, "merge-base", base_commit, head).strip()
        diff_args = [merge_base, "HEAD"]
        mode = "branch"
        base_label = base
        old_revision = merge_base
    else:
        diff_args = ["HEAD"]
        mode = "working"
        base_label = None
        old_revision = "HEAD"

    raw = _git_bytes(root, "diff", "--name-status", "-z", "--find-renames", *diff_args)
    changes = _parse_name_status(raw)
    changes = [item for item in changes if not _internal_change(item)]
    for item in changes:
        patch_paths = [path for path in (item.get("old_path"), item.get("new_path")) if path]
        patch = _git(root, "diff", "--unified=0", "--no-color", "--find-renames",
                     *diff_args, "--", *patch_paths)
        item["line_ranges"] = _line_ranges(patch)
        item["binary"] = "Binary files " in patch or "GIT binary patch" in patch
        if item["status"] == "deleted" and not item["binary"]:
            item["_old_content"] = _git(root, "show", f"{old_revision}:{item['old_path']}")
            item["_old_revision"] = old_revision

    if mode == "working":
        max_size = RepoBrainConfig.load(root).max_file_size_bytes
        untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
        for raw_path in sorted(path for path in untracked.split(b"\0") if path):
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if _internal_path(path):
                continue
            absolute = root / path
            special_file = "symlink" if absolute.is_symlink() else None
            if special_file:
                data = os.readlink(absolute).encode("utf-8", errors="surrogateescape")
            else:
                size = absolute.stat().st_size
                if size > max_size:
                    with absolute.open("rb") as stream:
                        data = stream.read(8192)
                    special_file = "oversized"
                else:
                    data = absolute.read_bytes()
            binary = not special_file and b"\0" in data[:8192]
            line_count = 0 if binary or special_file == "oversized" else len(
                data.decode("utf-8", errors="replace").splitlines()
            )
            changes.append({
                "status": "added", "git_status": "?", "old_path": None,
                "new_path": path, "line_ranges": {
                    "old": [], "new": ([{"start": 1, "end": max(1, line_count)}]
                                        if not binary else []),
                },
                "binary": binary, "untracked": True, "special_file": special_file,
            })

    changes.sort(key=lambda item: (item.get("new_path") or item.get("old_path") or ""))
    return {"mode": mode, "base": base_label, "head": head, "changes": changes}


def _internal_path(path: str | None) -> bool:
    return bool(path and (path == ".repobrain" or path.startswith(".repobrain/")))


def _internal_change(item: dict) -> bool:
    paths = [item.get("old_path"), item.get("new_path")]
    return all(_internal_path(path) for path in paths if path)


def _git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        errors="replace", timeout=15,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "Git command failed"
        raise GitDiffError(detail)
    return process.stdout


def _git_bytes(root: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, timeout=15,
    )
    if process.returncode != 0:
        detail = process.stderr.decode(errors="replace").strip() or "Git command failed"
        raise GitDiffError(detail)
    return process.stdout


def _parse_name_status(raw: bytes) -> list[dict]:
    tokens = [token.decode("utf-8", errors="surrogateescape")
              for token in raw.split(b"\0") if token]
    changes, index = [], 0
    while index < len(tokens):
        code = tokens[index]
        index += 1
        kind = code[0]
        old_path: str | None
        new_path: str | None
        if kind in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise GitDiffError("Malformed rename/copy record from git diff")
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
        else:
            if index >= len(tokens):
                raise GitDiffError("Malformed path record from git diff")
            path = tokens[index]
            index += 1
            old_path = path if kind == "D" else None
            new_path = None if kind == "D" else path
        status = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed",
                  "C": "copied", "T": "type_changed"}.get(kind, "unknown")
        changes.append({
            "status": status, "git_status": code, "old_path": old_path,
            "new_path": new_path, "binary": False, "untracked": False,
        })
    return changes


def _line_ranges(patch: str) -> dict:
    old, new = [], []
    for match in _HUNK.finditer(patch):
        old_start, old_count = int(match.group(1)), int(match.group(2) or 1)
        new_start, new_count = int(match.group(3)), int(match.group(4) or 1)
        if old_count:
            old.append({"start": old_start, "end": old_start + old_count - 1})
        if new_count:
            new.append({"start": new_start, "end": new_start + new_count - 1})
    return {"old": _merge_ranges(old), "new": _merge_ranges(new)}


def _merge_ranges(ranges: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for item in sorted(ranges, key=lambda value: value["start"]):
        if merged and item["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        else:
            merged.append(dict(item))
    return merged


def _map_change(store: GraphStore, change: dict) -> tuple[dict | None, list[dict]]:
    path = change.get("new_path") or change.get("old_path")
    ranges = change["line_ranges"]["new" if change.get("new_path") else "old"]
    old_content = change.get("_old_content")
    old_revision = change.get("_old_revision")
    if (
        change["status"] == "deleted"
        and isinstance(path, str)
        and isinstance(old_content, str)
        and isinstance(old_revision, str)
    ):
        nodes = _parse_deleted_nodes(path, old_content, old_revision)
    else:
        nodes = [dict(row) for row in store.conn.execute(
            "SELECT id,type,name,qualified_name,path,start_line,end_line,language "
            "FROM nodes WHERE path=? ORDER BY start_line,type,name", (path,),
        ).fetchall()]
    file_node = next((_node_record(node, change) for node in nodes if node["type"] == "File"), None)
    symbols = []
    for node in nodes:
        if node["type"] not in _SYMBOL_TYPES or node.get("start_line") is None:
            continue
        if ranges and not any(
            node["start_line"] <= span["end"]
            and (node.get("end_line") or node["start_line"]) >= span["start"]
            for span in ranges
        ):
            continue
        symbols.append(_node_record(node, change))
    return file_node, symbols


def _parse_deleted_nodes(path: str, content: str, revision: str) -> list[dict]:
    registry = default_registry()
    for parser in registry.all():
        begin = getattr(parser, "begin_run", None)
        if begin is not None:
            begin({path})
    language = detect_language(path)
    nodes = []
    for parser in registry.parsers_for(path, language):
        try:
            nodes.extend(parser.parse(path, content).nodes)
        except Exception:
            continue
    return [
        {"id": node.id, "type": str(node.type), "name": node.name,
         "qualified_name": node.qualified_name, "path": node.path,
         "start_line": node.start_line, "end_line": node.end_line,
         "language": node.language, "source_revision": revision}
        for node in nodes
    ]


def _node_record(node: dict, change: dict) -> dict:
    return {
        "id": node["id"], "type": node["type"], "name": node["name"],
        "qualified_name": node.get("qualified_name") or "", "path": node["path"],
        "start_line": node.get("start_line"), "end_line": node.get("end_line"),
        "language": node.get("language"),
        "provenance": (
            f"git:{node['source_revision']}:{node['path']}:{node.get('start_line') or 1}"
            if node.get("source_revision") else f"{node['path']}:{node.get('start_line') or 1}"
        ),
        "changed_because": {
            "status": change["status"], "old_path": change.get("old_path"),
            "new_path": change.get("new_path"),
        },
    }


def _historical_impact(
    store: GraphStore, freshness: dict, changes: list[dict], *, mode: str,
) -> dict:
    """Historically co-changed files that are NOT part of this diff.

    Like stale-doc detection, this is review evidence: recent history says
    these files usually change together with the changed set, and this diff
    leaves them untouched. Served only when extracted history matches HEAD.
    """
    history = freshness.get("history") or {"status": "unknown"}
    if not history_serveable(history):
        return {
            "status": history.get("status", "unknown"), "items": [],
            "reason": history_status_message(history),
        }
    paths = sorted({item["new_path"] for item in changes if item.get("new_path")})
    evidence = historical_co_change(store, paths, only_external=True)
    items = [
        {**item,
         "why": "Historically co-changed with this diff's files but unchanged here."}
        for item in evidence["items"] if not item["partner_changed"]
    ]
    notes = []
    if any(
        item["status"] == "deleted"
        or (mode == "working" and item["status"] == "renamed")
        for item in changes
    ):
        # graph reconciliation removed the old File node, and with it every
        # co-change edge keyed on a deleted or uncommitted pre-rename identity;
        # committed branch renames are rebuilt through history continuity
        notes.append("Historical co-change partners of deleted or uncommitted "
                     "renamed paths are unavailable after graph reconciliation.")
    return {"status": "ok", "items": items, "notes": notes,
            "explanation": evidence["explanation"],
            "provenance": evidence["provenance"]}


class _ReasonTable:
    """Intern the "why is this in the blast radius" records.

    A reason is drawn from the diff, so there are at most a few per changed
    path — but before this they were re-serialized in full at every citing
    item, which on this repository's own two-commit diff meant 3,175 copies
    across the impact set and 599 across the tests, for 525k characters. Items
    now carry indices into one table.
    """

    def __init__(self) -> None:
        self._ids: dict[tuple, int] = {}
        self.reasons: list[dict] = []

    def id_for(self, reason: dict) -> int:
        key = tuple(sorted(reason.items()))
        if key not in self._ids:
            self._ids[key] = len(self.reasons)
            self.reasons.append(reason)
        return self._ids[key]


def _aggregate_impacts(
    store: GraphStore, changes: list[dict], reasons: _ReasonTable,
) -> tuple[dict, list[dict], list[str]]:
    evidence: dict[str, dict] = {}
    tests: dict[str, dict] = {}
    unknowns = []
    for change in changes:
        targets = []
        if change.get("new_path"):
            targets.append((change["new_path"], "file"))
        targets.extend(
            (symbol["qualified_name"] or symbol["name"], "symbol")
            for symbol in change["symbols"]
        )
        if change["status"] == "deleted":
            unknowns.append(
                f"Impact relationships for deleted path {change['old_path']} are unavailable "
                "after freshness reconciliation."
            )
        for target, target_kind in targets:
            # Historical evidence is aggregated once per diff (see
            # _historical_impact), not per resolved target.
            result = impact_analysis(store, target, change_type=change["status"],
                                     include_history=False)
            if result is None:
                continue
            reason_id = reasons.id_for({
                "changed_path": change.get("new_path") or change.get("old_path"),
                "status": change["status"], "target": target, "target_kind": target_kind,
            })
            for bucket in ("high_confidence", "medium_confidence", "low_confidence"):
                for item in result[bucket]:
                    # One row per node, not per edge: the node is the fact about
                    # the blast radius and each edge is only evidence for it.
                    record = evidence.setdefault(item["node"]["id"], {
                        "node": item["node"], "confidence": item["confidence"],
                        "evidence": {}, "reason_ids": [],
                    })
                    record["confidence"] = max(record["confidence"], item["confidence"])
                    # One edge reached at two traversal depths is one edge; its
                    # confidence decays with depth, so keep the shortest path's.
                    key = (item["via"], item["evidence_path"], item["evidence_line"])
                    proof = record["evidence"].setdefault(key, {
                        "via": item["via"], "path": item["evidence_path"],
                        "line": item["evidence_line"], "confidence": item["confidence"],
                    })
                    proof["confidence"] = max(proof["confidence"], item["confidence"])
                    if reason_id not in record["reason_ids"]:
                        record["reason_ids"].append(reason_id)
            for item in result["recommended_tests"]:
                path = item["node"]["path"]
                record = tests.setdefault(path, {**item, "reason_ids": []})
                if reason_id not in record["reason_ids"]:
                    record["reason_ids"].append(reason_id)
    buckets: dict[str, list[dict]] = {
        "high_confidence": [], "medium_confidence": [], "low_confidence": [],
    }
    for item in evidence.values():
        item["evidence"] = sorted(
            item["evidence"].values(),
            key=lambda proof: (-proof["confidence"], proof["path"],
                               proof["line"] or 0, proof["via"]),
        )
        bucket = ("high_confidence" if item["confidence"] >= 0.85 else
                  "medium_confidence" if item["confidence"] >= 0.6 else "low_confidence")
        buckets[bucket].append(item)
    for values in buckets.values():
        values.sort(key=lambda item: (-item["confidence"], item["node"]["path"],
                                      item["node"]["start_line"] or 0))
    return buckets, sorted(tests.values(), key=lambda item: item["node"]["path"]), sorted(set(unknowns))


def _stale_doc_candidates(store: GraphStore, changes: list[dict], changed_paths: set[str]) -> list[dict]:
    target_reasons: dict[str, list[dict]] = {}
    for change in changes:
        reason = {"changed_path": change.get("new_path") or change.get("old_path"),
                  "status": change["status"]}
        if change.get("file_node"):
            target_reasons.setdefault(change["file_node"]["id"], []).append(reason)
        for symbol in change["symbols"]:
            target_reasons.setdefault(symbol["id"], []).append({**reason, "symbol": symbol["qualified_name"]})
    if not target_reasons:
        return []
    marks = ",".join("?" for _ in target_reasons)
    rows = store.conn.execute(
        f"""
        SELECT s.path AS doc_path,s.name AS section,s.type AS doc_type,s.start_line AS section_line,
               t.id AS target_id,t.name AS target_name,t.qualified_name AS target_qname,
               t.type AS target_type,t.path AS target_path,t.start_line AS target_line,
               e.start_line AS evidence_line,e.confidence,e.is_inferred,e.inference_reason,
               e.metadata_json
        FROM edges e JOIN nodes s ON s.id=e.source_node_id JOIN nodes t ON t.id=e.target_node_id
        WHERE e.type='MENTIONS' AND e.target_node_id IN ({marks})
        ORDER BY s.path,e.start_line,t.path,t.start_line
        """, tuple(target_reasons),
    ).fetchall()
    results: list[dict] = []
    for row in rows:
        if row["doc_path"] in changed_paths:
            continue
        results.append({
            "doc_path": row["doc_path"], "section": row["section"],
            "doc_type": row["doc_type"], "section_line": row["section_line"],
            "evidence_line": row["evidence_line"],
            "provenance": f"{row['doc_path']}:{row['evidence_line'] or row['section_line'] or 1}",
            "confidence": row["confidence"], "inferred": bool(row["is_inferred"]),
            "inference_reason": row["inference_reason"],
            "target": {"id": row["target_id"], "name": row["target_name"],
                       "qualified_name": row["target_qname"] or "", "type": row["target_type"],
                       "path": row["target_path"], "start_line": row["target_line"]},
            "changed_reasons": target_reasons[row["target_id"]],
            "why": "Potentially stale: this unchanged document mentions changed code.",
        })
    results.extend(_historical_path_doc_candidates(store, changes, changed_paths))
    deduped: dict[tuple, dict] = {}
    for item in results:
        key = (item["doc_path"], item["evidence_line"], item["target"]["id"])
        deduped.setdefault(key, item)
    return sorted(deduped.values(), key=lambda item: (
        item["doc_path"], item["evidence_line"] or 0, item["target"]["path"],
    ))


def _historical_path_doc_candidates(
    store: GraphStore, changes: list[dict], changed_paths: set[str]
) -> list[dict]:
    """Recover exact old-path doc evidence after rename/delete reconciliation.

    The live MENTIONS edge is necessarily gone once its old target disappears.
    We reuse the reconciler's exact path normalization over persisted Markdown
    reference candidates; fuzzy or ambiguous symbol recovery is never attempted.
    """
    historical = {
        change["old_path"]: change
        for change in changes
        if change.get("old_path") and change["status"] in {"deleted", "renamed"}
    }
    if not historical:
        return []
    documents = store.conn.execute(
        "SELECT path,metadata_json FROM nodes WHERE type='MarkdownDocument' ORDER BY path"
    ).fetchall()
    results = []
    for document in documents:
        if document["path"] in changed_paths:
            continue
        try:
            references = json.loads(document["metadata_json"] or "{}").get("references", [])
        except (json.JSONDecodeError, TypeError):
            references = []
        for reference in references:
            raw = str(reference.get("value") or "").strip()
            kind = str(reference.get("kind") or "")
            line = reference.get("line")
            normalized = MarkdownMentionReconciler._normalize_path(raw, document["path"])
            change = historical.get(normalized)
            if change is None:
                continue
            section = store.conn.execute(
                "SELECT name,type,start_line FROM nodes WHERE path=? "
                "AND type='MarkdownSection' AND start_line<=? AND end_line>=? "
                "ORDER BY start_line DESC LIMIT 1",
                (document["path"], line or 1, line or 1),
            ).fetchone()
            old_path = change["old_path"]
            target_id = (change.get("file_node") or {}).get("id") or file_node_id(old_path)
            reason = {"changed_path": change.get("new_path") or old_path,
                      "old_path": old_path, "status": change["status"]}
            results.append({
                "doc_path": document["path"],
                "section": section["name"] if section else document["path"],
                "doc_type": section["type"] if section else "MarkdownDocument",
                "section_line": section["start_line"] if section else 1,
                "evidence_line": line,
                "provenance": f"{document['path']}:{line or 1}",
                "confidence": 1.0 if kind == "link" else 0.95,
                "inferred": False,
                "inference_reason": "historical-exact-path-reference",
                "target": {"id": target_id, "name": old_path.rsplit("/", 1)[-1],
                           "qualified_name": old_path, "type": "File",
                           "path": old_path, "start_line": 1},
                "changed_reasons": [reason],
                "why": "Potentially stale: this unchanged document still references the "
                       "old path of renamed or deleted code.",
            })
    return results


def _payload_chars(result: dict) -> int:
    return len(json.dumps(result))


def _prune_reasons(result: dict) -> None:
    """Drop reasons nothing cites and renumber what is left.

    Trimming an item takes its citations with it, so without this a budgeted
    payload spends its allowance on attribution for facts it no longer carries.
    """
    cited = sorted(
        {index for bucket in result["impact"].values()
         for item in bucket for index in item["reason_ids"]}
        | {index for item in result["tests_to_run"] for index in item["reason_ids"]}
    )
    renumbered = {old: new for new, old in enumerate(cited)}
    result["reasons"] = [result["reasons"][index] for index in cited]
    for bucket in result["impact"].values():
        for item in bucket:
            item["reason_ids"] = [renumbered[index] for index in item["reason_ids"]]
    for item in result["tests_to_run"]:
        item["reason_ids"] = [renumbered[index] for index in item["reason_ids"]]


def _apply_budget(result: dict, budget: int) -> None:
    """Trim lowest-priority evidence until the payload fits, and report it.

    Truncation is reported rather than silent: a caller that cannot tell a
    trimmed impact set from a complete one will treat it as complete, which is
    the confidently-wrong answer the freshness gate exists to prevent.
    """
    limit = budget * 4
    dropped: dict[str, int] = {}
    # Installed before measuring so the report of the trimming is itself inside
    # the budget rather than pushing the payload back over it.
    result["budget"] = budget
    result["token_heuristic"] = "ceil(characters / 4)"
    result["token_estimate"] = budget
    result["truncation"] = {"applied": False, "budget": budget,
                            "within_budget": True, "dropped": dropped}

    remaining = _payload_chars(result)
    for key, sub in _TRIM_ORDER:
        if remaining <= limit:
            break
        container = result[key] if sub is None else result[key][sub]
        label = key if sub is None else f"{key}.{sub}"
        while container and remaining > limit:
            # Decrement by the removed item rather than re-serializing the whole
            # payload per pop, resynchronising periodically: dropping an item
            # also releases the reasons only it cited, which this estimate
            # cannot see and which is large enough to matter.
            remaining -= len(json.dumps(container.pop())) + 1
            dropped[label] = dropped.get(label, 0) + 1
            if dropped[label] % _RESYNC_EVERY == 0:
                _prune_reasons(result)
                remaining = _payload_chars(result)
        _prune_reasons(result)
        remaining = _payload_chars(result)
    while _payload_chars(result) > limit:
        for key, sub in _TRIM_ORDER:
            container = result[key] if sub is None else result[key][sub]
            if container:
                container.pop()
                label = key if sub is None else f"{key}.{sub}"
                dropped[label] = dropped.get(label, 0) + 1
                break
        else:
            break
        _prune_reasons(result)
    result["truncation"]["applied"] = bool(dropped)
    result["token_estimate"] = (_payload_chars(result) + 3) // 4
    # Every trimmable list can be emptied and the payload still costs what its
    # own scaffolding costs — status, head, the freshness envelope. A budget
    # under that floor is unmeetable, and saying so beats implying it was met.
    result["truncation"]["within_budget"] = result["token_estimate"] <= budget


def render_change_context(result: dict) -> str:
    lines = ["RepoBrain change context", f"Mode: {result['mode']}"]
    if result.get("base"):
        lines.append(f"Base: {result['base']}")
    lines.append("\nChanged files")
    for change in result["changes"]:
        path = change.get("new_path") or change.get("old_path")
        rename = f" (from {change['old_path']})" if change["status"] == "renamed" else ""
        lines.append(f"- {change['status']}: {path}{rename}")
        for symbol in change["symbols"]:
            lines.append(f"  - {symbol['type']} {symbol['qualified_name'] or symbol['name']} "
                         f"({symbol['provenance']})")
    lines.append("\nLikely impact")
    for bucket in ("high_confidence", "medium_confidence", "low_confidence"):
        for item in result["impact"][bucket]:
            node = item["node"]
            for proof in item["evidence"]:
                lines.append(f"- {bucket}: {node['name']} via {proof['via']} "
                             f"({proof['path']}:{proof['line'] or 1}, "
                             f"confidence={proof['confidence']:.2f})")
    lines.append("\nHistorical co-change (heuristic)")
    historical = result["historical_impact"]
    if historical["status"] != "ok":
        lines.append(f"- Unavailable: {historical.get('reason', historical['status'])}")
    elif historical["items"]:
        for item in historical["items"]:
            commits = ", ".join(sha[:10] for sha in item["supporting_commits"][:3])
            lines.append(
                f"- {item['node']['path']} co-changed with {item['co_changed_with']} "
                f"(support={item['support']}, score={item['score']:.2f}, "
                f"commits: {commits}) — unchanged in this diff"
            )
    else:
        lines.append("- None identified.")
    for note in historical.get("notes", []):
        lines.append(f"- Note: {note}")
    lines.append("\nTests to run")
    lines.extend(f"- {item['node']['path']}" for item in result["tests_to_run"])
    if not result["tests_to_run"]:
        lines.append("- None identified.")
    lines.append("\nDocs to review")
    lines.extend(f"- {item['doc_path']} · {item['section']} ({item['provenance']}, "
                 f"confidence={item['confidence']:.2f})" for item in result["docs_to_review"])
    if not result["docs_to_review"]:
        lines.append("- None identified.")
    if result["unknowns"]:
        lines.append("\nUnknowns")
        lines.extend(f"- {item}" for item in result["unknowns"])
    truncation = result.get("truncation") or {}
    if truncation.get("applied"):
        lines.append(f"\nTruncated to fit {truncation['budget']} tokens")
        lines.extend(f"- {label}: {count} item(s) not shown"
                     for label, count in sorted(truncation["dropped"].items()))
        if not truncation.get("within_budget", True):
            lines.append(f"- Still over budget at {result['token_estimate']} tokens; "
                         "nothing further can be dropped.")
    return "\n".join(lines) + "\n"

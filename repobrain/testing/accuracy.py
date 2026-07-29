"""Labeled extraction evaluation for deterministic graph facts.

Scores are properties of the committed corpus, not of the extractor in
general (D38): this is a regression gate, and a figure taken from it must be
quoted with the corpus it was measured over.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Iterable

from ..graph.store import GraphStore
from ..indexing.indexer import Indexer


def _identity(type_: str, qualified_name: str, name: str, path: str) -> str:
    return f"{type_}:{qualified_name or name}@{path}"


def collect_fact_keys(store: GraphStore) -> set[str]:
    """Return stable, human-authorable keys for every persisted node and edge."""
    facts = {
        f"node|{_identity(row['type'], row['qualified_name'], row['name'], row['path'])}"
        for row in store.conn.execute(
            "SELECT type, qualified_name, name, path FROM nodes"
        )
    }
    for row in store.conn.execute(
        """
        SELECT
            e.type,
            e.path,
            source.type AS source_type,
            source.qualified_name AS source_qualified_name,
            source.name AS source_name,
            source.path AS source_path,
            target.type AS target_type,
            target.qualified_name AS target_qualified_name,
            target.name AS target_name,
            target.path AS target_path
        FROM edges e
        JOIN nodes source ON source.id = e.source_node_id
        JOIN nodes target ON target.id = e.target_node_id
        """
    ):
        source = _identity(
            row["source_type"],
            row["source_qualified_name"],
            row["source_name"],
            row["source_path"],
        )
        target = _identity(
            row["target_type"],
            row["target_qualified_name"],
            row["target_name"],
            row["target_path"],
        )
        facts.add(f"edge|{row['type']}|{source}|{target}|{row['path']}")
    return facts


@dataclass(frozen=True)
class AccuracyResult:
    expected_count: int
    forbidden_count: int
    true_positives: int
    false_negatives: tuple[str, ...]
    false_positives: tuple[str, ...]

    @property
    def precision(self) -> float:
        denominator = self.true_positives + len(self.false_positives)
        return self.true_positives / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.expected_count if self.expected_count else 1.0

    @property
    def passed(self) -> bool:
        return not self.false_negatives and not self.false_positives

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "expected_count": self.expected_count,
            "forbidden_count": self.forbidden_count,
            "true_positives": self.true_positives,
            "false_negatives": list(self.false_negatives),
            "false_positives": list(self.false_positives),
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
        }


def evaluate_facts(
    store: GraphStore,
    *,
    expected: Iterable[str],
    forbidden: Iterable[str],
) -> AccuracyResult:
    """Score labeled positive and negative facts against one indexed graph."""
    actual = collect_fact_keys(store)
    expected_set = set(expected)
    forbidden_set = set(forbidden)
    overlap = expected_set & forbidden_set
    if overlap:
        raise ValueError(
            "accuracy specification labels facts as both expected and forbidden: "
            + ", ".join(sorted(overlap))
        )
    missing = tuple(sorted(expected_set - actual))
    unexpected = tuple(sorted(forbidden_set & actual))
    return AccuracyResult(
        expected_count=len(expected_set),
        forbidden_count=len(forbidden_set),
        true_positives=len(expected_set) - len(missing),
        false_negatives=missing,
        false_positives=unexpected,
    )


def load_specification(path: str | Path) -> tuple[list[str], list[str]]:
    """Read and validate an expected/forbidden fact specification."""
    specification = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(specification, dict):
        raise ValueError(f"accuracy specification must be a JSON object: {path}")
    labels = []
    for field in ("expected", "forbidden"):
        facts = specification.get(field, [])
        if not isinstance(facts, list) or not all(isinstance(x, str) for x in facts):
            raise ValueError(
                f"accuracy specification '{field}' must be an array of strings: {path}"
            )
        labels.append(facts)
    return labels[0], labels[1]


def evaluate_repository(
    repository: str | Path,
    *,
    expected: Iterable[str],
    forbidden: Iterable[str],
) -> tuple[AccuracyResult, list[str]]:
    """Index a repository from scratch and score it; also return its warnings."""
    with tempfile.TemporaryDirectory(prefix="repobrain-accuracy-") as temp_dir:
        with GraphStore(Path(temp_dir) / "repobrain.sqlite") as store:
            stats = Indexer(store).index(Path(repository).resolve(), incremental=False)
            result = evaluate_facts(store, expected=expected, forbidden=forbidden)
    return result, list(stats.warnings)


def evaluate_corpus(manifest_path: str | Path) -> dict:
    """Score every entry of a labeled corpus and aggregate the totals.

    Entry ``repository`` and ``spec`` paths resolve relative to the manifest,
    so a corpus can be moved without rewriting every entry.
    """
    manifest = Path(manifest_path).resolve()
    entries = json.loads(manifest.read_text(encoding="utf-8")).get("entries", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"corpus manifest must list entries: {manifest}")

    base = manifest.parent
    scored: list[dict] = []
    totals: dict[str, float] = {
        "expected_count": 0, "forbidden_count": 0, "true_positives": 0,
        "false_positives": 0,
    }
    for entry in entries:
        expected, forbidden = load_specification(base / entry["spec"])
        result, warnings = evaluate_repository(
            base / entry["repository"], expected=expected, forbidden=forbidden
        )
        payload = result.to_dict()
        payload["name"] = entry.get("name", entry["repository"])
        payload["languages"] = entry.get("languages", [])
        payload["warnings"] = warnings
        # Extraction warnings mean the indexer could not read part of the
        # corpus; a score computed over a partial index is not a score.
        payload["passed"] = payload["passed"] and not warnings
        scored.append(payload)
        totals["expected_count"] += result.expected_count
        totals["forbidden_count"] += result.forbidden_count
        totals["true_positives"] += result.true_positives
        totals["false_positives"] += len(result.false_positives)

    retrieved = totals["true_positives"] + totals["false_positives"]
    totals["precision"] = round(
        totals["true_positives"] / retrieved if retrieved else 1.0, 6
    )
    totals["recall"] = round(
        totals["true_positives"] / totals["expected_count"]
        if totals["expected_count"]
        else 1.0,
        6,
    )
    return {
        "passed": all(entry["passed"] for entry in scored),
        "entries": scored,
        "totals": totals,
    }

"""Labeled extraction evaluation for deterministic graph facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..graph.store import GraphStore


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

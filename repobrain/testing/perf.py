"""Lightweight, dependency-free instrumentation for scale profiling/tests.

Wraps `sqlite3.Connection.set_trace_callback` to count executed statements
(a hardware-independent proxy for "how much SQL work happened") and a small
wall-clock timer for human-readable benchmark reports. Test assertions
should prefer the query/row counters; wall-clock numbers are for the
benchmark script's printed report, not narrow pass/fail thresholds.
"""
from __future__ import annotations

import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class QueryStats:
    total: int = 0
    by_verb: Counter = field(default_factory=Counter)

    def record(self, sql: str) -> None:
        self.total += 1
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else "?"
        self.by_verb[verb] += 1


@contextmanager
def count_queries(conn: sqlite3.Connection):
    """Yield a :class:`QueryStats` populated with every statement `conn` runs
    for the duration of the ``with`` block (including ones run via
    ``executemany``, which SQLite traces once per row)."""
    stats = QueryStats()
    conn.set_trace_callback(stats.record)
    try:
        yield stats
    finally:
        conn.set_trace_callback(None)


@dataclass
class Timing:
    label: str
    seconds: float


@contextmanager
def timer(label: str, sink: list[Timing]):
    start = time.perf_counter()
    try:
        yield
    finally:
        sink.append(Timing(label, time.perf_counter() - start))

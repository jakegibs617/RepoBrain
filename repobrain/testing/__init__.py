"""Deterministic large-repository fixtures and lightweight perf instrumentation.

Nothing here is imported by the shipped `repobrain` runtime code paths; it
exists to generate scale-benchmark corpora (never committed as generated
files) and to measure indexing/query work for the scale-hardening tests and
`scripts/benchmark_scale.py`.
"""

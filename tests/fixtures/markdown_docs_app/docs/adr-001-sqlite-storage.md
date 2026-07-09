# ADR-001: Use SQLite for storage

## Status

Accepted

## Context

The app needs durable storage without operational overhead. Candidates were
PostgreSQL, SQLite, and a JSON file on disk. The service is single-node and
read-heavy.

## Decision

Use SQLite. The connection string is provided by the `DATABASE_URL`
environment variable, read in `app/db/config.py`.

## Consequences

- No database server to operate.
- Concurrent writes are limited; acceptable for current load.
- Migration to PostgreSQL later only requires changing `DATABASE_URL`.

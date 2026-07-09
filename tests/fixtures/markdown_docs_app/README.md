# Markdown Docs App

A documentation-heavy fixture project for RepoBrain's Markdown pipeline.

## Overview

This project documents a small user API. See the
[architecture decision record](docs/adr-001-sqlite-storage.md) and the
[implementation plan](docs/implementation-plan.md).

## Database

All persistent state lives in a single SQLite database. The connection string
comes from the `DATABASE_URL` config key. The database schema is described in
the ADR.

## Routes

- `POST /api/users` creates a user.
- `GET /api/users/<id>` fetches a user.

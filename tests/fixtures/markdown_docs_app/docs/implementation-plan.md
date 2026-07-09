# Implementation Plan

## Phase 1: User API

Implement the `POST /api/users` route in `app/api/routes.py`, delegating to
`app/services/user_service.py`. See the [ADR](adr-001-sqlite-storage.md) for
storage rationale.

```python
@app.route("/api/users", methods=["POST"])
def create_user_route():
    return handle_create_user()
```

Remaining work:

- TODO: add input validation for the create user payload
- TODO: return 404 from `GET /api/users/<id>` when the user is missing
- FIXME: `DATABASE_URL` default should not point at a production path

## Phase 2: Hardening

Add rate limiting and structured logging.

```bash
export DATABASE_URL=sqlite:///local.db
pytest tests/
```

- [ ] TODO: wire up CI to run the test suite
- Write the deployment guide

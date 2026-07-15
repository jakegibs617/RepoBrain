# Small Python App

A tiny flask-style user API used as a RepoBrain test fixture.

## Architecture

The `POST /api/users` route is declared in `app/api/routes.py`. It delegates to
the handler in `app/handlers/user_handler.py`, which calls
`app/services/user_service.py` via `create_user`, which persists through
`app/repositories/user_repository.py`.

## Database

The database connection is configured in `app/db/config.py`, which reads the
`DATABASE_URL` environment variable. Changing `DATABASE_URL` affects every
database-backed route.

`app/models/user.py` and
`app/repositories/sqlalchemy_user_repository.py` provide deterministic
SQLAlchemy-style syntax coverage: a literal `User.__tablename__` plus exact
`session.get(User, ...)` and `session.add(User(...))` model operations. They
are indexing fixtures and require no SQLAlchemy runtime.

## Testing

Run the tests in `tests/test_users.py` with pytest.

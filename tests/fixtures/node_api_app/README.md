# Node API App

A small Express-style user API used as a RepoBrain test fixture.

- `src/server.js` boots the app and mounts the user routes.
- `src/routes/users.js` defines `POST /api/users` and `GET /api/users/:id`.
- `src/services/userService.js` holds the business logic.
- `src/config.js` reads `PORT`, `DATABASE_URL`, and `LOG_LEVEL` from the
  environment (see `.env.example`).

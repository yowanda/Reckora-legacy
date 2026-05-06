# cacadoxx-backend

FastAPI service that gates a username footprint scanner behind a
members-only login. Scans 500+ public sites and streams results back over
Server-Sent Events.

## Local dev

```bash
uv sync

# REQUIRED on first boot — seeds the admin account in SQLite.
export CACADOXX_ADMIN_USERNAME=adminganteng
export CACADOXX_ADMIN_PASSWORD=adminadmin

uv run uvicorn app.main:app --reload
```

Then `curl http://localhost:8000/api/health`. The first time the server
boots it creates `var/cacadoxx.db` (SQLite) and seeds the admin from the
environment variables above. Subsequent boots reuse the existing DB.

## Endpoints

Public:

- `GET  /api/health` — liveness + version + site count
- `POST /api/auth/login` — `{username, password}` → sets `cacadoxx_session` cookie
- `POST /api/auth/logout` — clears the session cookie

Member+:

- `GET  /api/auth/me` — current member
- `GET  /api/sites` — list of sites + tag catalog
- `POST /api/search` — `{username, tags, limit}` → SSE stream of results

Admin only:

- `GET    /api/admin/members` — list members
- `POST   /api/admin/members` — `{username, password, role}` create member
- `PATCH  /api/admin/members/{username}` — `{role?, password?}` update / reset password
- `DELETE /api/admin/members/{username}` — delete member

## Auth implementation

- **Hashing:** `pbkdf2_sha256`, 200k iterations, 16-byte salt (stdlib).
- **Sessions:** HMAC-SHA256-signed cookie, 7-day expiry. Cookie is
  `HttpOnly`, `SameSite=lax`, `Secure`. Non-browser clients can use
  `Authorization: Bearer <token>` instead — login also returns the token
  in the JSON body for that purpose.
- **Storage:** SQLite at `CACADOXX_DB_PATH` (default `backend/var/cacadoxx.db`).
- **Seed admin:** never overwrites an existing user; only inserts on first
  boot when the DB is empty for that username.

See `app/auth.py` for password hashing, session signing, and the SQLite
schema. Per-site OSINT check logic lives in `app/checker.py`. Site
definitions come from [maigret](https://github.com/soxoj/maigret) (MIT)
and are curated into `app/data/sites.json` via `scripts/build_sites.py`.

# Cacadoxx

> Trace any username across the open web. Concurrent OSINT lookup over 500+
> public sites with a streaming API and a modern UI — gated behind a
> members-only login that admins fully control.

Cacadoxx is a small, modern web app inspired by
[maigret](https://github.com/soxoj/maigret). It checks whether a given
username has a public profile on hundreds of sites and streams the results
back to your browser as they arrive.

It does **not** bypass authentication, scrape private content, or attempt
credential discovery. Use it only for legitimate self-research, security
work, or journalism.

## What's inside

```
backend/   FastAPI: auth, member management, site catalog, async checker, SSE streaming
frontend/  Vite + React + Tailwind UI: login page, search view, admin panel
data/      Curated site database derived from upstream maigret (MIT)
```

- **Auth & admin** — `backend/app/auth.py`, `backend/app/main.py`
- **Search engine** — `backend/app/checker.py`, `backend/app/sites.py`
- **Frontend** — `frontend/src/App.tsx`, `frontend/src/components/{LoginPage,AdminPanel}.tsx`
- **Site DB** — `backend/app/data/sites.json` (built by `backend/scripts/build_sites.py`)

## Auth model (TL;DR)

- Cacadoxx is **members-only**. There is no public sign-up.
- A seed admin is created from environment variables on first boot
  (`CACADOXX_ADMIN_USERNAME` / `CACADOXX_ADMIN_PASSWORD`).
- **Admins** can log in, run searches, and manage other members
  (add / delete / change role / reset password).
- **Members** can log in and run searches — that's it. Members **cannot**
  change their own password; only an admin can reset it.
- Sessions are signed-cookie based (HMAC-SHA256, 7-day expiry, `HttpOnly`,
  `SameSite=lax`, `Secure`). Passwords are stored as
  `pbkdf2_sha256` hashes (200k iterations, 16-byte salt).

## Quick start

```bash
git clone https://github.com/yowanda/Cacadoxx
cd Cacadoxx
```

Backend:

```bash
cd backend
uv sync                                 # or: pip install -e ".[dev]"

# Required for first boot — seeds the admin account.
export CACADOXX_ADMIN_USERNAME=adminganteng
export CACADOXX_ADMIN_PASSWORD=adminadmin

uv run uvicorn app.main:app --reload    # http://localhost:8000
```

Frontend:

```bash
cd frontend
pnpm install
pnpm dev                                # http://localhost:5173
```

Vite proxies `/api` to `http://127.0.0.1:8000`. Open http://localhost:5173,
log in with the admin credentials you exported, then add members from the
**Admin** tab.

## Single-origin production layout

For deployment you can build the frontend and let the backend serve it,
giving you a single FastAPI process and no CORS:

```bash
# 1) Build frontend
cd frontend && pnpm install && pnpm build

# 2) Drop dist/ into the backend's static dir
rm -rf ../backend/app/static
mkdir -p ../backend/app/static
cp -r dist/* ../backend/app/static/

# 3) Run the backend — it auto-mounts /assets and serves /, /login, etc.
cd ../backend
CACADOXX_ADMIN_USERNAME=adminganteng \
CACADOXX_ADMIN_PASSWORD=adminadmin \
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`backend/Dockerfile` and `fly.toml` are pre-configured for
[Fly.io](https://fly.io); just run `fly deploy` after setting the admin
secrets via `fly secrets set CACADOXX_ADMIN_USERNAME=… CACADOXX_ADMIN_PASSWORD=…`.

## API

| Method | Path                                  | Auth     | Description                                              |
| ------ | ------------------------------------- | -------- | -------------------------------------------------------- |
| GET    | `/api/health`                         | public   | Liveness + version + site count                          |
| POST   | `/api/auth/login`                     | public   | `{username, password}` → set cookie                      |
| POST   | `/api/auth/logout`                    | public   | Clear session cookie                                     |
| GET    | `/api/auth/me`                        | member+  | Current logged-in member                                 |
| GET    | `/api/sites`                          | member+  | Site catalog and tag list                                |
| POST   | `/api/search`                         | member+  | SSE stream of per-site check results                     |
| GET    | `/api/admin/members`                  | admin    | List all members                                         |
| POST   | `/api/admin/members`                  | admin    | `{username, password, role}` — create a member           |
| PATCH  | `/api/admin/members/{username}`       | admin    | `{role?, password?}` — update role and/or reset password |
| DELETE | `/api/admin/members/{username}`       | admin    | Delete a member (cannot delete yourself or last admin)   |

`POST /api/search` body:

```json
{ "username": "torvalds", "tags": ["coding"], "limit": 200 }
```

Each SSE event has the shape `event: result\ndata: {...}`. The terminal
event is `event: done`. Per-IP rate limits: **20 req/min** for `/api/search`,
**8 req/min** for `/api/auth/login`.

`POST /api/auth/login` also returns the session as a `token` field in the
response body, so non-browser clients (curl, scripts) can authenticate via
`Authorization: Bearer <token>` instead of cookies.

## Tests & linting

```bash
cd backend  && uv run ruff check . && uv run pytest
cd frontend && pnpm lint && pnpm tsc --noEmit && pnpm build
```

CI runs both on every push (`.github/workflows/ci.yml`).

## Credits

- Site definitions are derived from
  [soxoj/maigret](https://github.com/soxoj/maigret) (MIT). All upstream
  authors retain copyright over their respective contributions.
- UI / product credit: **cacapaspas**.

## License

[MIT](LICENSE)

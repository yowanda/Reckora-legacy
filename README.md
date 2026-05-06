---
title: Reckora
emoji: 🎯
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: Members-only OSINT username footprint scanner
license: mit
---

# Reckora

> Recon any username across the open web. Concurrent OSINT lookup over
> 2,500+ public sites with a streaming API and a modern UI — gated behind a
> members-only login that admins fully control.

🌐 **Live demo**: <https://yesmyyt-cacadoxx.hf.space>

Reckora is a small, modern web app inspired by
[maigret](https://github.com/soxoj/maigret). It checks whether a given
username has a public profile on hundreds of sites and streams the results
back to your browser as they arrive.

It does **not** bypass authentication, scrape private content, or attempt
credential discovery. Use it only for legitimate self-research, security
work, or journalism.

## Progress / changelog

Reckora is being upgraded in 4 phases to surpass maigret on the dimensions
its users care about most. Each phase ships as one commit, gets verified
against live HF Space, and is benchmarked here with concrete numbers.

| Phase | Status | Site count | Confidence | BLOCKED detection | 2-stage verify | Cookie prewarm | Chrome-TLS rescue | Recursive extract | ML classifier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 — coverage & precision wins | shipped (ed584e4) | **2,506** ↑ from 555 | 0–100 score | yes | — | — | — | — | — |
| 2 — anti-bot & verify | shipped (bd7b768) | 2,506 | 0–100 | yes | yes (Firefox UA + rotated referrer) | yes (~928 sites) | yes (curl-cffi chrome131) | — | — |
| 3 — extraction & graph | shipped | 2,506 | 0–100 | yes | yes | yes | yes | **yes (socid_extractor + react-flow)** | — |
| 4 — beyond maigret | planned | 2,506+ | 0–100 + ML | yes | yes | yes | yes | yes | yes (ONNX) + admin live DB editor + scan-history diff |

**Phase 1 benchmark — 100-site synthetic scan, no caching, prod HF Space.**

| Metric | Pre-phase-1 | Phase 1 | Δ |
| --- | --- | --- | --- |
| Active sites | 555 | **2,506** | +351 % |
| 100-site scan p50 | n/a | **28.6 s** | baseline |
| 100-site scan p95 | n/a | **30.5 s** | baseline |
| SSE payload / 100 sites | n/a | **30 KB** | baseline |
| Confidence p50 (per result) | n/a (binary) | **92** | full granularity |
| BLOCKED hits / 100 (Cloudflare/WAF/429) | melted into ERROR | **11** | finally distinguishable from ERROR |
| Smart-retry recoveries / 100 | 0 (no retry loop) | **2** | recall ↑ on transient 5xx |

**Phase 2 benchmark — same 100-site scan, all phase-2 features enabled.**

| Metric | Phase 1 | Phase 2 | Δ |
| --- | --- | --- | --- |
| 100-site scan total wall | 28.6 s | **26.3 s** | −8 % (faster despite extra probes — most verify probes ride the warm pool) |
| Per-site probe p50 | n/a | **690 ms** | new |
| Per-site probe p95 | n/a | **1,947 ms** | new |
| Auto-verified CLAIMED hits / 100 | 0 | **5** | second probe with rotated UA agreed |
| Verify-disagreement demotions / 100 | 0 | **0–2** | borderline CLAIMED → UNKNOWN, false positives suppressed |
| BLOCKED → CLAIMED/FREE rescued (curl-cffi) | 0 | **2 / 50** | Flickr family rescued from 429 via Chrome TLS impersonation |
| Cookie-jar prewarm hosts | 0 | **~928** sites flagged | XenForo / vBulletin / uCoz / Discourse engines auto-tagged |
| SSE payload / 100 sites | 30 KB | **32 KB** | +7 % (verify + cffi-rescue signals + ``verified`` field) |

**Phase 3 benchmark — same 100-site scan; primary mode, then ``/api/search/graph`` with depth-1 recursion.**

| Metric | Phase 2 | Phase 3 (list) | Phase 3 (graph) | Δ vs phase 2 |
| --- | --- | --- | --- | --- |
| 100-site scan total wall (primary only) | 26.3 s | **25.8 s** | 25.8 s | −2 % (extraction is sync on already-fetched body, no extra I/O) |
| CLAIMED hits / 100 (real username "torvalds") | n/a | **28** | 28 | comparable to phase 2 |
| **CLAIMED with extracted identifiers** | 0 | **17 / 28 (61 %)** | 17 / 28 (61 %) | brand-new signal: real names, bios, alt-platform links, emails |
| Identifier fields per CLAIMED extract (median) | 0 | **4** | 4 | e.g. ``fullname`` + ``bio`` + ``profile_url`` + ``avatar_url`` |
| Graph mode total wall (depth-1, "soxoj") | n/a | n/a | **51.9 s** | 1× primary + 1× sub-scan for the discovered alt |
| **Secondary usernames discovered (depth-1)** | n/a | n/a | **1 unique** ("sox0j") | recursion correctly surfaces typo-variant aliases without manual hinting |
| Recursion edges emitted | n/a | n/a | **1** | parent → child link with evidence signal (``link:mastodon`` etc.) |
| SSE payload / 100 sites (list mode) | 32 KB | **35 KB** | 35 KB | +9 % (extracted dict per CLAIMED) |
| SSE payload / graph scan with 1 sub-scan | n/a | n/a | **81 KB** | primary 35 KB + extracted events 1 KB + secondary 35 KB + chrome 10 KB |
| Recursion safety caps | n/a | n/a | **depth ≤ 1, max 5 secondary per parent, max 8 seed hits** | bounded fanout; sub-scans skip extraction to keep payload small |

Maigret comparison ([details](./comparison-maigret.md)): Reckora's catalog
is now within 20 % of maigret's 3,154 (we still drop maigret's 644
explicitly-disabled entries by default), but emits **richer per-result
telemetry**: confidence score, signals array, BLOCKED status, retry
count, **verified** flag, **cffi-rescue** signal — none of which maigret
exposes today. Maigret has a ``cloudscraper`` fallback for Cloudflare
but not the Chrome-TLS impersonation curl-cffi gives us, and it has no
2-stage verification at all.

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

- Reckora is **members-only**. There is no public sign-up.
- A seed admin is created from environment variables on first boot
  (`RECKORA_ADMIN_USERNAME` / `RECKORA_ADMIN_PASSWORD`).
- **Admins** can log in, run searches, and manage other members
  (add / delete / change role / reset password).
- **Members** can log in and run searches — that's it. Members **cannot**
  change their own password; only an admin can reset it.
- Sessions are signed-cookie based (HMAC-SHA256, 7-day expiry, `HttpOnly`,
  `SameSite=lax`, `Secure`). Passwords are stored as
  `pbkdf2_sha256` hashes (200k iterations, 16-byte salt).

## Quick start

```bash
git clone https://github.com/yowanda/Reckora
cd Reckora
```

Backend:

```bash
cd backend
uv sync                                 # or: pip install -e ".[dev]"

# Required for first boot — seeds the admin account.
export RECKORA_ADMIN_USERNAME=adminganteng
export RECKORA_ADMIN_PASSWORD=adminadmin

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
RECKORA_ADMIN_USERNAME=adminganteng \
RECKORA_ADMIN_PASSWORD=adminadmin \
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Deploy

Two paths. **Hugging Face Spaces** is recommended because it is genuinely
free, doesn't ask for a credit card, and supports raw Docker so you can
ship the existing `backend/Dockerfile` as-is.

#### Hugging Face Spaces (free, no credit card)

The repo ships with a top-level `Dockerfile` that does the whole build
(frontend → static bundle → backend image, single origin) so the Space
can use **Docker SDK** with no extra config.

1. Sign up at <https://huggingface.co/join>.
2. Create a Space at <https://huggingface.co/new-space>:
   - **Space SDK**: **Docker** → blank template.
   - **Visibility**: public (the auth gate is in-app).
3. Add Space secrets under *Settings → Variables and secrets*:
   - `RECKORA_ADMIN_USERNAME`
   - `RECKORA_ADMIN_PASSWORD`
   - (optional) `RECKORA_SESSION_SECRET` — any 32+ char random string.
4. Mirror this repo's contents into the Space's git
   (the Space build picks up the top-level `Dockerfile` automatically):
   ```bash
   git clone https://huggingface.co/spaces/<user>/<space-name> hf-space
   cp -r Reckora/* hf-space/
   cd hf-space && git add -A && git commit -m "deploy" && git push
   ```
5. The Space builds the image and serves the app at
   `https://<user>-<space-name>.hf.space`. Sign in with the seed admin
   credentials, then create the rest of your members from the
   **Admin** tab.

#### Fly.io (paid as of late 2024)

`backend/Dockerfile` and `fly.toml` are pre-configured. Note that Fly now
requires a payment method on file even for the small free allowance.

```bash
fly auth login
fly secrets set RECKORA_ADMIN_USERNAME=adminganteng \
                RECKORA_ADMIN_PASSWORD=<strong-password>
fly deploy
```

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

### What's covered

- **Backend (27 pytest cases)** — health endpoint, auth gate on
  `/api/sites` and `/api/search`, login (success / wrong password /
  rate-limit), `/api/auth/me` round-trip, admin CRUD on
  `/api/admin/members`, role-gating (member → 403), self-delete and
  last-admin guards.
- **Frontend** — ESLint + `tsc --noEmit` + production `vite build`.

### End-to-end verified flows

The login + admin lifecycle has been exercised manually against a built
single-origin server. All assertions below passed:

1. Anonymous load forces the login screen; `/api/sites` returns `401`.
2. Wrong password → exact text `Invalid username or password.` (no info
   leakage on whether the user exists).
3. Admin login lands on the search view with the **Admin** tab visible
   and the seed admin row marked `(you)`.
4. Adding a member from the Admin tab refreshes the table immediately
   with the new role badge and "Reset password" / "Delete" actions.
5. The new member can log in, **does not** see the Admin tab, and gets
   `403 admin role required` on `GET /api/admin/members` and on
   `PATCH /api/admin/members/<self>` (so a member cannot self-reset
   their own password — admin-only by design).
6. The member can run a streaming search (250 sites checked, dozens of
   claimed accounts on a real public username).
7. After an admin password reset, the old password is rejected and the
   new one works.
8. Admin delete removes the row immediately. `DELETE` against the
   currently-signed-in admin returns `400 cannot delete the
   currently signed-in admin`; demoting the only admin returns
   `400 cannot demote the last admin`.

## Migrating from `Cacadoxx` (env vars)

The `CACADOXX_*` environment-variable prefix has been renamed to
`RECKORA_*`. Operators upgrading an existing deployment must rename:

| Old                          | New                          |
| ---------------------------- | ---------------------------- |
| `CACADOXX_ADMIN_USERNAME`    | `RECKORA_ADMIN_USERNAME`     |
| `CACADOXX_ADMIN_PASSWORD`    | `RECKORA_ADMIN_PASSWORD`     |
| `CACADOXX_SESSION_SECRET`    | `RECKORA_SESSION_SECRET`     |
| `CACADOXX_DB_PATH`           | `RECKORA_DB_PATH`            |
| `CACADOXX_CORS_ORIGINS`      | `RECKORA_CORS_ORIGINS`       |
| `CACADOXX_LOGIN_RATE_LIMIT`  | `RECKORA_LOGIN_RATE_LIMIT`   |
| `CACADOXX_SEARCH_RATE_LIMIT` | `RECKORA_SEARCH_RATE_LIMIT`  |

Existing SQLite data is unaffected if you point `RECKORA_DB_PATH` at the
old `cacadoxx.db` file. The session cookie name has changed from
`cacadoxx_session` to `reckora_session`, so members will need to sign in
again after the upgrade.

## Credits

- Site definitions are derived from
  [soxoj/maigret](https://github.com/soxoj/maigret) (MIT). All upstream
  authors retain copyright over their respective contributions.
- UI / product credit: **cacapaspas**.

## License

[MIT](LICENSE)

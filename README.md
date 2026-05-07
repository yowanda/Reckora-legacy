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
| 4 — beyond maigret | shipped | 2,506+ | 0–100 + ML | yes | yes | yes | yes | yes | **yes (ONNX) + admin live DB editor + scan-history diff** |
| 5 — operator polish | shipped (8/8: 5a–5h incl. 5h-A–E + 5h-FE + 5h-deploy) | 2,506+ | 0–100 + ML | yes | yes | yes | yes | yes (depth admin-tunable) | yes |
| 6 — accuracy & gap closure vs maigret | shipped (4/4: 6a, 6b, 6c, 6d) | **2,506** (regen from upstream) | 0–100 + ML | yes | yes + engine-aware probes | yes | yes | yes | yes |
| 7 — intel & enrichment | shipped 4/4 (**7a** pHash + **7b** WHOIS/HIBP + **7c** reverse-image + **7d** ML retrain + drift) | 2,506+ | 0–100 + ML retrainable | yes | yes | yes | yes | yes + pHash image stitching | yes + WHOIS/HIBP/reverse-image/ML drift |
| 8 — scale & infra | shipped (4/4: 8a Celery+Redis, 8b proxy rotator, 8c GraphQL gateway, 8d public-scan edge cache; deployed `0551938 → 5723927`) | 2,506 | 0–100 + ML | yes (distributed worker fleet, opt-in) | yes | yes | yes (rotating proxy pool, opt-in) | yes | yes |
| 9 — anti-bot probe + scale-out reliability | in-flight (2/8: **9a** anti-bot probe pipeline shipped — `b911a72`; **9b** distributed host rate limit shipped — `b6073af`; 9c–9h pending — see [ROADMAP](./ROADMAP.md)) | catalog freshness ≤1%/quarter via 9e | 0–100 + ML | yes (Redis sliding-window rate limit shared across N Celery workers via **9b — shipped**) | yes | yes | yes (curl-cffi JA3/JA4 + Playwright fallback via **9a — shipped**; geo-aware via 9c; CAPTCHA classifier via 9d) | yes | yes |

> [`ROADMAP.md`](./ROADMAP.md) is the **single source of truth** for what's
> in flight + planned. The "Live status" header at the top of the roadmap is
> updated at the end of every batch (current `main` SHA, current HF Space
> SHA, active batch, last batch shipped, test baseline). The master checklist
> ticks off each sub-phase as it lands. Phase 5h shipped a precision floor
> + link-liveness probe so the SPA's claimed bucket only contains hits the
> operator can click and immediately see a real profile (no soft-404s, no
> dead links); Phase 6 closed the coverage gap with `soxoj/maigret` (sites
> regen, engine-aware probes, head-to-head benchmark, PDF export). Phase 7
> closed the intel-enrichment frontier — **7a** stitches CLAIMED
> hits whose profile-image perceptual hashes match closely as a
> distinct edge type in the graph view, **7b** bolts a WHOIS /
> HIBP-style enrichment side panel onto every email-mode scan
> (RDAP for the domain, HIBP for breach exposure when an admin
> key is configured), **7c** adds a one-click reverse-image lookup
> chip row (Yandex / Google Lens / TinEye) under any CLAIMED hit
> whose extractor surfaced an avatar URL, and **7d** wires the ML
> reranker to a one-click admin retrain pipeline plus a pure-Python
> Kolmogorov-Smirnov drift detector that compares the production
> feature distribution against the synthetic training set, with
> per-feature alarms surfaced in a new admin **ML** tab. Together
> they open new dimensions of OSINT signal beyond maigret. Phase
> 8 made the platform horizontally scalable — **8a** distributed
> Celery + Redis worker fleet (opt-in via `RECKORA_USE_CELERY=1`),
> **8b** rate-limit-aware multi-proxy rotator with per-host async
> token bucket (opt-in via `enable_proxy_rotator=1` +
> `enable_host_rate_limit=1`), **8c** GraphQL gateway as an alternate
> transport over the existing REST/SSE surface (opt-in via the
> optional `[gql]` extra + `enable_graphql_gateway=1`), **8d** public-
> scan edge cache with CDN-friendly Cache-Control / ETag / 304
> revalidation for anonymous share links (opt-in via
> `enable_share_links=1`). All four sub-phases ship default-off so
> the live HF Space byte-for-byte mirror of `main` stays unchanged in
> default deployment. Phase 9 is the in-flight batch — it closes
> the residual-risk gaps that Phase 8b's proxy rotator does *not*
> cover. **9a (anti-bot probe pipeline) is shipped** (`b911a72`):
> a curl-cffi `AsyncSession`-backed `httpx.AsyncBaseTransport`
> spoofs real Chrome / Safari / Firefox TLS handshake bytes
> (cipher suite ordering + extension list = JA3 / JA4) plus a
> headless-chromium fallback for sites that still serve a JS
> challenge HTML, both opt-in via `enable_anti_bot_pipeline=1` —
> closing residual-risk **C1** (Cloudflare / Akamai / Datadome JS
> challenge) and **C2** (TLS / JA3 / JA4 fingerprint blocking),
> both **CRITICAL** in the Phase 8 deploy debrief.
> **9b (distributed host rate limit) is shipped** (`b6073af`):
> a Redis-backed sliding-window counter — single atomic Lua
> script for the `INCR + EXPIRE + check` round-trip — replaces
> Phase 8b's per-process token bucket so N Celery workers share
> one bucket per host, closing residual-risk **M1**
> (per-process token bucket; **MEDIUM**); opt-in via the
> `host_rate_limit_use_redis` admin knob with fail-open fallback
> to the in-memory bucket on Redis transport error. The
> remaining 6 sub-phases (9c–9h) cover geo-aware proxy
> selection, CAPTCHA classifier + structured `UNKNOWN`
> labelling, quarterly catalog auto-refresh from upstream
> maigret, per-scan hard budget with a new `SKIPPED` bucket,
> proxy reputation warm-up / weighted selection, and an HF
> Space keep-warm cron. Each Phase 9 sub-phase carries the
> residual-risk ID it closes (`C1`, `C2`, `M1`–`M7` from the
> Phase 8 deploy debrief) and is detailed in
> [`ROADMAP.md`](./ROADMAP.md).

### Phase 5 — operator polish (8/8 sub-phases shipped)

Phase 5 lands as a stream of focused commits, each landing direct-to-main with
its own CI green + auto-deploy to the HF Space. The sub-phase letters reflect
implementation order, not feature priority. The current state of every
sub-phase + planned Phase 9 work is tracked in [`ROADMAP.md`](./ROADMAP.md).

| Sub-phase | Commit | One-liner |
| --- | --- | --- |
| 5f — infra polish | [`589f768`](https://github.com/yowanda/Reckora/commit/589f768) | `pre-commit` hooks (ruff check + format), mypy strict on all 16 source files, separate lint/typecheck steps in CI, Playwright e2e scaffold (3 login flow cases) + new `e2e (Playwright)` CI job |
| 5d — admin knobs | [`1e8c170`](https://github.com/yowanda/Reckora/commit/1e8c170) | New `app/admin_settings.py` with 5s-TTL cache + `KnobSpec` registry; runtime-tunable `graph_max_depth`, `graph_max_secondary`, `graph_max_seed_hits`, `extract_min_confidence` exposed via `GET /api/settings` (members) and `GET/PATCH /api/admin/settings` (admins); SettingsTab UI for live editing |
| 5c — username permutations | [`80734f4`](https://github.com/yowanda/Reckora/commit/80734f4) | New `app/permutations.py` (4 strategies: separator, suffix, prefix, leetspeak); new `POST /api/search/permute` SSE route + `GET /api/permute/strategies`; new `permute` mode joins `list` and `graph` (with non-destructive SQLite migration); new SearchView mode toggle + per-candidate result filter |
| 5e — outbound proxy / Tor | [`81ed51a`](https://github.com/yowanda/Reckora/commit/81ed51a) + [`4d2c470`](https://github.com/yowanda/Reckora/commit/4d2c470) | New `app/proxy.py` gate (`RECKORA_PROXY_URL` env + `proxy_enabled` admin knob) — supports `socks5://`, `socks5h://`, `http://`, `https://`; centralised httpx client construction so list/graph/permute streams + curl-cffi rescue all pick up proxy uniformly; `GET /api/health` and `GET /api/settings` expose `{configured, enabled, active, scheme}` descriptor (URL itself never leaks); Header pill "via tor" / "via http proxy" |
| 5b — scan export | [`17b50c2`](https://github.com/yowanda/Reckora/commit/17b50c2) | New `app/exports.py` (CSV / JSON / Markdown / HTML formatters, pure stdlib); new `GET /api/history/<id>/export.<fmt>` route (auth + ownership + format whitelist + `Cache-Control: private, no-store`); ScanModal export buttons + browser-side download via object URL + `Content-Disposition` filename. PDF intentionally deferred — HTML export prints to clean A4 PDF via browser File→Print, and adding `reportlab`/`weasyprint` would pull in a multi-MB native dep stack |
| 5a — identity types | [`6e7b64b`](https://github.com/yowanda/Reckora/commit/6e7b64b) + [`93681f8`](https://github.com/yowanda/Reckora/commit/93681f8) | New `app/identity.py` with per-type validators (`username` / `email` / `phone` / `gaia` / `yandex` / `skype`), normalisers, and a curated `KNOWN_IDENTITY_OVERRIDES` map (Gravatar→email, Blogger-by-GAIA→gaia, Yandex* → yandex). `sites.json` schema gains optional `identityTypes` per site (default `["username"]` keeps the catalog backwards-compatible). `SiteCatalog.filtered()` and `check_site()` accept an `identity_type` kwarg; non-username scans bypass username-shaped `regex_check` and skip the username-trained ML reranker. Per-site transforms applied before URL substitution (Gravatar email → MD5 hex via `IDENTITY_TRANSFORMS`). `SearchRequest` validates the identifier against the active type and normalises (email→lower, phone→digits+plus, yandex→lower). Graph + permute SSE routes reject non-username identity types at the validator layer (their extraction / permutation logic is username-specific). Frontend ships an identity-type picker dropdown next to the search input, with dynamic placeholder + per-type regex client-side validation; the picker auto-locks to `username` when graph/permute mode is selected. Companion commit `93681f8` pins the pre-commit ruff hook to `v0.15.12` so local hooks and CI's `ruff format --check` agree |
| **5g — scan-depth picker** | [`61d7716`](https://github.com/yowanda/Reckora/commit/61d7716) | Backend `SearchRequest.limit` cap raised from `le=600` to `le=3000` so a single request can probe the entire 2,506-site catalog (with headroom for Phase 6a's regen). Frontend `types.ts` introduces `SCAN_DEPTH_PRESETS = [100, 250, 500, 1000, 'all']` + `resolveScanDepth(depth, catalogCount)` which maps `'all'` to the live catalog count clamped to the backend hard cap. `SearchBar.tsx` exposes a `Scan depth` `<select>` next to the existing identity-type picker, labelled with estimated wall-clock per preset (e.g. `All sites (~2,506) (~330s)`). `App.tsx` forwards the picker value through all three SSE stream call sites (list / graph / permute). Default stays at `250` so casual single-username lookups still take ~30 s; `All sites` is opt-in for thorough sweeps. |
| **5h — claimed precision floor** *(in flight — see [ROADMAP](./ROADMAP.md))* | — | Multi-layer post-claim demotion so the SPA's claimed bucket only contains hits the operator can click and immediately see a real profile. Layer A: `min_claimed_confidence` admin knob (default 60); Layer B: `min_claimed_ml_confidence` (default 50); Layer C: `require_verify_for_claim` (default 0); Layer D: post-claim link-liveness probe on the human-facing display URL (404/410, redirect-to-homepage, redirect-to-login, soft-404 → demote to `UNKNOWN`); Layer E: dual-URL check when `urlProbe ≠ url`. Each demoted row keeps its evidence (signals, ml_confidence) so it can still be inspected — it's just not in the claimed bucket. |

**Phase 5 test growth:** 115 → **207 pytest cases** at end-of-5g (+92 across 5d/5c/5e/5b/5a; 5g introduces no new cases since it's pure SPA wiring + a Pydantic field bound). 0 mypy errors across 17 source files, 3 Playwright e2e cases. Frontend bundle 435.91 KB / 134.37 KB gzip after 5g (+1.12 KB gzip from the picker). Phase 5h will grow the test count by ~+10 cases (one per new demote signal + admin-knob round-trip) — see [ROADMAP](./ROADMAP.md) for current progress.

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

**Phase 4 benchmark — same 100-site scan with the bundled ONNX classifier enabled vs. disabled.**

| Metric | Phase 3 | Phase 4 (ml on) | Δ |
| --- | --- | --- | --- |
| 100-site scan total wall | 25.8 s | **26.0 s** | +0.5 s (~+2 %) — 9 ms / probe ML overhead |
| ML scoring overhead per probe (median) | n/a | **8.8 ms** | runs synchronously on each result, no extra I/O |
| ML probability median for CLAIMED probes | n/a | **97 / 100** | scoring properly anchors confident hits |
| ML probability median for FREE probes | n/a | **24 / 100** | clean separation from CLAIMED — no overlap of medians |
| `ml-disagree` signals / 100 (rule vs ML) | n/a | **10** | surfaces borderline calls for the operator without overriding the rule classifier |
| Bundled model size (ONNX) | n/a | **~1 KB** | scikit-learn LogisticRegression, 8 features, 3 classes, ships in repo |
| Live site catalog editor | — | **`POST /api/admin/sites` w/ override merge** | upsert / disable / delete the bundled `sites.json` from the SPA — no rebuild needed |
| Custom sites supported | 0 | **unbounded** | persisted to SQLite `site_overrides`, merged at runtime, hot-reloadable |
| Per-member scan history persisted | 0 | **last 50 scans / member / username** | gz-compressed JSON results in SQLite `scans`, with auto-pruning |
| Scan diff endpoint | n/a | **`GET /api/history/diff/{a}/{b}`** | reports newly_claimed / no_longer_claimed / status_changed / confidence_shift |

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

| Method | Path                                       | Auth     | Description                                              |
| ------ | ------------------------------------------ | -------- | -------------------------------------------------------- |
| GET    | `/api/health`                              | public   | Liveness + version + site count + proxy descriptor       |
| POST   | `/api/auth/login`                          | public   | `{username, password}` → set cookie                      |
| POST   | `/api/auth/logout`                         | public   | Clear session cookie                                     |
| GET    | `/api/auth/me`                             | member+  | Current logged-in member                                 |
| GET    | `/api/sites`                               | member+  | Site catalog and tag list                                |
| GET    | `/api/settings`                            | member+  | Effective admin knobs + proxy descriptor (Phase 5d/5e)   |
| POST   | `/api/search`                              | member+  | SSE: per-site check results (`list` mode)                |
| POST   | `/api/search/graph`                        | member+  | SSE: depth-N recursive graph search (Phase 3, depth admin-tunable in 5d) |
| POST   | `/api/search/permute`                      | member+  | SSE: username permutation scan (Phase 5c)                |
| GET    | `/api/permute/strategies`                  | member+  | Permutation strategy descriptors (Phase 5c)              |
| GET    | `/api/history`                             | member+  | List own scan history (last 50)                          |
| GET    | `/api/history/{id}`                        | member+  | One scan: summary + results                              |
| GET    | `/api/history/{id}/export.{fmt}`           | member+  | Download as csv \| json \| markdown \| html (Phase 5b)   |
| DELETE | `/api/history/{id}`                        | member+  | Delete one scan                                          |
| GET    | `/api/history/diff/{from}/{to}`            | member+  | Diff two scans (Phase 4)                                 |
| GET    | `/api/admin/members`                       | admin    | List all members                                         |
| POST   | `/api/admin/members`                       | admin    | `{username, password, role}` — create a member           |
| PATCH  | `/api/admin/members/{username}`            | admin    | `{role?, password?}` — update role and/or reset password |
| DELETE | `/api/admin/members/{username}`            | admin    | Delete a member (cannot delete yourself or last admin)   |
| GET    | `/api/admin/settings`                      | admin    | Read all knob values (Phase 5d)                          |
| PATCH  | `/api/admin/settings`                      | admin    | Update knob values (Phase 5d/5e)                         |
| GET    | `/api/admin/sites`                         | admin    | List bundled + override sites (Phase 4)                  |
| POST   | `/api/admin/sites`                         | admin    | Upsert / disable / delete a site (Phase 4)               |

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
cd backend  && uv run ruff check . && uv run ruff format --check . \
            && uv run mypy && uv run pytest
cd frontend && pnpm lint && pnpm tsc --noEmit && pnpm build
cd frontend && pnpm e2e:install && pnpm e2e   # Playwright smoke tests
```

CI runs three jobs on every push (`.github/workflows/ci.yml`):

1. `backend` — ruff lint + ruff format check + mypy + pytest.
2. `frontend` — eslint + `tsc --noEmit` + `vite build`.
3. `e2e` — builds the SPA, mounts it in the backend, boots `uvicorn`
   with a seed admin, and runs Playwright tests against the
   single-origin server. Failures upload a `playwright-report`
   artifact for offline triage.

### Pre-commit hooks

The repo ships with a `.pre-commit-config.yaml`. After the first clone:

```bash
uv tool install pre-commit  # or: pip install pre-commit
pre-commit install
```

This wires up trailing-whitespace, end-of-file-fixer, ruff (lint + fix),
ruff-format, and a local `eslint --max-warnings=0` hook on the frontend
sources. The hooks deliberately stay fast; full pytest and Playwright
runs live in CI so `git commit` doesn't block on them.

### What's covered

- **Backend (76 pytest cases)** — health endpoint, auth gate on
  `/api/sites` and `/api/search`, login (success / wrong password /
  rate-limit), `/api/auth/me` round-trip, admin CRUD on
  `/api/admin/members`, role-gating (member → 403), self-delete and
  last-admin guards, scan history + diff (Phase 4), site overrides
  CRUD (Phase 4), ML feature builder (Phase 4), socid-extractor
  integration (Phase 3).
- **Backend type-check** — `mypy` (zero errors against `app/`,
  `ignore_missing_imports = true` for third-party deps without stubs).
- **Frontend** — ESLint + `tsc --noEmit` + production `vite build`.
- **End-to-end (Playwright)** — anonymous load shows login + gates
  `/api/sites`, wrong password renders the static error message, admin
  login lands on the search view with the **Admin** tab visible.

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

# Reckora vs Maigret — architecture & feature comparison

Pulled directly from the source trees:
- Reckora — `yowanda/Reckora@ef75b8d` (`/home/ubuntu/repos/Cacadoxx`).
- Maigret — `soxoj/maigret@main` (`/home/ubuntu/repos/maigret`, depth 50). Package version 0.6.0.

---

## TL;DR

| Axis | Reckora | Maigret |
| --- | --- | --- |
| Goal | A small **multi-tenant web app** that streams username-availability checks to a browser, gated by login. | A CLI **OSINT toolkit** that produces a full *dossier* on a target identity, with a thin web UI as a side mode. |
| Primary surface | HTTPS + SSE web API + React SPA (single-origin). | `maigret` console command (3,000+-site scan). Flask web UI is optional. |
| Code size (Python) | ~1,135 LOC across 6 modules. | ~6,132 LOC across 14 modules — ~5.4× larger. |
| Sites | **2,506 curated sites** *derived* from Maigret’s DB (`backend/scripts/build_sites.py`, MIT). | 3,154 native sites + 13 shared engines. |
| Identity types | usernames only (`a-zA-Z0-9._-`, length 1–64). | usernames, emails, GAIA IDs, phones, Yandex IDs, … (`--id-type`), plus URL parsing & permutation. |
| Recursive enrichment | **yes** (Phase 3) — `socid_extractor` integration on every CLAIMED hit (61 % of CLAIMED yield identifiers), depth-1 graph recursion, **edges + extracted events emitted on the SSE stream**. Bounded fanout (≤ 8 seed hits × ≤ 5 secondary). | yes — extracts other IDs from profile pages and re-scans, no fanout cap by default. |
| Confidence scoring | **0–100 rule score + complementary 0–100 ML score** (Phase 4, bundled ONNX, ~9 ms / probe overhead). `ml-disagree` signal flags rule/ML disagreements for the operator. | binary CLAIMED/AVAILABLE/UNKNOWN; no probability surface. |
| Site catalog editing | **live admin DB editor** (Phase 4) — `POST /api/admin/sites` upserts/disables/deletes the bundled `sites.json` at runtime via SQLite `site_overrides`. Hot reload, no rebuild. | edit `data.json` in the package on disk; `--self-check` / `--auto-disable`. |
| Scan history & diff | **per-member persisted history** (Phase 4) — last 50 scans / member / username, `GET /api/history/diff/{a}/{b}` returns newly_claimed / no_longer_claimed / status_changed / confidence_shift. | none — CLI is stateless across runs. |
| Reporting | **streamed JSON + interactive react-flow graph view + admin-side history diff viewer** (consumer renders cards or graph in-browser). | TXT, CSV, HTML, JSON, Markdown, PDF, XMind, Graph (PyVis), AI summary. |
| Auth | mandatory members-only login (admin-managed). | none (it’s a local CLI). |
| Deploy target | one Docker image → HF Space / Fly.io. | `pip install maigret`, plus a Docker stage for the optional web UI. |
| Async runtime | `httpx.AsyncClient` + asyncio task pool. | `aiohttp` + a custom `CheckerBase` hierarchy (incl. cloudscraper / curl-cffi for WAF-evading runs). |

---

## 1. Mission and scope

**Maigret is an investigation tool.** Given a username (or a parsed URL, or an email), it:

1. checks 3,000+ sites,
2. *parses* found profile pages with `socid_extractor` to pull names, bios, IDs, links to other accounts,
3. recursively re-scans those discovered IDs,
4. cross-references everything into a single per-target dossier (HTML/PDF/Markdown/Graph/XMind/AI),
5. can submit new sites it discovers back into the upstream DB.

Maigret optimises for completeness, evidence quality, and report-grade output for analysts.

**Reckora is a multi-user web service.** It scopes the problem hard:

1. one input — a `^[A-Za-z0-9._-]{1,64}$` username,
2. one output — a stream of per-site `{status, url, elapsed_ms}` JSON events,
3. delivered to a logged-in member’s browser through an SSE channel,
4. with everything else (extraction, dossier generation, deep parsing, proxies) intentionally cut.

Reckora optimises for low-latency interactive scans, predictable hosting cost, and the ability to *gate* access behind admin-managed accounts.

The two tools are **complementary, not competing**: Reckora is roughly “the username-availability slice of Maigret with a SaaS UX wrapper.”

---

## 2. Process model & runtime architecture

### Reckora

```
┌────────────────────────────────────────────────────────────┐
│ FastAPI (uvicorn worker, single process, asyncio)          │
│                                                             │
│  CORS → cache-control mw → routes                           │
│                                                             │
│   /api/auth/login      ──► sqlite3 (members + secret)       │
│   /api/auth/me         ──► HMAC session cookie (reckora_session)
│   /api/admin/members*  ──► sqlite3                          │
│   /api/sites           ──► load_catalog() lru_cache         │
│   /api/search          ──► StreamingResponse SSE  ┐         │
│                                                    │         │
│                                                    ▼         │
│   _search_stream()  →  asyncio.gather over httpx.AsyncClient │
│                        (per-host pooling, follow_redirects)  │
│   TTLCache (in-memory, dict)                                 │
│                                                             │
│  StaticFiles("/assets") + FileResponse SPA fallback         │
└────────────────────────────────────────────────────────────┘
```

Key facts:

- **Single FastAPI app** (`backend/app/main.py:37`) serves both API and the built React SPA from one origin. SPA fallback at `/{path:path}` returns `index.html` so the React Router-style routes work.
- **HTTP client**: a fresh `httpx.AsyncClient` per request, configured with HTTP/2 (the `httpx[http2]` extra) and a `Limits(max_connections=settings.total_concurrency)` pool.
- **Concurrency**: bounded by `httpx.Limits` plus a semaphore inside `stream_checks` (`backend/app/checker.py`). Default `total_concurrency=200`.
- **Caching**: `TTLCache` in `backend/app/cache.py` — naive in-memory dict keyed on `(username, sorted tags, limit)`, with `cache_max_entries`/`cache_ttl_seconds` knobs in `settings`.
- **Persistence**: SQLite at `RECKORA_DB_PATH` (default `/data/reckora.db`). Three tables: `members`, `sessions` (no longer used at runtime — sessions are HMAC-signed JWT-ish strings), `session_secret` (single row holding the HMAC key if `RECKORA_SESSION_SECRET` env isn’t set).
- **Auth**: HMAC-signed session cookie, base64-url JSON payload `{u, r, e}` (username, role, expiry). No DB hit per request beyond fetching the user row inside `current_member`.
- **Static assets**: Vite content-hashed → `/assets/index-*.js`, served by `StaticFiles`. The cache-busting middleware (`backend/app/main.py:55-85`) sets `Cache-Control: public, max-age=31536000, immutable` for `/assets/*`, `no-cache, no-store, must-revalidate` for HTML, and skips `/api/*`.

### Maigret

```
maigret CLI ──► argparse ──► checking.maigret(...) ──► aiohttp ClientSession
                                       │
                                       ├── SimpleAiohttpChecker        (default)
                                       ├── ProxiedAiohttpChecker       (--proxy/--tor-proxy/--i2p-proxy)
                                       ├── CurlCffiChecker             (curl-cffi for browser TLS fingerprint)
                                       ├── AiodnsDomainResolver        (--with-domains)
                                       └── CheckerMock                 (tests / self-check)
                       │
                       ├── process_site_result()  → CheckResult (with extracted IDs)
                       ├── socid_extractor.extract() → links, profile fields
                       ├── permutator (--permute) → derived usernames
                       └── recursion: requeue discovered IDs
                       │
                       ▼
                  report.py → save_{txt,csv,json,html,pdf,md,graph,xmind} +
                  ai.py    → optional OpenAI summary of dossier
```

Key facts:

- **CLI-first**, with an *optional* Flask web UI in `maigret/web/app.py` (~330 LOC). The web UI runs scans in a background thread and exposes `/`, `/search`, `/status/<ts>`, `/results/<sid>`, `/reports/<file>`.
- **Pluggable checkers** (`maigret/checking.py`): five concrete subclasses of `CheckerBase`, switching strategy based on protocol (HTTP, DNS, Tor, I2P) or anti-bot avoidance (`CurlCffiChecker` + `cloudscraper` for Cloudflare/WAF).
- **Recursive search**: `extract_ids_data` → `parse_usernames` → `update_results_info`. Discovered IDs are re-queued with their proper `id_type` (`gaia_id`, `yandex_public_id`, etc.) and re-scanned.
- **Database**: `MaigretDatabase` reads `maigret/resources/data.json` (3,154 sites + 13 engines) and supports `--db <path|url>`, `--no-autoupdate`, `--force-update`, `--self-check`, `--auto-disable`, `--diagnose`, `--stats`, `--submit`. Auto-fetches latest DB from GitHub once per 24h.
- **No persistent storage** — outputs are written to `--folderoutput` (default `./reports`).
- **Concurrency**: `--max-connections` (default 100) sets `aiohttp.TCPConnector(limit=…)`.

### Where the architectures genuinely differ

| Concern | Reckora | Maigret |
| --- | --- | --- |
| Process model | Long-running ASGI server (multi-tenant, persistent). | Short-lived CLI invocation. Web mode is a thin Flask wrapper around the same async core. |
| State | SQLite (members) + in-memory TTLCache + HMAC cookies. | None at runtime; only output files. |
| Async lib | `httpx[http2]` (asyncio). | `aiohttp` (asyncio) + `aiohttp-socks` + `cloudscraper` + `curl-cffi`. |
| Anti-bot strategy | a single `httpx` SSL context, default UA from `settings.user_agent`. | full strategy stack: TLS context, browser-like fingerprints (`curl-cffi`), Cloudflare bypass (`cloudscraper`), Tor/I2P/SOCKS5 proxies, rotating engines, retry on “temporarily failed”. |
| Identity surface | username only. | username, email, GAIA, Yandex, Steam, Skype, Vimeo, etc. (cf. `--id-type`). Also `--parse <url>` to derive identifiers from a profile URL. |
| Error reporting | `CheckResult.status` ∈ {claimed, free, unknown, error, skipped}. | rich `CheckError` taxonomy + diagnose mode + optional `--print-errors`. |

---

## 3. Site database

| Aspect | Reckora `backend/app/data/sites.json` | Maigret `maigret/resources/data.json` |
| --- | --- | --- |
| Total entries | **555** | **3,154** (644 disabled, 1,371 use a shared engine) |
| Schema | flat dict, custom subset of Maigret’s schema (15 allowed keys). | full Maigret schema with `engine`, `activation`, `headers`, `errors`, `requestHeadOnly`, `tags`, etc. |
| Source | curated by `backend/scripts/build_sites.py` from Maigret’s `data.json`. Filters: not disabled, no `engine`, must have `{username}` URL placeholder, `alexaRank ≤ 200_000`. | hand-maintained + community submissions (`maigret --submit <url>`). |
| Distribution by `checkType` | 256 status_code / 254 message / 45 response_url. | 2,125 status_code / 933 message / 96 response_url. |
| Tag vocab | derived from upstream tags (top 10: `ru, gaming, coding, social, photo, blog, news, music, forum, us`). | 142 tags total (top 10: `ru, forum, gaming, coding, ua, us, photo, tech, social, news`). |
| Auto-update | none. Refreshed by re-running `build_sites.py` against a fresh upstream copy. | yes — every run pulls the latest `data.json` from GitHub, falling back to bundled copy (`db_updater.py`). |
| Engines | not supported. Engine-derived sites are skipped at build time. | first-class concept — 1,371 engine-derived sites (`MaigretEngine`). |

The build script honestly cites the source: `"source": "https://github.com/soxoj/maigret (MIT)"` is embedded in the JSON itself.

---

## 4. Check pipeline (per-site logic)

### Reckora `backend/app/checker.py`

For each `(Site, username)` pair:

1. apply `regexCheck` (skip if mismatch).
2. issue `client.get(url.replace("{username}", username), follow_redirects=True)` with the global `User-Agent` plus per-site headers.
3. classify by `check_type`:
   - **status_code** — `[200,300) → CLAIMED`, `404|410 → FREE`, else `UNKNOWN`.
   - **response_url** — compare the final URL prefix against the templated URL pattern.
   - **message** — substring match against `presenseStrs` / `absenceStrs` in the response body, with `error_strs` mapping to `ERROR`.
4. emit a `CheckResult` (`Status`, `url`, `url_main`, `tags`, `elapsed_ms`, `error?`).
5. concurrency is bounded by `httpx.Limits` + an asyncio semaphore in `stream_checks`.

Dimensions explicitly **not** present:
- ~~no follow-on profile parsing (no `socid_extractor` integration)~~ — **added in phase 3**: every CLAIMED hit with confidence ≥ 70 runs through `extract_identifiers()` (socid_extractor + OG meta + email/social-link mining). Surfaced via `extracted` field on `CheckResult` and `event: extracted` / `event: edge` on `/api/search/graph`.
- no per-site cookie jar,
- no proxy support per site,
- no `requestHeadOnly` short-circuit,
- no domain-mode fallback,
- no per-site retry policy,
- no engine resolution layer.

### Maigret `maigret/checking.py`

The pipeline is much heavier:

1. select a checker class based on protocol (`SimpleAiohttpChecker` / `ProxiedAiohttpChecker` / `AiodnsDomainResolver` / `CurlCffiChecker`).
2. resolve `engine` macros (a site can inherit URL/regex/headers from a shared engine definition).
3. honour `requestHeadOnly`, custom HTTP method/payload, per-site cookies, retry/timeout.
4. classify response:
   - status, message body, `response_url`, **plus** profile-page parsing (`process_site_result` → `extract_ids_data`).
5. attach extracted IDs (display name, full name, links, GAIA IDs, etc.) to the `CheckResult`.
6. requeue discovered identifiers for recursion.
7. accumulate per-username results into a graph / dossier.

So Maigret’s pipeline is a multi-pass enrichment loop; Reckora’s is a single-pass yes/no/maybe classifier.

---

## 5. User-facing surfaces

### Reckora

- **React SPA** (`frontend/src/`, ~1,063 LOC across 10 `.tsx` files):
  - `LoginPage.tsx` — gate.
  - `SearchBar.tsx` + `App.tsx` — single search input → SSE consumption.
  - `ResultCard.tsx` / `Stats.tsx` — live result grid with claimed / free / unknown / error counters.
  - `TagFilter.tsx` — multi-select for `tags`.
  - `AdminPanel.tsx` (305 LOC) — full CRUD on members (add, delete, change role, reset password).
  - `Header.tsx` + `Disclaimer.tsx` — chrome.
- **REST + SSE API** (under `/api/`):
  - `POST /api/auth/login` (rate-limited).
  - `POST /api/auth/logout`.
  - `GET /api/auth/me`.
  - `GET /api/admin/members`, `POST /api/admin/members`, `PATCH /api/admin/members/{u}`, `DELETE /api/admin/members/{u}` (admin-only).
  - `GET /api/sites` — catalog with tag counts.
  - `POST /api/search` — SSE stream of `start`, `result*`, `done` events; rate-limited.
  - `POST /api/search/graph` — SSE stream of `start`, `result*`, `extracted*`, `edge*`, `secondary_start*`, `done` events for depth-1 recursive identity discovery (Phase 3).
  - `GET /api/health` — public liveness.
- **Swagger** at `/docs`, ReDoc at `/redoc`.

### Maigret

- **CLI** (`maigret <username>`) — primary surface, with 50+ flags:
  - selection: `-a/--all-sites`, `--top-sites N`, `--tags ru,forum`, `--exclude-tags`, `--site`, `--use-disabled-sites`.
  - identity: `--id-type {username,gaia_id,yandex_public_id,…}`, `--permute`, `--parse <url>`.
  - extraction: `--no-recursion`, `--no-extracting`.
  - DB: `--db`, `--no-autoupdate`, `--force-update`, `--self-check`, `--auto-disable`, `--diagnose`, `--stats`, `--submit`.
  - networking: `--proxy`, `--tor-proxy`, `--i2p-proxy`, `--with-domains`, `--cookies-jar-file`, `--max-connections`, `--timeout`, `--retries`.
  - reporting: `-T/-C/-H/-X/-P/-M/-G/-J`, `--reports-sorting`, `--folderoutput`, `--print-not-found`, `--print-errors`.
  - AI: `--ai`, `--ai-model`.
  - misc: `--web <port>`, `--ignore-ids`, `-v/-vv/-vvv`, `--no-color`, `--no-progressbar`, `--version`.
- **Web UI** (`maigret --web`): Flask (`flask[async]`) app with templates `index.html`, `results.html`, `status.html`, `base.html`. Routes: `/`, `/search` (POST), `/status/<ts>`, `/results/<session_id>`, `/reports/<file>`. Each search runs in a background thread; clients poll `/status` for progress.
- **Python library**: `import maigret; await maigret.checking.maigret(...)` — embeddable.
- **Telegram bot**: external, mentioned in README (not in repo).

---

## 6. Reporting / output

| Format | Reckora | Maigret |
| --- | --- | --- |
| JSON streaming (live) | ✅ via SSE (`event: result`/`done`) | ❌ (CLI prints to terminal) |
| JSON file | ❌ | ✅ (`-J ndjson|simple`) |
| CSV | ❌ | ✅ (`-C`) |
| TXT | ❌ | ✅ (`-T`) |
| HTML | ✅ (the SPA *is* the HTML report) | ✅ standalone (`-H`) |
| Markdown | ❌ | ✅ (`-M`) |
| PDF | ❌ | ✅ (`-P`, via `xhtml2pdf`/`reportlab`) |
| Graph (network of accounts/IDs) | **✅ live, in-browser** (`@xyflow/react` canvas, edges streamed in via `/api/search/graph`) | ✅ standalone HTML (`-G`, via `networkx` + `pyvis`) |
| XMind mind-map | ❌ | ✅ (`-X`) |
| AI narrative summary | ❌ | ✅ (`--ai`, OpenAI-compatible) |
| Profile-page extraction | **✅ (`socid_extractor` + OG/email/social-link mining)** | ✅ (`socid_extractor`) |

Reckora’s “report” is the live grid in the SPA + the consumer’s ability to read the SSE stream programmatically; there is no offline artefact.

---

## 7. Auth & access control

| | Reckora | Maigret |
| --- | --- | --- |
| Auth model | Members-only. Seed admin from `RECKORA_ADMIN_USERNAME`/`RECKORA_ADMIN_PASSWORD`. Admins can invite/delete/reset. Members can search but not manage. | None — runs locally. |
| Sessions | HMAC-signed cookie + Bearer token with the same payload. 7-day TTL. Rate-limited login (default `5/minute`). | n/a |
| Rate limiting | `slowapi` on `/api/auth/login` and `/api/search`. | n/a |
| Public surface | only `/api/health` is public. Everything else requires `current_member`. | the binary itself is the surface; no over-the-network attack surface unless `--web` is enabled. |
| Disclaimer / ToU | mandatory dismissable disclaimer modal on first login (localStorage `reckora.disclaimer.v1`). | none in the tool itself; spelled out in the README/docs. |

This is the dimension where Reckora most clearly diverges from Maigret’s philosophy: Reckora is built so an operator can self-host one instance and *invite a small team* to use it, then revoke access centrally.

---

## 8. Operations & deployment

| | Reckora | Maigret |
| --- | --- | --- |
| Distribution | a single `Dockerfile` at repo root (Vite build → FastAPI image). Plus `fly.toml` for Fly.io. Deployed image runs as a single-origin SPA on `${PORT:-7860}`. | `pip install maigret` (PyPI). Plus a multi-stage `Dockerfile` that exposes both `cli` and `web` stages. |
| Ports | one HTTP port. | for CLI: none. For web: `flask run`–style port (`5000` default, `${PORT}` honoured). |
| Persistence | a `RECKORA_DB_PATH` SQLite file (HF Spaces persists `/data` only on paid tiers; otherwise the seed admin re-runs on boot from env). | none required. |
| Caching | in-memory TTLCache (per-process). HTTP-level cache headers via the new middleware (`/assets/*` immutable, HTML no-cache, `/favicon.svg` 1h). | none. |
| Observability | `logging.getLogger("reckora")` at INFO. No structured logs / metrics. | extensive `--debug` mode that dumps responses to `debug.log`; `--diagnose` to debug bad sites. |
| CI | GitHub Actions: `pnpm lint && tsc && build` for frontend, `ruff` + `pytest` for backend. | GitHub Actions: `pytest`, lint, doc build. |
| Hosting today | https://yesmyyt-cacadoxx.hf.space (HF Space, Docker SDK). | Local CLI; Telegram bot run by the maintainer; Cloud Shell tutorials. |
| Cost profile | one always-on web process (free HF Space tier on cpu-basic). | $0 incremental (it’s a CLI). |

---

## 9. Tests & QA

| | Reckora | Maigret |
| --- | --- | --- |
| Test files | `backend/tests/test_api.py`, `test_checker.py`, `test_sites.py` (29 tests). | `tests/` with extensive coverage of checking, reporting, sites, submitting, AI, etc. |
| Test runner | `pytest` + `pytest-asyncio` + `pytest-httpx` (HTTP mocking). | `pytest`. |
| Lint | `ruff` (E/F/I/N/UP/B/C4/SIM/RUF). | `flake8` + `mypy` (per CHANGELOG). |
| Self-check of site DB | the build script trims; no runtime self-check. | first-class — `--self-check`, `--auto-disable`, `--diagnose`. |

---

## 10. What Reckora removed *on purpose*

Reading `build_sites.py` and the slim `Site` dataclass tells you which Maigret features Reckora is **explicitly** opting out of:

- Engine-resolved sites (`engine` field is dropped at build time → 1,371 sites lost vs upstream).
- Disabled sites (`disabled: true` is dropped).
- Activation / cookies (`activation` field is dropped — these are sites that need a one-time captcha / login flow).
- HEAD-only requests (`request_head_only` is allowed in the dataclass but the checker doesn’t do anything special with it).
- ~~Profile-page parsing & recursion (no `socid_extractor` dependency at all).~~ — **shipped in phase 3** with hard caps on fanout (depth ≤ 1, ≤ 5 secondary usernames per parent, ≤ 8 seed CLAIMED hits).
- Permutation, ignore-list, ID-type variants, URL parsing.
- Tor / I2P / SOCKS5 proxies, custom CA bundles, browser-fingerprinting clients.
- Heavy reporting (PDF/Graph/XMind/Markdown/AI) — replaced with “consumer renders the stream however it wants.”

These cuts are what allow Reckora to fit in ~1,135 LOC and ship as a single FastAPI image.

---

## 11. Where the two tools could share more code

If you ever want to close the feature gap without rewriting Maigret:

- **Engine resolution at build time** in `build_sites.py` — instead of dropping engine sites, expand the engine macros and pre-bake the URLs. That would push Reckora’s site count from 555 → ~1,800+ without any runtime change.
- **`requestHeadOnly`** — already in the schema; honour it in `check_site` to halve bandwidth on the cheap-classify subset.
- **Cookie jar / per-site headers** — Reckora already loads `headers` from upstream; a small change in `check_site` to maintain a per-site `httpx.Cookies` dict would unlock several Cloudflare-fronted sites.
- **`--self-check` parity** — port Maigret’s self-check into a CI job that runs against `data/sites.json` and surfaces flapping sites.
- **Optional report export** — adding a `GET /api/search/report?format=md|json` endpoint is straightforward given the SSE stream is already collected into `collected: list[dict]`.

None of these are required; they’re just the cheapest paths to absorb more of Maigret without changing Reckora’s product positioning.

---

## 12. Honest verdict

- Reckora is **not** trying to be Maigret. It’s a *member-gated, single-origin web app* whose check pipeline happens to share Maigret’s site DB.
- For a forensics analyst, Maigret is still the right tool: dossier, recursion, extraction, multi-format reports, three thousand sites, proxies.
- For *“let my team scan a username and watch the results stream in their browser, behind a login I control,”* Reckora is leaner, easier to host, and intentionally narrower.
- The two can coexist: Reckora’s site database is regenerated from Maigret on demand, so any site-DB improvement upstream lands in Reckora at the next `python scripts/build_sites.py` run.

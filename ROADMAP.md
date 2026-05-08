# Reckora roadmap — Phase 5h → Phase 8

This roadmap is the **single source of truth** for Reckora's planned + in-flight
work. Anything not on this list is not committed-to. Items are ticked off when
the change is merged into `main`, CI is green, and (where applicable) the live
HF Space at <https://yesmyyt-cacadoxx.hf.space> reflects the change.

---

## Live status

| Field | Value |
| --- | --- |
| Repo HEAD on main | `696576d` (Phase 10g — AI investigation summary with streaming markdown + runtime secrets store; feat commit `696576d` pushed direct to `main`; new module `app/ai_summary.py` builds a markdown report from scan history + extractor fields + graph edges + pHash matches and streams a chat-completion summary back through `POST /api/history/{scan_id}/ai_summary`; pluggable wire-format adapters for OpenAI / Anthropic / OpenRouter / local Ollama with the OpenAI/Anthropic/OpenRouter paths consuming SSE and the Ollama path consuming newline-delimited JSON; new runtime secret store `app/secrets_store.py` backed by `/data/secrets.json` (atomic write, 0600 perms, in-process cache) so API keys never live in the SQLite admin DB and cannot leak through the existing settings export endpoint; new admin endpoints `GET /api/admin/secrets`, `PUT /api/admin/secrets/{key}`, `DELETE /api/admin/secrets/{key}` so an operator can configure the key from the SPA without shelling onto the box (values never echoed back — only a boolean `configured` flag); 4 admin knobs total (`enable_ai_summary` master switch registered in `admin_settings.KNOBS` with min=0/max=1; `ai_summary_provider`/`ai_summary_base_url`/`ai_summary_model` env-var-driven `Settings` fields read by `ai_summary.current_provider`/`current_base_url`/`current_model`); SPA component `frontend/src/components/AISummary.tsx` mounted under the scan timeline in `HistoryView` streams chunks via `ReadableStream` and renders incrementally, collapsing to a "feature disabled" hint when the master switch is off and an "API key not configured" hint on a 412; default-off by virtue of `enable_ai_summary=0` so the live HF Space mirror stays byte-for-byte identical until an operator opts in; 20 new backend tests in `tests/test_ai_summary.py` cover the markdown report builder (claimed/empty/with-graph-and-phash), graph + pHash row helpers, the four provider wire-format adapters, the unknown-provider/missing-API-key guards, the SSE/NDJSON streaming-response unwrap (one async test per provider), an `httpx.MockTransport` end-to-end through `stream_summary`, the master-switch knob round-trip, and the four REST endpoints (503/412/404 plus the secrets PUT/GET/DELETE round-trip); baseline pytest **806 + 8 skipped** (default `[dev]` extra; +20 from Phase 10g, was 786 + 8 skipped at the prior 10d HEAD), mypy 38 src files clean (with `[gql]` extra so the strawberry plugin loads; the 60 graphql-only mypy errors observed without the extra are a pre-existing baseline not introduced by Phase 10g), ruff clean, frontend lint+build green, all 3 CI checks (backend / frontend / e2e Playwright) green) |
| HF Space HEAD | `d526a31` (still mirrors Phase 9 main `03aeadd` from 2026-05-07T14:45:55Z — Phase 10 sub-phases 10a / 10b / 10c / 10d / 10h are merged on `main` but have not yet been deployed to the HF Space mirror; HF Space stays on the default opt-out path until the next batch deploy because every Phase 10 capability is gated by an admin knob that defaults to 0 — flip `enable_initial_hit_pdf=1` for auto-PDF on initial CLAIMED hit (10a), flip `enable_tentative_sites=1` for the recovered ~500-site precision-trim catalog (10b), pick from the new identity types in the SPA dropdown for vk_id / ok_id / steam_id / wikimapia_uid / uidme_uguid / yelp_userid scans (10c), bump `graph_max_depth` from 1 to 2/3 with the new `graph_secondary_min_confidence` + `graph_max_total_subscans` safety rails for depth>1 recursion (10d), and use the new `txt` / `xmind` / `d3` formats on `/api/history/{id}/export.{fmt}` (10h, additive — no knob); previous deploy commits `0551938` to `5723927` (Phase 8a–8d) preserved in HF Space history) |
| Active batch | _Phase 10 — close the maigret gap (8/9 sub-phases shipped: 10a auto-PDF on initial hit · 10b tentative-sites catalog parity · 10c identity-types parity (+6) · 10d recursive graph depth >1 with conf floor · 10f reckora-cli console-script · 10g AI investigation summary · 10h XMind8 + D3-HTML + plain-text exports · 10i public OSS launch governance docs; remaining: 10e Tor / I2P proxy support)_ |
| Last batch shipped | Phase 10g — AI investigation summary with streaming markdown + runtime secrets store (feat commit `696576d` direct on `main`); new module `app/ai_summary.py` builds a markdown report from scan history + extractor fields + graph edges + pHash matches and streams a chat-completion summary through `POST /api/history/{scan_id}/ai_summary` (`StreamingResponse`); pluggable wire-format adapters for OpenAI / Anthropic / OpenRouter / Ollama (SSE for the first three, newline-delimited JSON for Ollama); new runtime secret store `app/secrets_store.py` backed by `/data/secrets.json` (atomic write, 0600 perms, in-process cache) so API keys never live in the SQLite admin DB and cannot leak through the settings export endpoint; new admin endpoints `GET /api/admin/secrets`, `PUT /api/admin/secrets/{key}`, `DELETE /api/admin/secrets/{key}` so the operator can configure the key from the SPA (values never echoed back — only a boolean `configured` flag); 4 admin knobs (`enable_ai_summary` master switch in `admin_settings.KNOBS` with min=0/max=1, plus three env-var-driven `Settings` fields `ai_summary_provider`/`ai_summary_base_url`/`ai_summary_model` read by `ai_summary.current_provider`/`current_base_url`/`current_model`); SPA component `frontend/src/components/AISummary.tsx` mounted under the scan timeline in `HistoryView` streams chunks via `ReadableStream` and renders markdown incrementally, collapsing to a "feature disabled" hint when the master switch is off and an "API key not configured" hint on a 412; default-off by `enable_ai_summary=0` so the live HF Space mirror stays byte-for-byte identical until an operator opts in; 20 new backend tests in `tests/test_ai_summary.py` cover the markdown report builder (claimed/empty/with-graph-and-phash), graph + pHash row helpers, the four provider wire-format adapters, the unknown-provider/missing-API-key guards, the SSE/NDJSON streaming-response unwrap (one async test per provider), an `httpx.MockTransport` end-to-end through `stream_summary`, the master-switch knob round-trip, and the four REST endpoints (503 when off / 412 when key missing / 404 when scan not visible / secrets PUT/GET/DELETE round-trip) — closes **Phase 10g** AI-summary gap from `reckora-vs-maigret.md`; baseline pytest 806 + 8 skipped (+20 from Phase 10g, was 786 + 8 skipped at the prior 10d HEAD), mypy 38 src files clean (with `[gql]` extra), ruff clean, frontend lint+build green, all 3 CI checks (backend / frontend / e2e Playwright) green |

| Last update | 2026-05-08 (Phase 10i — public OSS launch governance docs shipped on top of the merged Phase 10f `reckora-cli` baseline `730d61e`; new `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `CONTRIBUTING.md` (PR review flow + Phase-N runbook lifecycle + commit-message convention), `SECURITY.md` (responsible-disclosure address + 90-day fix window), `CHANGELOG.md` (one entry per Phase, generated from `feat()` / `fix()` commits), README rewrite emphasises differentiation vs maigret (anti-bot probe pipeline, Celery + Redis horizontal scale, ML reranker + drift detector, 2,506-site catalog) + links to live HF Space; existing `LICENSE` (MIT, matching `pyproject.toml`) preserved; Phase 10 progress 8/9 — 10a, 10b, 10c, 10d, 10f, 10g, 10h, 10i merged; only 10e Tor / I2P proxy support remaining; HF Space mirror still on `d526a31` pending the next deploy batch but no runtime delta from 10i so the mirror stays byte-for-byte identical) |
| Test baseline | backend pytest **806 + 8 skipped** (default `[dev]` extra; +20 from Phase 10g, was 786 + 8 skipped at the prior 10d HEAD) · mypy 38 src files clean (with `[gql]` extra so the strawberry plugin loads; the 60 graphql-only mypy errors observed without the extra are a pre-existing baseline not introduced by Phase 10g) · ruff clean · frontend lint+build green · e2e **8 / 8** |

The `Active batch` line is updated at the start of every batch. The `Repo
HEAD on main` and `Last update` lines are updated at the end of every batch
once `command git push origin main` returns and CI is green.

---

## Master checklist

> Status legend: `[ ]` planned · `[~]` in-flight · `[x]` shipped to `main`
> with green CI · `[D]` deployed to live HF Space · `[V]` verified in
> production (live click-test where applicable).

### Phase 5h — claimed precision floor & link liveness  *(complete)*

> **Goal:** the SPA's claimed bucket only contains hits the operator can
> click and immediately see a real profile — no soft-404s, no "redirect
> to homepage", no low-confidence borderline rows. Each demoted row
> moves to `UNKNOWN` (not deleted) so it stays inspectable.

- [x] **5h-A** — confidence floor knob (`min_claimed_confidence=60` default; admin-tunable)
- [x] **5h-B** — ML-confidence floor knob (`min_claimed_ml_confidence=50` default; admin-tunable)
- [x] **5h-C** — verify-required gate (`require_verify_for_claim=0` default; opt-in zero-FP mode)
- [x] **5h-D** — link-liveness probe: HEAD/GET on the human-facing display URL after every CLAIMED hit; demote on `404 / 410`, redirect-to-homepage, redirect-to-login/signup, soft-404
- [x] **5h-E** — dual-URL check when `urlProbe ≠ url`: probe URL OK but display URL fails liveness → demote with `display-url-dead`
- [x] **5h-FE** — SPA signal badges (`Link verified live` green; `Demoted (link dead)` amber) + `Stats` "Filtered (link)" / "Filtered (confidence)" counters + Playwright e2e
- [x] **5h-deploy** — HF mirror sync + Docker rebuild + health/auth verified (click-test deferred to operator with admin creds)

### Phase 6 — accuracy & gap closure vs maigret  *(complete)*

> **Goal:** quantitatively match or beat `soxoj/maigret` on every dimension where
> maigret is currently ahead (raw site count, per-site identity-type annotations).
> Make the gain measurable with a head-to-head benchmark.

- [x] **6a** — regenerate `sites.json` from `soxoj/maigret` upstream (3,154 raw → 2,506 active; auto-import `identityTypes` on 8 sites)
- [x] **6b** — engine-aware probe heuristics (XenForo / Discourse / vBulletin / uCoz) + `engine_probes.py` + 13 tests
- [x] **6c** — head-to-head benchmark vs `soxoj/maigret` CLI (`docs/benchmark-phase-6.md`)
- [x] **6d** — PDF export via weasyprint (optional `[pdf]` extra; graceful HTML fallback) + 6 tests

### Phase 7 — intel & enrichment beyond maigret  *(complete)*

> **Goal:** capabilities `soxoj/maigret` does not have. Each sub-phase opens a new
> dimension of OSINT signal.

- [x] **7a** — profile-image perceptual-hash (pHash) graph stitching (8x8 dHash via `app/imagephash.py`; admin knobs `enable_image_phash` + `image_phash_max_distance`; SPA renders dashed purple `phash_edge` between matching CLAIMED hits + `pHash matches` SidePanel stat; 24 new tests covering pure hash math, mocked HTTP fetch, SSE emission, and a Playwright e2e)
- [x] **7b** — WHOIS / HIBP-style breach enrichment for `identity_type=email` (`app/enrichment.py` runs RDAP + HIBP lookups in parallel; admin knobs `enable_email_enrichment` + `enrichment_rate_per_min`; HIBP key in env-only `enrichment_hibp_api_key`; SPA mounts `EmailEnrichmentPanel` after every list-mode email scan; 33 new tests covering parsers, rate limiter, mocked HTTP fetchers, endpoint auth/gating/rate-limit, and a Playwright e2e)
- [x] **7c** — reverse-image lookup hook (Yandex / Google Lens / TinEye) (`app/reverseimage.py` builds public reverse-search landing-page URLs; admin knob `enable_reverse_image` + 3 template settings; SPA `ReverseImageChips` renders a chip row under any CLAIMED hit whose extractor pass produced an `avatar_url`; no API key, no upstream HTTP call from the backend; 21 new tests + a Playwright e2e)
- [x] **7d** — ML reranker retrain pipeline + drift detector (`app/ml_drift.py` pure-Python two-sample Kolmogorov-Smirnov + `FeatureSummary`/`FeatureDrift`/`DriftReport` dataclasses; `app/ml_retrain.py` synchronous trainer that subprocess-runs `scripts/train_ml.py`, persists `RetrainStatus` to `app/ml/retrain_status.json` so it survives worker restarts; admin knobs `ml_drift_ks_threshold_pct` + `ml_drift_window_size`; ML scoring fan-outs into a bounded ring buffer used as the drift "current" sample; new admin endpoints `GET /api/admin/ml/status` + `POST /api/admin/ml/retrain`; SPA AdminPanel grows an `ML` tab with model age, last-retrain row, "Retrain now" button, drift summary chip and per-feature KS table; 31 new backend tests + a Playwright e2e)
- [x] **7-deploy** — HF Space mirror sync to catch up with main (8 commits `8faf4c8`→`945c6bc` rsync-mirrored to HF Space `784ac59`→`0551938`; Docker rebuild green; `GET /api/health` 200 with sites=2506; every Phase 6+7 endpoint verified reachable behind auth gate: `POST /api/enrich/email`, `POST /api/reverse-image`, `GET /api/admin/ml/status`, `POST /api/admin/ml/retrain`, `POST /api/search/permute`, `GET /api/permute/strategies` — all return 401 "login required" not 404/405, confirming new code is live; click-test deferred to operator with admin creds)

### Phase 8 — scale & infra  *(in flight)*

> **Goal:** make Reckora scale horizontally so a `10k`-site catalog and `100s`
> of concurrent operator scans don't melt a single `uvicorn` process.

- [x] **8a** — distributed worker fleet via Celery + Redis (opt-in `[scale]` extra ships `celery>=5.4` + `redis>=5.0`; `backend/app/tasks.py` defines `check_site_task` and a JSON-only Celery app with acks-late + reject-on-worker-lost for at-least-once semantics; `stream_checks` transparently dispatches to `stream_checks_via_celery` when `settings.use_celery=1`, polling each `AsyncResult` and yielding rebuilt `CheckResult` rows so the SSE pipeline is unchanged; `docker-compose.celery.yml` runs `redis:7-alpine` + 2 worker replicas + the API container for local dev; 7 new backend tests in `tests/test_tasks.py` cover dict round-trip, eager-mode task body, fan-out across multiple sites, and the `stream_checks` dispatch hook; default deployment (HF Space single container) keeps the in-process path so this batch ships the *capability* without changing the default runtime)
- [x] **8b** — rate-limit-aware proxy rotator (`backend/app/proxy_pool.py` adds a round-robin multi-proxy egress pool over the comma-separated env `RECKORA_PROXY_POOL_URLS`, with a per-URL health cooldown that skips a proxy for `proxy_health_cooldown_sec` after a transport error and falls open to direct egress when every URL is in cooldown; `backend/app/host_rate_limit.py` adds a per-host async token bucket whose refill rate is the admin-tunable `host_rate_per_min` and whose capacity matches it so a 1-minute idle window absorbs a 60-request burst on a 60/min bucket; `backend/app/transport.py` exposes `RotatingProxyTransport` — an `httpx.AsyncBaseTransport` that acquires a token, picks a proxy, dispatches via a per-proxy cached `httpx.AsyncHTTPTransport`, and marks the proxy failed on `httpx.HTTPError`; `main._new_async_client`, `tasks._run_check_site`, and the `checker.stream_checks` fallback client all switch to `transport.build_async_client()` so every outbound request goes through the new wiring; new admin knobs `enable_proxy_rotator` / `enable_host_rate_limit` / `host_rate_per_min` / `proxy_health_cooldown_sec` (all default 0 / 0 / 60 / 60) live alongside the Phase 5e single-proxy gate so a deployment with one proxy + Tor stays on 5e and only fleets opt into 8b; `/api/health` reports `proxy_pool` + `host_rate_limit` status descriptors that surface scheme-only proxy info — no credentialed URLs leak; 26 new backend tests in `tests/test_proxy_pool.py` / `tests/test_host_rate_limit.py` / `tests/test_transport.py` cover round-robin selection, env parsing, cooldown math, fail-open contract, token-bucket refill behaviour, lowercase host normalisation, and end-to-end transport dispatch through `httpx.MockTransport`)
- [x] **8c** — GraphQL gateway (alternate transport over the existing REST/SSE; new optional `[gql]` extra installs `strawberry-graphql[fastapi]>=0.235`; `backend/app/graphql_api.py` defines a Strawberry schema with read-only queries — `health`, `me`, `sites`, `permuteStrategies`, `history`, `historyItem`, `adminSettings` — and a `search` mutation that reuses `app.checker.stream_checks` to drain the SSE pipeline into a single response so a GraphQL client doesn't have to parse server-sent events; the route is mounted unconditionally at `/api/graphql` so introspection tools always work, but every resolver runs `_ensure_enabled()` first and raises a GraphQL error with extension code `DISABLED` when `enable_graphql_gateway` is 0 — keeping the gateway off-by-default without the fragility of remounting routers in response to admin edits; auth piggybacks on the same `current_member` / `current_admin` dependencies as REST so a Bearer token or `reckora_session` cookie unlocks queries identically and `adminSettings` stays admin-gated; new admin knob `enable_graphql_gateway` (min 0, max 1) lives next to the Phase 8b proxy knobs in `KNOBS`; the public `/api/health` payload gains a `graphql.{available,installed}` descriptor so a dashboard can detect whether the schema is currently serving without firing a query; 13 new backend tests in `tests/test_graphql_api.py` cover route mount, knob gate, auth gate, admin-role gate, every read-only query, and a mutation input-validation edge case; default deployment unchanged — without the `[gql]` extra the route returns 503 with a clear remediation message and `/api/health.graphql.installed` reports `false`)
- [x] **8-deploy** — HF Space mirror sync to catch up with main 34a79d1 (Phase 8a + 8b + 8c + 8d, all opt-in via admin knobs / Docker image rebuild required for [gql] extra; rsync-mirrored to HF Space `0551938` → `5723927`; Docker rebuild green; `GET /api/health` 200 with `sites=2506`, `proxy_pool.configured=0` and `host_rate_limit.enabled=false` (8b knobs default off), `graphql.installed=false` (8c [gql] extra not bundled in HF Dockerfile by design — knob check happens after install check), `share_links.enabled=false` (8d knob default off); endpoint behaviour byte-for-byte verified — `POST /api/graphql` returns 503 with remediation message, `GET /share/{token}` returns 503 ("share links disabled"), `POST /api/history/{id}/share` returns 401 ("login required") proving the auth-gated route is reachable; Phase 6+7 endpoints (`POST /api/enrich/email`, `POST /api/reverse-image`, `GET /api/admin/ml/status`, `GET /api/permute/strategies`) all still return 401 confirming no regression; `diff -rq --exclude=.git --exclude=.venv --exclude=__pycache__ /home/ubuntu/repos/Reckora /tmp/hf-space` reports 0 differences confirming byte-for-byte parity)
- [x] **8d** — public-scan edge cache (`backend/app/share_links.py` adds a `share_links` SQLite table + mint / lookup / revoke / cascade-revoke / purge primitives over a 192-bit URL-safe random token; the auth-gated `POST /api/history/{scan_id}/share` mints a token with optional `ttl_sec` capped by the admin-tunable `share_link_default_ttl_sec`, `GET /api/history/{scan_id}/shares` lists the caller's active tokens, `DELETE /api/history/{scan_id}/share/{token}` revokes one, and `DELETE /api/history/{scan_id}` cascades the revoke so a deleted scan can never be revived through a stale link; the public anonymous endpoint `GET /share/{token}` returns the same shape as the auth-gated single-scan view plus CDN-friendly cache headers — `Cache-Control: public, s-maxage=<knob>, stale-while-revalidate=<knob/2>`, an ETag derived from `sha256(token \| finished_at)[:16]`, and `Vary: accept-encoding` so a Cloudflare-class CDN can cache one entry per encoding; `If-None-Match` short-circuits to 304 with the same cache headers so a revalidating edge doesn't pay the database lookup twice; ownership is verified by reading the parent scan with the caller's username (cross-member returns 404, so a scanned target's id can't be probed for); a per-scan cap (`share_link_max_per_scan`, default 5) prevents tokens from accumulating without bound; the `enable_share_links` knob (default 0) makes every endpoint 503 until an operator opts in, and `/api/health.share_links.{enabled, cache_max_age_sec}` lets a dashboard verify the configuration without firing a query; 15 new backend tests in `tests/test_share_links.py` cover storage primitives (mint, cap, lookup, expiry, revoke, cascade, purge-expired, etag stability), HTTP knob gate (mint / list / revoke / public 503 when off), end-to-end mint → list → public read → 304 revalidation → revoke round trip, cross-member privacy guard, and parent-scan delete cascade)

### Phase 9 — anti-bot probe + scale-out reliability  *(complete)*

> **Goal:** close the residual-risk gaps that Phase 8 (proxy rotator + per-host
> rate limit) does not cover. Phase 8b spreads request load across IPs and
> across time, but it does not change the *fingerprint* of each individual
> request (Phase 9a / C1 + C2), the *coordination* of the rate-limit budget
> across N Celery workers (Phase 9b / M1), the *region* of the egress IP
> (Phase 9c / M2), the *labelling accuracy* when a site challenges with a
> CAPTCHA instead of a 429 (Phase 9d / M3), the *freshness* of the bundled
> site catalog (Phase 9e / M4), the *time + cost cap* of a long-tail scan
> (Phase 9f / M5), the *reputation curve* of a freshly-added proxy in the
> pool (Phase 9g / M6), or the *cold-start latency* of an idle HF Space
> container (Phase 9h / M7). Each sub-phase below includes the residual-
> risk ID it closes (`C1`, `M1`, etc.) so the analysis stays traceable.
>
> **All Phase 9 capabilities ship opt-in (admin knob default 0)** — same
> contract as Phase 8 — so the live HF Space stays byte-for-byte unchanged
> in default deployment.

- [x] **9a** — anti-bot probe pipeline (closes residual risk **C1** Cloudflare/Akamai/Datadome JS challenge + **C2** TLS / JA3 / JA4 fingerprint blocking, both **CRITICAL**; new module `backend/app/anti_bot_transport.py` wraps `curl-cffi` (already a dependency from Phase 1, currently used only in `chrome_tls_rescue` for a small allowlist) into an `httpx.AsyncBaseTransport` with `impersonate="chrome120"` / `impersonate="safari17"` / `impersonate="firefox120"` selectable per-host so a WAF that fingerprints TLS handshake bytes (cipher suite ordering + extension list = JA3/JA4) sees a real browser handshake instead of the Python uvloop default; new module `backend/app/playwright_fallback.py` shells out to a headless chromium (`playwright install chromium` lazy-installed at first use) with a 10s budget when a curl-cffi impersonation still returns a JS challenge HTML (`<title>Just a moment...</title>`, `<title>Attention Required! \| Cloudflare</title>`, Datadome challenge meta tag), parses the post-challenge response, and feeds it back into the regex / engine probe — limited to a configurable per-scan budget (`anti_bot_playwright_max_per_scan`, default 5) so a worst-case 2,500-site scan can't fan out 2,500 chromium spawns; new admin knobs `enable_anti_bot_pipeline` (default 0), `anti_bot_default_impersonation` (string default `"chrome120"`), `anti_bot_playwright_fallback` (default 0), `anti_bot_playwright_max_per_scan` (default 5), `anti_bot_playwright_timeout_sec` (default 10); per-site override via a new `anti_bot_profile` field in `site_overrides` so a site that needs Safari TLS while default is Chrome can be tagged individually; `app/transport.py` gains a `build_anti_bot_client()` factory that wires `RotatingProxyTransport` (Phase 8b) over `AntiBotTransport` so proxy rotation + impersonation compose correctly; `/api/health` exposes `anti_bot.{enabled, default_impersonation, playwright_fallback, playwright_chrome_installed}` so an operator can verify chromium is installed without firing a probe; expected miss-rate reduction on Cloudflare-fronted sites: 80–150 sites recovered from UNKNOWN to CLAIMED/FREE; ~25 new backend tests covering impersonation header injection, JA3 hash stability, challenge HTML detection regex, Playwright spawn budget enforcement, per-site override resolution, and the transport-stacking contract (Phase 8b proxy rotator stays on the outside, Phase 9a impersonation on the inside))
- [x] **9b** — distributed host rate limit (closes residual risk **M1** per-process token bucket; **MEDIUM**; new module `backend/app/host_rate_limit_redis.py` ships `RedisHostRateLimiter` whose acquire path is a single atomic Lua script (`HGETALL` per-second buckets → sum entries inside `[now - window + 1, now]` → on `count < limit` issue `HINCRBY` + `EXPIRE` + return `{1, 0}` allowed, else return `{0, ms_to_wait}` rejected so two workers cannot both observe `count < limit` and then both increment past it — the classic check-then-set race that a process-local Phase 8b token bucket cannot prevent across a Phase 8a Celery fan-out); `app/host_rate_limit.py` keeps the same public `acquire(host)` coroutine but dispatches to the Redis backend when `host_rate_limit_use_redis=1` and a non-empty Redis URL resolves, falling **open** to the in-memory bucket on any Redis transport error so a Redis outage stays fail-open rather than fail-closed; new admin knob `host_rate_limit_use_redis` (default 0) + env-only `host_rate_limit_redis_url` (default `""`, falls back to the Phase 8a `celery_broker_url`); NOSCRIPT recovery hooks redis-py `NoScriptError` plus `"NOSCRIPT"` / `"No matching script"` substrings so a SCRIPT FLUSH is transparent; `/api/health.host_rate_limit.backend` surfaces `"memory"` or `"redis"`; `.github/workflows/ci.yml` backend job spins a `redis:7-alpine` service container on port 6379 and exports `RECKORA_TEST_REDIS_URL` so live-Redis tests run on every push; 15 new backend tests in `tests/test_host_rate_limit_redis.py` cover the four `status()` descriptor branches, the fail-open contract, the `RedisUnavailableError` raise paths, and seven live-Redis integration tests (acquire-within-window-immediate, acquire-blocks-when-window-full, 4-worker shared-bucket contention via `asyncio.gather`, per-host bucket isolation, NOSCRIPT recovery, idempotent `aclose`, end-to-end dispatch through the public `HostRateLimiter`); 473 / 473 pytest with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL` (466 / 466 + 7 skipped with default `[dev]`), mypy 32 src files clean — feat commit `b6073af` on `main` with all three CI jobs (backend + frontend + e2e Playwright) green; HF Space stays on the default opt-out path until an operator flips `host_rate_limit_use_redis=1`)
- [x] **9c** — geo-aware proxy selection (closes residual risk **M2** round-robin pool ignores region locks; **MEDIUM**; shipped via commits `ecbebef` (feat) — region-tagged `RECKORA_PROXY_POOL_URLS` parser (`<region>:<url>` syntax decided against an httpx scheme allowlist so a typo doesn't silently swallow URLs; bare URLs tag as `region="default"` to preserve 8b behaviour byte-for-byte), three-pass `ProxyPool.acquire(preferred_region=...)` dispatch (region match → default region → any healthy URL fallback chain, collapses to Phase 8b single-pass round-robin when no preference set), 30s in-process cache for `site_overrides.<name>.preferredRegion` lookups so the request hot path never hits SQLite per call, internal `X-Reckora-Site-Name` request header set by the checker + consumed and stripped by `RotatingProxyTransport._select_proxy` so the routing label never leaks over the wire, new admin knob `enable_geo_aware_proxy` (default 0), `/api/health.proxy_pool` surfaces `geo_aware_enabled` / `geo_aware_active` / `geo_distribution`, 18 new backend tests in `tests/test_proxy_pool_geo.py`; baseline pytest 491 / 491 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 32 src files clean, all CI green — original planned bullet retained below for archival reference: ~ extend `RECKORA_PROXY_POOL_URLS` parser to accept `region:scheme://host:port` syntax (e.g. `"us-east:socks5://a.example,eu-west:socks5://b.example,ru:socks5://c.example"`) — bare URLs continue to work and are tagged `region="default"`; add a new optional `preferred_region` field to per-site catalog entries (catalog regen scripts in 9e learn this for known geo-locked sites — Yandex/VK = `"ru"`, Sina Weibo = `"cn"`, Naver = `"kr"`, etc.); selection logic in `proxy_pool.pick()` prefers a proxy whose region matches the target site's `preferred_region`, falls back to `"default"` proxies when no regional match is available, then falls back to any healthy proxy as a last resort so a single-region pool still works; new admin knob `enable_geo_aware_proxy` (default 0) so the Phase 8b round-robin behaviour is preserved unless explicitly opted in; expected miss-rate reduction: ~30–60 region-locked sites; ~10 new tests covering region-tag parser, preferred-region match, fallback chain, and the fail-open contract when no regional proxy is healthy ~)
- [x] **9d** — CAPTCHA classifier + structured `UNKNOWN` labelling (closes residual risk **M3** silent UNKNOWN on CAPTCHA challenge; **MEDIUM**; shipped via commit `18903bc` (feat) — new module `backend/app/captcha_signatures.py` ships a curated regex bank covering reCAPTCHA v2 (api2/anchor iframe src), reCAPTCHA v3 (`api.js?render=` script src), hCaptcha (`hcaptcha.com` / `js.hcaptcha.com` script src), Cloudflare Turnstile (`challenges.cloudflare.com/turnstile` script src), Datadome (`window._dd_` / `geo.captcha-delivery.com` / `datadome.co/captcha` / `dd_cookie_test_`), AWS WAF Captcha (`awswaf.com/captcha` / `aws-waf-token` / `aws-waf-captcha.js`), Arkose / FunCaptcha (`funcaptcha.com`, `client-api.arkoselabs.com`, `fc-token` field), Geetest (`static.geetest.com`, `api.geetest.com`, `initGeetest`), PerimeterX (`_pxAppId`, `px-captcha`, `client.perimeterx.net`), and a generic `<form action="...captcha...">` fallback for bespoke captcha endpoints — order matters: vendor-specific patterns are listed before the generic fallback so a hCaptcha widget that ships next to a `<form action="/captcha">` no-JS fallback never gets misclassified as `generic_form_captcha` (regression test in `tests/test_captcha_signatures.py::test_specific_vendor_wins_over_generic_form_captcha` guards this contract); `classify(body)` returns the first matching vendor name or `None`, empty bodies short-circuit so the regex tape never compiles on every HEAD probe; rolling per-vendor counter is an in-process `collections.Counter` wrapped in a `threading.Lock` (the lock guards both reads and writes because `Counter.__iadd__` is not thread-safe across concurrent probes); `vendor_counts()` returns a defensive copy so the admin JSON serialiser cannot accidentally clear the counter; new optional `CheckResult.reason: str | None = None` field defaults to `None` so all pre-9d call sites that build a `CheckResult` without `reason=` continue to work unchanged, and `to_dict()` / `check_result_from_dict()` round-trip the field across the Phase 8a Celery JSON-on-the-wire boundary; `app/checker.py` wires the classifier in inside the existing `_is_blocked_body` BLOCKED branch (line ~928): when the `enable_captcha_classifier` admin knob is on a vendor-specific match adds `body-fingerprint-block` to `signals` if it isn't already there (so vendors not in `_BLOCK_FINGERPRINTS` — Datadome / Turnstile / FunCaptcha — still trigger BLOCKED) and stamps `reason="captcha_<vendor>"` on the result, with the vendor-specific reason ALWAYS winning over the generic fingerprint when both fire (so a Cloudflare interstitial that ships hCaptcha gets `reason="captcha_hcaptcha"`); `record_vendor` is called once per BLOCKED-with-captcha result so `/api/admin/ml/status` can surface the rolling per-vendor counter without persisting analytics state to disk; new admin knob `enable_captcha_classifier` (default 0); `/api/admin/ml/status` returns the existing `model_age_days` / `last_retrain` / `drift` payload plus `captcha_vendors_seen: {vendor: count}` (empty `{}` when the knob is off, no new endpoint, no SPA breaking change); `app/graphql_api.py` `CheckResultInfo` exposes the new `reason: str | None` field and `_result_to_info` reads `payload.get("reason")` so the GraphQL transport mirrors the REST/SSE shape; 26 new backend tests in `tests/test_captcha_signatures.py` cover every vendor signature via `pytest.mark.parametrize`, the false-positive guards (empty body / footer mention of "captcha" in plain text / a profile page that mentions captcha in a help link), the specific-wins-over-generic ordering, the `known_vendors()` invariant, the counter round-trip + reset + defensive-copy contracts, the `CheckResult.reason` default + `to_dict ↔ check_result_from_dict` round-trip, the default-off byte-for-byte regression (a Turnstile body still classifies as `CLAIMED` on the 200 since Turnstile is not in `_BLOCK_FINGERPRINTS`), the knob-on Turnstile → `BLOCKED` + `reason="captcha_turnstile"` + counter increment integration, the vendor-specific override of the generic Cloudflare `body-fingerprint-block` when both fire, and the no-vendor-match path on a 429 with no body still surfacing as `BLOCKED` via `status-429` with `reason=None`; baseline pytest 510 + 7 skipped (default `[dev]` extra) / 517 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 33 src files clean, all CI green — original planned bullet retained below for archival reference: ~ new module `backend/app/captcha_signatures.py` ships a curated regex / selector signature DB for reCAPTCHA v2 (`<iframe src="https://www.google.com/recaptcha/api2/anchor`), reCAPTCHA v3 (script src match), hCaptcha (`<script src="https://hcaptcha.com/1/api.js`), Cloudflare Turnstile (`<script src="https://challenges.cloudflare.com/turnstile`), Datadome (`window._dd` script), AWS WAF Captcha, FunCaptcha, Geetest, and the generic `<form action="/captcha"` Apache module pattern; checker pipeline classifies a 200-OK response that matches a CAPTCHA signature as `UNKNOWN` with `reason="captcha_required"` (instead of the current generic `reason="anomalous_response"`) so the SPA can render a dedicated badge and the operator knows whether to flip the Phase 9a Playwright fallback knob or skip the host entirely; per-CAPTCHA-vendor counts surface in `/api/admin/ml/status` so an operator can see "47 sites returned Cloudflare Turnstile this week"; ~15 new tests covering each signature, false-positive guards (e.g. a site that mentions "captcha" in a footer link should not match), and the SPA badge rendering contract via the existing `result.reason` string ~ ; design adjustment landed during implementation: the locked-in `.devin/PHASE9.md` design decisions specified `BLOCKED` + `reason="captcha_<vendor>"` rather than the original `UNKNOWN` + `reason="captcha_required"` shape — the vendor-specific reason gives the SPA a more useful badge, and reusing the existing BLOCKED status keeps the Phase 2.2 cffi rescue path and downstream scan stats untouched)
- [x] **9e** — catalog auto-refresh from upstream maigret (closes residual risk **M4** ~5%/year drift in bundled catalog; **MEDIUM**; shipped via commit `ac9bbdf` (feat) — new module `backend/scripts/refresh_catalog.py` ships a pure-function diff classifier (no network, fully unit-testable) that maps every (old, new) pair into one of `{DELTA_ADDED, DELTA_REMOVED, DELTA_URL_CHANGED, DELTA_IDENTITY_TYPE_CHANGED, DELTA_ENGINE_HINT_CHANGED}` with `url_changed → identity_type_changed → engine_hint_changed` precedence so the most-disruptive category surfaces in the diff report when multiple categories apply; `infer_identity_type(entry)` picks the most-specific tag from a curated priority list (`dating > gaming > porn > forum > music > video > art > shopping > news > tech > blog > social`) so a XenForo board flipping upstream from `social` → `forum` shows up as `identityType_changed` rather than as a no-op tag rename; `merge_into_candidate(upstream, current)` builds a brand-new `sites` dict that takes upstream-controlled fields verbatim but preserves `RECKORA_OVERRIDE_FIELDS = {"prewarm", "ignore403", "requestHeadOnly", "identityTypes"}` so a quarterly refresh never undoes Phase 5h cookie-prewarm tags / Phase 6 trim flags / per-site identityType overrides; `render_markdown_report(diff, *, old_count, new_count, upstream_tag, sample_limit)` emits a deterministic PR-ready markdown body with a header, summary table, and per-kind sections truncated to `sample_limit` (default 20) entries with a `… and N more` marker so a 500-row diff doesn't bloat the PR body; CLI `main(argv)` reads either the Reckora envelope (`{source, license, count, sites}`) shape or a raw `sites` dict, writes the candidate JSON with envelope preserved, and writes the markdown report to caller-supplied paths; new GitHub Actions workflow `.github/workflows/catalog-regen.yml` (cron `0 5 1 */3 *` = 05:00 UTC on the first day of every quarter) resolves the latest `soxoj/maigret` tag via the GitHub releases API, downloads `maigret/resources/data.json` from that tag, runs `scripts/build_sites.py` to convert upstream → Reckora envelope shape, runs `backend/scripts/refresh_catalog.py` to diff + emit candidate + report, and opens a PR titled `chore(catalog): quarterly refresh from soxoj/maigret <tag>` with the markdown report inlined as the PR body; `workflow_dispatch` trigger so a maintainer can fire an ad-hoc refresh from the GitHub UI without waiting for the next quarter boundary; the workflow exits 0 silently when the candidate is byte-equivalent to the current bundled catalog (no spurious empty PRs); 24 new backend tests in `tests/test_catalog_refresh.py` cover `infer_identity_type` priority + defensive `None` handling, `classify_delta` ordering + each kind boundary case (including engine added / engine removed), `diff_catalogs` aggregation + sort + `total_changes` invariant + `by_kind()` always exposing the full set, `merge_into_candidate` override preservation + brand-new entries not inheriting overrides + fresh-dict-per-call invariant, `render_markdown_report` summary table + section truncation + `… and N more` marker, the CLI integration on both envelope and raw shapes, `_read_sites` rejecting non-mapping top-level JSON, and the module-level constant invariants (`DELTA_KINDS` covers the five named constants in the documented order; the override field set matches the module contract); baseline pytest 534 + 7 skipped (default `[dev]` extra) / 541 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 33 src files clean (the new script lives in `scripts/` which is intentionally outside the `files = ["app"]` mypy gate, matching the precedent set by `scripts/train_ml.py` and `scripts/build_sites.py`); HF Space stays on the default opt-out path because Phase 9e adds no runtime feature, no admin knob, no app code path — the diff classifier and renderer are imported only by the cron job and by the unit tests, and the bundled `sites.json` is unchanged in this commit (byte-for-byte identical to `18903bc`); original planned bullet retained below for archival reference: ~ new script `scripts/regen-from-maigret.py` clones the latest `soxoj/maigret` release tag into a temp dir, parses `maigret/resources/data.json`, diffs against `backend/app/sites_data.json`, classifies each delta as one of `{added, removed, url_changed, identityType_changed, engine_hint_changed}`, and writes a structured PR-ready markdown report plus the regenerated `sites_data.json` candidate; new GitHub Action workflow `.github/workflows/catalog-regen.yml` (cron `0 5 1 */3 *` = first day of every quarter) runs the script, opens a PR titled `chore(catalog): quarterly refresh from soxoj/maigret <tag>` with the diff report inlined, and pings the maintainer; the regen pipeline preserves Reckora-specific fields (`engine_hints`, `identityTypes` overrides, Phase 6 trim flags) so a quarterly catalog refresh doesn't undo Phase 6 precision wins; expected impact: drift drops from ~5%/year to ≤1%/quarter; no runtime feature, no admin knob, no test surface beyond the script's own unit tests for the diff classifier (~8 tests) ~ ; design adjustment landed during implementation: the script is named `backend/scripts/refresh_catalog.py` (matching the §4 file-touch map in `.devin/PHASE9.md`) rather than `scripts/regen-from-maigret.py` since `backend/scripts/` already houses `build_sites.py` / `train_ml.py` / `bench_vs_maigret.py` and the regen pipeline composes naturally with the existing `build_sites.py`; the test count ratcheted from "~8 tests" to 24 tests because the merge logic and markdown renderer needed coverage too; the workflow uses `gh pr create --body-file` rather than embedding the report in a JSON payload because the markdown is too large for a shell-quoted `--body` argument)
- [x] **9f** — per-scan hard budget + `SKIPPED` bucket (closes residual risk **M5** scan tail-latency runaway; **MEDIUM**; shipped via commit `7dd7bfd` (feat) — new module `backend/app/scan_budget.py` introduces a per-scan `ScanBudget` dataclass with three reason constants exposed at module scope (`REASON_SCAN_TOTAL_KILLED` / `REASON_BUDGET_EXCEEDED` / `REASON_PROBE_TIMEOUT`) plus a `REASONS` `frozenset` for downstream iteration; `from_settings(settings)` reads four admin knobs (`enable_scan_budget` master switch + `scan_max_total_sec` / `scan_per_site_timeout_sec` / `scan_max_concurrent_skips` thresholds), `is_enabled` + `per_site_timeout` properties (the latter is `float | None` so callers can pass it directly to `asyncio.wait_for` — `None` = "no timeout" matches the disabled-path no-op contract), `elapsed_sec(now=None)` + `is_total_exceeded(now=None)` clock helpers using `time.monotonic()`, `record_skip()` ticks consecutive + total counters and *latches* `_bulk_skip_armed` once the threshold is hit, `record_success()` resets consecutive but does NOT disarm the bulk-skip latch (the bulk-skip is one-way — once we've concluded the network is wedged, the rest of the scan is skipped; the operator re-runs SKIPPED rows in a second scan instead of letting the first one limp through), `force_bulk_skip()` arms the latch without ticking when the *total* wall-clock budget is exceeded, and `stats()` returns a serializable snapshot (the `threading.Lock` is acquired once for the whole snapshot so consumers never see a torn state); `app/checker.py::stream_checks` instantiates the budget once per scan inside the in-process worker fan-out (after the Phase 8a Celery dispatch branch, before the `asyncio.Queue` construction) so when `enable_scan_budget=0` (the default) the returned budget's `is_enabled` is `False` and every gating method short-circuits to `False`/`None` — the worker walk stays byte-for-byte identical to the pre-9f path; when on, the `worker(site)` coroutine short-circuits before the `check_site` call: if `budget.is_total_exceeded()` it forces the bulk-skip latch and emits SKIPPED with `reason="scan_total_killed"`; else if `budget.should_bulk_skip()` (latched after the consecutive-timeout threshold) it emits SKIPPED with `reason="budget_exceeded"`; otherwise it wraps the `check_site` call in `asyncio.wait_for(..., timeout=budget.per_site_timeout)` and on `asyncio.TimeoutError` calls `budget.record_skip()` (which may latch the bulk-skip) and emits SKIPPED with `reason="probe_timeout"` — on success `budget.record_success()` clears the consecutive-skip counter, on a non-timeout exception the worker still calls `record_success()` (the network isn't wedged, the site just errored) and emits the existing `Status.ERROR` frame; `_make_skipped(site, reason)` helper produces a consistent `CheckResult(status=Status.SKIPPED, reason=reason)` shape with the same `url` substitution + `url_main` + `tags` every other terminal status uses; four new admin knobs in `app/config.py` (`enable_scan_budget=0`, `scan_max_total_sec=300`, `scan_per_site_timeout_sec=8`, `scan_max_concurrent_skips=50`) with corresponding `KnobSpec` entries in `app/admin_settings.py` (range validation: master switch 0-1, total cap 1-3600 = max 1 hour, per-site cap 1-120 = max 2 min, bulk-skip threshold 1-2500 = max matches catalog size); 17 new backend tests in `tests/test_scan_budget.py` cover the `REASONS` `frozenset` shape + each constant's literal string value, `from_settings` defaults to disabled with safe caps + every gating method as a no-op when disabled, `from_settings` reads all four knobs when enabled, `record_skip` ticks consecutive + total counters, `record_success` resets consecutive but keeps total + total_probes, `should_bulk_skip` latches at threshold and stays armed across subsequent `record_success` calls (the one-way trip invariant), `force_bulk_skip` arms without ticking the counters, `is_total_exceeded` uses the monotonic clock with proper `>=` boundary at the threshold edge, `elapsed_sec` returns floating seconds, `stats` snapshot includes all documented keys, end-to-end `stream_checks` integration regression that the default-off path emits no SKIPPED rows and `CheckResult.reason` stays `None` (the byte-for-byte compatibility regression guard), per-probe timeout emits a single SKIPPED with `reason="probe_timeout"`, low-threshold bulk-skip latches on a 5-site scan with threshold=2 (first 2 emit `probe_timeout`, remaining 3 emit `budget_exceeded`), total-budget kill path emits SKIPPED with `reason="scan_total_killed"` (or `budget_exceeded` if the bulk-skip latch fires first), `CheckResult.reason` round-trips through `to_dict`, and the `Settings` singleton + `ALLOWED_KEYS` expose all four knobs; baseline pytest 551 + 7 skipped (default `[dev]` extra) / 558 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 33 → 34 src files clean (`app/scan_budget.py` joins the gate); HF Space stays on the default opt-out path because Phase 9f adds no runtime behaviour change when `enable_scan_budget=0` (the default) — the `ScanBudget` is still instantiated once per scan via `from_settings` but its `is_enabled` is `False` so every gating method short-circuits, the per-probe `asyncio.wait_for` wrapper falls through to the bare `check_site` call, and no SKIPPED rows are emitted; original planned bullet retained below for archival reference: ~ replace the existing `scan_timeout_sec` (which only caps total elapsed) with a triple-budget model: `scan_max_total_sec` (hard kill — 300s default), `scan_per_site_timeout_sec` (per-probe budget — 8s default, separate from `httpx` connect/read timeouts), and `scan_max_concurrent_skips` (after this many sequential probe-budget exceedances the remaining sites are bulk-marked SKIPPED — 50 default); new bucket `SKIPPED` next to CLAIMED / FREE / BLOCKED / UNKNOWN with `reason="budget_exceeded" \| "scan_total_killed"` so an operator can re-run only the SKIPPED rows instead of the whole scan; SPA gets a `Stats` panel `Filtered (budget)` counter; ~10 new tests covering the cumulative-budget bookkeeping, the bulk-skip threshold path, and the SSE `event: skipped` frame format ~ ; design adjustment landed during implementation: the four-knob model added a master switch (`enable_scan_budget`) on top of the original three thresholds so the default-off contract is byte-for-byte exact rather than relying on individual knob values being "high enough to never fire"; the test count ratcheted from "~10 tests" to 17 tests because the integration scenarios needed three separate paths (per-probe timeout, threshold latch, total-budget kill) each with a regression guard; the SPA stats panel + SSE `event: skipped` frame format is deferred to the SPA polish iteration — the backend already emits `Status.SKIPPED` with the structured `reason` field on the existing SSE channel so an SPA consumer can filter without a new event type)
- [x] **9g** — proxy reputation warm-up + weighted selection (closes residual risk **M6** fresh proxy = poor WAF reputation; **MEDIUM**; shipped via commit `2d5f4ad` (feat) — new module `backend/app/proxy_reputation.py` ships a thread-safe `ProxyReputation` singleton: each tracked URL has `successes` / `failures` integer counters and a `first_seen` timestamp (`time.monotonic()`, never wall-clock so a system clock jump can't retroactively flip a fresh proxy out of warm-up); `record_success(url)` / `record_failure(url)` are O(1) under a single `threading.Lock` (acquired once per probe end, never held across an `await`, so the asyncio fan-out can't observe a torn counter); `is_enabled()` reads the `Settings.enable_proxy_reputation` knob via the same `get_settings()` accessor every other Phase 9 module uses so the import gate is consistent and admin knob hot-reload is honoured; `weight(url, *, now=None)` returns `1.0` for any URL still inside the warm-up window (`now - first_seen < proxy_warmup_window_days * 86400`) so a freshly-added proxy gets full weight regardless of whether its first probe failed (the warm-up exists precisely so a fresh proxy can collect samples without being instantly de-prioritised by one bad request), and `successes / (successes + failures)` for any URL past warm-up (with the floor at 0.0 when both counters are zero, never a `ZeroDivisionError`); `pick_weighted(urls, *, now=None)` runs a stride-scheduling algorithm — each URL gets a virtual time cursor `_virt_time[url]` and a stride `1 / weight`, the pick advances the URL with the smallest virtual time and bumps it by its stride — which is deterministic, collapses to strict round-robin when all weights are equal (the default round-robin invariant of Phase 8b is preserved exactly when `enable_proxy_reputation=1` but every URL has the same recorded ratio), and biases toward high-weight URLs in proportion to their weight without ever starving a low-weight URL (a 0.1-weight proxy still gets one pick per ten 1.0-weight picks, so a slumping proxy can recover when the WAF cools down); `reset_for_tests()` helper clears state; `app/proxy_pool.py` is extended with `_acquire_weighted(preferred_region)` which iterates the same three-pass region tier walk Phase 9c introduced (region match → default region → any healthy URL fallback chain) but, inside each tier, calls `proxy_reputation.pick_weighted` over the cooldown-eligible URLs in that tier rather than walking the round-robin index — the cooldown latch from Phase 8b is preserved on top (a URL whose `cooldown_until` is still in the future is excluded from the candidate list before `pick_weighted` runs, so a recently-failed URL still gets its short rest before being eligible to win the weighted pick); `acquire(preferred_region=None)` gates the entire weighted path on `proxy_reputation.is_enabled()` so when `enable_proxy_reputation=0` (the default) the call falls through to the pre-9g `_acquire_round_robin(preferred_region)` byte-for-byte; `mark_failed(url)` and `mark_success(url)` feed the reputation tracker via `proxy_reputation.record_failure` / `record_success` only when the master switch is on so the disabled path adds zero counter mutations on the hot path; two new admin knobs in `app/config.py` (`enable_proxy_reputation=0` master switch + `proxy_warmup_window_days=7` warm-up cap, range 0–365) with corresponding `KnobSpec` entries in `app/admin_settings.py` (range validation: master switch 0–1, warm-up window 0–365 = max one year); 28 new backend tests in `tests/test_proxy_reputation.py` (the 22 test functions expand to 28 collected cases via `pytest.mark.parametrize`) cover `is_enabled` reading the knob with hot-reload, `record_success` / `record_failure` counter invariants, `weight` returning `1.0` during warm-up regardless of recorded outcome, `weight` returning the ratio after warm-up with a 0.0 floor on the empty-counters case (no `ZeroDivisionError`), `weight` honouring `proxy_warmup_window_days=0` to disable warm-up entirely so every URL is ranked by recorded ratio from the first probe, `pick_weighted` deterministic stride scheduling collapsing to round-robin when all weights are equal, `pick_weighted` biasing to high-weight URLs in proportion to weight without starving low-weight URLs, `pick_weighted` raising on an empty candidate list (operator gating contract), `ProxyPool._acquire_weighted` honouring the Phase 9c three-pass region tier walk inside the weighted pick, the cooldown latch composition (a recently-failed URL is excluded from the candidate list before `pick_weighted` runs even when its weight would otherwise dominate), the default-off byte-for-byte regression guard (a `ProxyPool` with `enable_proxy_reputation=0` and three URLs walks them in strict round-robin order across 9 acquires regardless of induced failures, identical to the Phase 8b/9c pre-9g walk), `mark_failed` / `mark_success` feeding the reputation tracker only when the master switch is on, the `Settings` singleton + `ALLOWED_KEYS` exposing both new knobs, and the `KnobSpec` registry round-trip; baseline pytest 579 + 7 skipped (default `[dev]` extra) / 586 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 34 → 35 src files clean (`app/proxy_reputation.py` joins the gate); HF Space stays on the default opt-out path because Phase 9g adds no runtime behaviour change when `enable_proxy_reputation=0` (the default) — the `ProxyReputation` singleton is allocated lazily but every gating method short-circuits, the weighted pick path is bypassed entirely, and the existing Phase 8b/9c round-robin walk is preserved byte-for-byte; original planned bullet retained below for archival reference: ~ extend `app/proxy_pool.py` with per-proxy `success_count` / `failure_count` rolling counters (sliding window of last 100 requests per proxy, in-memory only — no Redis dependency), expose a `weight = success_count / max(1, success_count + failure_count)` ratio, and bias `pool.pick()` to prefer high-weight proxies once each proxy has accumulated ≥ `proxy_warmup_min_requests` (default 50) requests so a freshly-added proxy still gets routed traffic to build reputation but a known-bad proxy is naturally de-prioritised; new admin knob `enable_proxy_pool_weighted_selection` (default 0) — round-robin behaviour preserved when knob is off; new admin knob `proxy_warmup_min_requests` (default 50); `/api/health.proxy_pool.members[].weight` exposes per-proxy weight so an operator can see "proxy A 0.94 / proxy B 0.32 — proxy B is dragging the pool down"; ~10 new tests covering weight calculation, warm-up window enforcement, weighted-random selection statistical fairness, and the round-robin fallback when the knob is off ~ ; design adjustments landed during implementation: the warm-up signal is a *time window* (`proxy_warmup_window_days`) rather than a *minimum-request count* (`proxy_warmup_min_requests`) so the warm-up duration is operator-predictable from the day a proxy is added rather than depending on how heavily that proxy gets probed (a proxy added on a quiet day would otherwise stay in warm-up artificially long under the request-count model); the weighted selection algorithm is deterministic stride scheduling rather than weighted-random because stride scheduling collapses to exact round-robin when all weights are equal (preserving the Phase 8b/9c round-robin byte-for-byte regression contract) whereas weighted-random would only converge to round-robin in expectation; the `enable_proxy_pool_weighted_selection` knob name was simplified to `enable_proxy_reputation` because the master switch gates *all* reputation-tracker behaviour (counter mutations, weight calculation, weighted pick) rather than just the selection step; the test count ratcheted from "~10 tests" to 28 collected cases (22 functions × parametrize) because the integration scenarios needed three separate paths (warm-up math, stride scheduling, region tier composition) each with a regression guard plus the default-off byte-for-byte invariant)
- [x] **9h** — HF Space keep-warm cron (closes residual risk **M7** cold-start latency; **MEDIUM-LOW**; shipped via feat commit `ea763c2` (merge `03aeadd`, PR #4) — new GitHub Action workflow `.github/workflows/hf-keep-warm.yml` (cron `*/30 * * * *`) hits `GET https://yesmyyt-cacadoxx.hf.space/api/health` every 30 minutes so the HF Space free-tier container stays warm and a returning user hits a sub-second response instead of the 30–45s Docker boot; new pure-stdlib script `backend/scripts/hf_space_keep_warm.py` (`urllib.request` only — no httpx/requests dependency so the GH Actions runner doesn't need Reckora's runtime extras) with configurable retry budget + exponential backoff capped at 8s + injectable `opener` / `sleep` / `clock` for deterministic tests; 28 new backend tests in `tests/test_hf_space_keep_warm.py`; baseline pytest 607 + 7 skipped (default `[dev]` extra) / 614 with `[scale]` + `[gql]` + `RECKORA_TEST_REDIS_URL`, mypy 35 src files clean (script lives in `scripts/` outside the `app/` mypy gate, matching `scripts/refresh_catalog.py` from Phase 9e); `workflow_dispatch` trigger so a maintainer can fire an ad-hoc warm-up from the GitHub UI; alternative migrate to HF Spaces "Always On" tier when budget allows — left as an inline comment in the workflow for the operator; CI for `03aeadd` (backend + frontend + e2e) green; HF Space stays on the default opt-out path because Phase 9h adds no runtime feature, no admin knob, no app code path — the script is invoked exclusively by the cron workflow and never imported at runtime)

### Phase 10 — close the maigret gap  *(in flight)*

> **Goal:** close the eight known accuracy / surface gaps Reckora has
> versus `soxoj/maigret` plus the operator-flagged UX gap (auto-PDF on
> initial target hit). Each sub-phase ships behind a `RECKORA_*` admin
> knob defaulted to **0** so existing HF Space deployments see no
> behaviour change until an operator flips the switch — same opt-out
> contract as Phase 8 + Phase 9. Sub-phases ordered by impact × ship
> effort, smallest/highest-value first. See
> [`PHASE10-ROADMAP.md`](./PHASE10-ROADMAP.md) for the full per-sub-phase
> design + deliverables map.

- [x] **10a** — auto-PDF preliminary export on initial CLAIMED target hit (closes UX gap "operator wants a dossier within seconds of the first concrete signal, not after the full 5+ minute scan"; shipped via feat commit `ed8bc7b`, merge `976f4f1` (PR #7) on `main`; new module `backend/app/initial_hit.py` ships an `InitialHitTracker` whose `consider(result)` latches `_fired = True` the first time a `CheckResult` satisfies all of `status == Status.CLAIMED` + `confidence >= initial_hit_min_confidence` (default 80, knob-tunable) + `verified is True` (verify-pass agreed; gates against the false-positive class) + the result's parent is the original primary target (not a Phase 5d secondary spawn), then schedules an async preliminary PDF render via `exports.to_pdf(partial_scan, results_so_far)` persisted to `/data/initial_pdfs/{scan_id}.pdf` with a 24h TTL janitor that wakes every 60min; new REST endpoint `GET /api/history/{scan_id}/initial_pdf` returns the persisted preliminary PDF with `Content-Disposition: attachment` (404 until the latch fires); SPA `frontend/src/components/Stats.tsx` listens for the new `initial_pdf_ready` SSE frame and renders a banner *"Initial target hit on {site} (confidence {N}). [Download preliminary PDF]"* until the full-scan PDF supersedes it on `done`; 5 new admin knobs (`enable_initial_hit_pdf` master switch default 0, `initial_hit_min_confidence` default 80, `initial_hit_require_verify` default 1, `initial_hit_pdf_ttl_sec` default 86400, `initial_hit_include_extras` default 1); 12 new backend tests covering the latch-fires-once invariant, the four gating predicates each rejecting independently, the persist + read-back round-trip, the TTL purge contract, the SSE frame shape, and all five knobs round-tripping via PATCH `/api/admin/settings`; default-off by `enable_initial_hit_pdf=0` so the live HF Space byte-for-byte mirror of Phase 9 stays unchanged in default deployment)
- [x] **10b** — tentative-sites catalog parity (closes catalog gap "Reckora 2,506 vs maigret 3,000+ — most of the gap is sites Reckora's `scripts/build_sites.py` quality gate trimmed"; shipped via feat commit `5d57458`, merge `737c6f0` (PR #10) on `main`; `scripts/build_sites.py` now sets `tentative=true` + a structured `tentative_reason` field (`returns_200_on_any_path` / `requires_login_for_classify` / etc.) on entries that fail the quality gate instead of dropping them, so the bundled catalog default-includes the recovered ~500-site precision-trim batch; `app/sites.py` `Site` dataclass gained a `tentative: bool = False` field with the `"tentative"` tag auto-applied to tentative sites; `app/checker.py` integration caps the rule-based confidence at `max_tentative_confidence` (default 70, knob-tunable) for tentative-flagged sites so a tentative CLAIMED never crosses the Phase 5h precision floor unless ML + verify both agree strongly, and auto-emits SKIPPED before probe when the master switch is off so the wire-on-the-default-path stays unchanged; SPA `TagFilter.tsx` exposes a `"tentative"` checkbox (default unchecked) plus a Stats counter `+N tentative sites available` when the filter is off; 2 new admin knobs (`enable_tentative_sites` master switch default 0; `max_tentative_confidence` range 0–100 default 70); ~15 new backend tests in `tests/test_tentative_sites.py` cover tentative-flag classification, confidence cap, tag filtering, and the SKIPPED-when-disabled path; default-off by `enable_tentative_sites=0` so the existing Phase 6 catalog walk is preserved byte-for-byte)
- [x] **10c** — identity-types parity (+6) (closes identity-type gap "maigret has 12 types, Reckora has 6"; shipped via feat commit `842572e`, merge `a3200dd` (PR #8) on `main`; `app/identity.py` extends the `IdentityType` Literal from 6 to 12 entries with the six new types `vk_id` / `ok_id` / `steam_id` / `wikimapia_uid` / `uidme_uguid` / `yelp_userid`, each with a per-type validator (VK numeric ID 1–11 digits, OK numeric ID 9–18 digits, Steam ID 17-digit or `STEAM_X:Y:Z` legacy format, Wikimapia UID numeric, UidMe UUID, Yelp user ID alphanumeric); `IDENTITY_TYPE_LABELS`, `_VALIDATORS`, `KNOWN_IDENTITY_OVERRIDES` tables extended in tandem; `app/sites.py` site catalog auto-tags VK / OK / Steam / Wikimapia / UidMe / Yelp sites with the new identity types via curated map; SPA `frontend/src/types.ts` mirrors the new Literal + label map so the identity picker dropdown auto-extends; 12 new backend tests covering validator accept/reject + URL substitution per type; zero behaviour change for existing username / email / phone scans — new types only fire when an operator explicitly picks them in the SPA dropdown, so the default-off invariant is satisfied by construction)
- [x] **10d** — recursive graph depth >1 with stricter secondary confidence floor (closes recursion-depth gap "maigret runs unbounded recursion via `socid_extractor`, Reckora's depth-1 cap at 80 sub-scans is conservative"; shipped via feat commit `c2b527f`, merge `ed6093e` (PR #11) on `main`; bumped `graph_max_depth` knob range from `(0, 1)` to `(0, 3)` so the operator can run depth-2 and depth-3 recursion when an investigation needs it; two new safety-rail knobs `graph_secondary_min_confidence` (range 50–100, default 80 — secondary scans must clear this floor before spawning tertiary scans, stricter than Phase 5h's 60 because deep secondaries are the most-noisy edge of the graph) and `graph_max_total_subscans` (range 50–500, default 200 — hard cap on the cumulative fanout across the entire graph search regardless of depth, prevents exponential blow-up at depth-3 worst case); `app/main.py::_graph_search_stream` refactored from the Phase 5h hard-coded depth-1 path into a BFS loop over generations 1..N with cumulative `subscan_count` tracking + cap enforcement (a SKIPPED row with `reason="graph_subscan_cap"` is emitted when the cap fires) and a stricter per-generation conf-floor gate so a CLAIMED secondary hit only spawns the next generation when its confidence clears `graph_secondary_min_confidence`; new `depth` field added to `extracted` / `edge` / `secondary_start` / `result` SSE events plus three new `done` payload fields (`capped_subscans`, `max_depth_reached`, `subscans_run`) so the SPA can render a "max depth=N, +K skipped (cap)" badge; 13 new backend tests in `tests/test_recursive_graph_depth.py` cover depth-2 + depth-3 fanout with multiple seed hits, secondary confidence floor demotion, subscan cap enforcement at candidate-enqueue time (uses realistic Twitter-style social URLs so `find_secondary_usernames()` extraction trips on the right `_SOCIAL_URL_PATTERNS` regex bank rather than on a synthetic example.com URL), the knob round-trip via PATCH `/api/admin/settings`, and the existing depth-1 behaviour byte-for-byte regression guard; default-off by virtue of `graph_max_depth=1` so the existing Phase 5h depth-1 walk is preserved byte-for-byte for callers that never bump the depth knob — pair with the Phase 9f scan-budget knob when running depth-3 since the worst-case 200 sub-scans × 8s/site = ~30 min wall-clock)
- [ ] **10e** — native Tor / I2P proxy support (closes Tor / I2P gap "Reckora cannot scan `.onion` or `.i2p` sites natively, requires an external proxy gateway"; new module `app/onion_transport.py` is an `httpx` transport adapter that routes via the local Tor SOCKS gateway (default `socks5://127.0.0.1:9050`) when a site is tagged `tor_only=true`, plus an I2P HTTP proxy adapter for `.i2p` sites; sites catalog gains ~50 known `.onion` sites (Twitter onion, BBC onion, etc.); 4 new admin knobs `enable_tor_proxy` (default 0), `tor_socks_url` (default `socks5://127.0.0.1:9050`), `enable_i2p_proxy` (default 0), `i2p_http_url` (default `http://127.0.0.1:4444`); new optional `Dockerfile.tor` build target bundles the Tor daemon so HF Space can flip onto the Tor-enabled image when an operator requests it; ~10 new tests covering transport selection, SOCKS handshake mock, fail-fast when daemon unreachable; pair with relaxed `scan_per_site_timeout_sec` (e.g. 30s) to absorb the 200–800ms Tor hop overhead + 1–5s p99 onion-site latency)
- [x] **10f** — `reckora-cli` console-script (closes CLI-mode gap "Reckora is server-only, maigret ships as a one-liner CLI"; new package layout `backend/app/cli/__init__.py` exposes the public Python API `async def scan(username, *, identity_type, tags, limit, include_tentative, catalog, verify, extract) -> AsyncIterator[CheckResult]` plus `build_scan_summary` / `render_report` / `select_sites` / `ensure_admin_db` / `load_default_catalog`; `backend/app/cli/__main__.py` ships an argparse front-end (`reckora username --html out.html --pdf out.pdf --json out.json --tags photo,dating`) with `--list-tags`, `--list-identity-types`, `--include-tentative`, `--no-verify`, `--no-extract`, `--admin-db PATH`, `--print-json`, `--quiet` knobs; `pyproject.toml` adds a `[cli]` extra (deliberately empty — argparse is stdlib, every other dep is reused from the base install) and a `[project.scripts]` entry `reckora = "app.cli.__main__:main"`; CLI reuses `app.checker` / `app.sites` / `app.exports` / `app.identity` / `app.transport` so the wrapper stays thin (no re-implementation of probe logic, anti-bot transport, or proxy rotation); stdout streaming progress (one-line per probed site to stderr, mimicking maigret's alive_progress) + reports written via `app.exports.render` for every documented format (csv, json, markdown, html, pdf, txt, xmind, d3); 30 new tests in `tests/test_cli.py` cover argparse defaults + every export-format flag, identity-type / limit / verify / extract toggles, tag CSV parsing, catalog projection (tag + identity-type + tentative-opt-in), `build_scan_summary` status counts, JSON / CSV round-trip plus every documented format, async `scan()` validation + stub-driven yield, exit codes (`0` claimed / `2` argv / `3` no-hits / `4` no-sites-matched / `130` SIGINT) and the cancel-token short-circuit, `--list-tags` / `--list-identity-types` admin commands, the SIGINT handler restoration after a successful run, and the `ensure_admin_db` tempfile / explicit-path round-trip; CLI mode skips SQLite for non-essential subsystems (no scan history, no share links) — knobs come from `RECKORA_*` env vars + CLI flags only, with an `--admin-db /path/to/reckora.db` flag for parity with the host's admin overrides; default boots an empty schema-valid tempfile DB so `admin_settings.effective_int` falls through cleanly to env-var defaults without raising `OperationalError`)
- [x] **10g** — AI investigation summary (closes AI-summary gap "maigret's `--ai` feature turns raw scan findings into a short investigation summary using an OpenAI-compatible chat API"; shipped via feat commit `696576d` direct on `main`; new module `app/ai_summary.py` ships `async def summarize_scan(scan_id, *, model, base_url, api_key, member, ...) -> str` plus a separate `async def stream_summary(...) -> AsyncIterator[str]` so the REST endpoint can forward chunks live without buffering — the markdown report builder draws from scan history + extractor fields + graph edges + pHash matches and the wire-format adapters cover OpenAI / Anthropic / OpenRouter (chat-completions SSE) and local Ollama (newline-delimited JSON); new REST endpoint `POST /api/history/{scan_id}/ai_summary` returns a `StreamingResponse` and is gated by the `enable_ai_summary` master switch (503 when off), the `ai_summary_api_key` secret (412 Precondition Failed when missing instead of an opaque 500), and the caller's history visibility (404 for scans they cannot see); new runtime secret store `app/secrets_store.py` backed by `/data/secrets.json` (atomic write via `os.replace`, `0600` perms, in-process cache with `_reset_cache_for_tests` for tests) — API keys deliberately do NOT live in the SQLite admin DB so they cannot leak through the existing settings export endpoint; new admin endpoints `GET /api/admin/secrets`, `PUT /api/admin/secrets/{key}`, `DELETE /api/admin/secrets/{key}` so an operator can configure the AI summary API key from the SPA without shelling onto the box (values are returned only as a boolean `configured` flag — never echoed back); 4 admin knobs total — `enable_ai_summary` is registered as an int knob in `admin_settings.KNOBS` (min=0/max=1) so an operator can flip it at runtime via PATCH `/api/admin/settings` without a redeploy, while `ai_summary_provider` / `ai_summary_base_url` / `ai_summary_model` live as string-typed `Settings` fields (the admin DB stays int-only by design) and are env-var driven, read by `ai_summary.current_provider` / `current_base_url` / `current_model`; SPA component `frontend/src/components/AISummary.tsx` mounted under the scan timeline in `HistoryView` streams markdown chunks via the `ReadableStream` API and renders them incrementally, collapsing to a "feature disabled" hint when the master switch is off and an "API key not configured" hint on a 412; default-off by virtue of `enable_ai_summary=0` so the live HF Space mirror stays byte-for-byte identical until an operator opts in; 20 new backend tests in `tests/test_ai_summary.py` cover the markdown report builder (claimed-table render / empty-rows path / with-graph-and-phash sections), the graph-edges + phash-matches helpers (rows-without-parent skipped; rows-without-avatar-hash empty), the four provider wire-format adapters (OpenAI chat-completions endpoint, OpenRouter referrer headers, Anthropic messages endpoint with `x-api-key`, Ollama no-API-key path), the unknown-provider + missing-API-key guards, the SSE / NDJSON streaming-response unwrap (one async test per provider), an `httpx.MockTransport` end-to-end through `stream_summary`, the master-switch knob round-trip via PATCH, and the four REST endpoints (503 when master switch off / 412 when API key missing / 404 when scan not visible / secrets PUT-GET-DELETE round-trip with unknown-key 404 guard); baseline pytest 806 + 8 skipped (default `[dev]` extra; +20 from Phase 10g, was 786 + 8 skipped at the prior 10d HEAD), mypy 38 src files clean (with `[gql]` extra so the strawberry plugin loads), ruff clean, frontend lint+build green, all 3 CI checks (backend / frontend / e2e Playwright) green)
- [x] **10h** — XMind8 + D3-HTML graph + plain-text exports (closes export-format gap "maigret writes XMind / D3 / TXT, Reckora only writes CSV / JSON / Markdown / HTML / PDF"; shipped via feat commit `ec01638`, merge `45f590e` (PR #9) on `main`; `app/exports.py` gains three new pure-stdlib renderers `to_txt(scan, results)` (matches maigret's `--txt` shape with `[+] <Site>: <URL>` claimed-hit stanzas + `[-] No CLAIMED hits` marker + `Outcome:` summary footer), `to_xmind(scan, results)` (XMind8 workbook — pure stdlib `zipfile` + `xml.etree.ElementTree`, no extra deps; valid zip with `content.xml` + `meta.xml` + `META-INF/manifest.xml` entries; central topic shows username + scan id, leaf topics per CLAIMED hit with `href` attribute pointing at the profile URL; deterministic across two renders of the same input so a diff stays clean), `to_d3_html(scan, results)` (self-contained HTML page with embedded graph data as a JSON `<script id='graph-data' type='application/json'>` block parsed by an inline JS via `JSON.parse` — never executed as JavaScript, so the embedded payload is data-not-code regardless of site name; loads D3 via `d3@7` CDN; the username + script-terminator escaping contract is enforced by two regression tests so a `</script>` substring in a site name never lets the embedded JSON terminate the surrounding script tag); `ALLOWED_FORMATS` / `MEDIA_TYPES` / `EXTENSIONS` / `render()` dispatcher extended with `txt` (`text/plain`) / `xmind` (`application/vnd.xmind.workbook`) / `d3` (`text/html`); REST endpoint `/api/history/{scan_id}/export.{txt,xmind,d3}` returns each format with `Content-Disposition: attachment`; ~22 new backend tests cover the three new renderers + the dispatcher round-trip + the route-level allowlist; additive — no admin knob, no behaviour change for existing CSV / JSON / Markdown / HTML / PDF callers)
- [x] **10i** — public OSS launch governance docs (closes community / discoverability gap "Reckora is private — closing the loop on `maigret`'s 'Used by' link, contributor governance, and a public README that explains the differentiation"; ships governance docs `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, with `security@duriancompany.us` as the enforcement contact), `CONTRIBUTING.md` (PR review flow, Phase-N runbook lifecycle, default-off invariant, conventional-commit `<type>(<scope>): <summary>` convention with `feat`/`fix`/`chore`/`docs`/`refactor`/`test` types), `SECURITY.md` (responsible-disclosure address `security@duriancompany.us` + 90-day disclosure timeline with concrete day-0 / 3-business-day / 14-day / 60-day / 90-day milestones, in-scope / out-of-scope tables, and a safe-harbour clause), and `CHANGELOG.md` (one entry per Phase 0 → Phase 10, grouped by `feat()` / `fix()` from `git log` on `main`); pre-existing `LICENSE` (MIT, matching the `pyproject.toml` `license = "MIT"` declaration) preserved unchanged; README grows a new "Why Reckora vs. maigret?" comparison table near the top emphasising the anti-bot probe pipeline (curl-cffi JA3/JA4 + Playwright fallback), distributed Celery + Redis worker fleet + Redis sliding-window per-host rate limit, ONNX ML reranker + KS drift detector, recursive identity extraction + pHash graph stitching + WHOIS/HIBP/reverse-image enrichment, ~2,506-site quarterly auto-refreshed catalog with +~500 tentative-sites behind `enable_tentative_sites`, members-only operator surface + `reckora-cli` (Phase 10f, in flight), CSV/JSON/Markdown/HTML/PDF/XMind8/D3/plain-text exports + auto-PDF on initial CLAIMED hit, default-off Phase 10 knobs guarantee; new "Governance" callout cross-links the new docs and points readers at `ROADMAP.md` as the single source of truth; pure docs change so no runtime delta, no new tests, baseline `pytest` / `mypy` / `ruff` / `pnpm lint` / `pnpm build` / Playwright e2e all stay at their Phase 10g values; the `soxoj/maigret` "Used by" wiki PR is tracked separately as a downstream task — it is a wiki edit on an external repo, not a Reckora-repo change)


### Phase 9-distribution — distribution ergonomics  *(out-of-scope, gated on user demand)*

> Items not on the committed plan. They close gaps in maigret's distribution
> surface (single-CLI binary, plugin community, PyPI publish, public docs
> site), not accuracy / scale, so they're parked until explicitly requested.

- [ ] **9-distribution-cli** — `reckora-cli` thin CLI wrapper (~200 LOC) calling the existing REST + SSE endpoints over HTTPS, packaged as a `pip install reckora-cli`-able wheel; gives users who prefer one-liner scans the same UX maigret has from PyPI without forking the server logic
- [ ] **9-distribution-plugin** — plugin SDK (`backend/app/plugins/` directory + a stable `Site` ABC + a registration entry-point group) so the community can drop in adapters for niche sites without forking `sites.json`; doubles as the migration path for the catalog rule DSL (richer than maigret's regex-only model — JSONPath on JSON responses, CSS selector + text-match on HTML, structured signals like `og:title contains <username>`)
- [ ] **9-distribution-binary** — single-binary release (PyInstaller / Nuitka) for users who want a single-file distribution without Python or Docker
- [ ] **9-distribution-pypi** — publish the backend as a `pip install reckora`-able wheel + frontend bundled via Docker entry-point; closes maigret's distribution edge directly
- [ ] **9-distribution-docs** — public docs site (Mintlify / Docusaurus) auto-generating REST API reference (from FastAPI OpenAPI) + GraphQL schema reference (from `graphql_api.py`) + admin knobs reference (from `admin_settings.KNOBS`) so newcomers don't have to read `ROADMAP.md` to onboard
- [ ] **9-distribution-license** — declare a public license (AGPL-3.0 mirrors maigret's GPL-3.0 ethos and protects the SaaS form factor); current state is "all rights reserved by default" which blocks external contributors

---

## Workflow guarantees per batch

These are non-negotiable guards I run on **every** batch before pushing to `main`:

1. Backend baseline must stay green: `uv run --extra dev ruff check . && ruff format --check . && mypy app && pytest -q`
2. Frontend baseline must stay green: `pnpm lint && pnpm build`
3. Playwright e2e must stay 3 / 3: `pnpm exec playwright test`
4. Direct-to-main push only after local baselines pass (no skipping hooks)
5. Post-push: `git_pr action=pr_checks wait_mode=all` until backend + frontend + e2e jobs all complete green; if red, fix before opening the next batch
6. After a batch lands, this ROADMAP is updated (mark `[x]`, bump `Live status` SHA + timestamp, advance `Active batch`) and the change is included in the *same* commit when feasible
7. Final batch in a phase (`*-deploy`) runs the manual HF mirror recipe + live verify; only when verify passes is the row promoted from `[x]` → `[V]`

---

## Phase 5h — claimed precision floor & link liveness *(detailed scope)*

> **Why this isn't Phase 6:** Phase 6's goal is *coverage* (more sites, better
> per-site annotations). Phase 5h's goal is *operator trust* on the existing
> catalog — the SPA's claimed bucket should be free of soft-404s and dead
> links **before** we widen the catalog further.

### 5h-A — confidence floor admin knob

- New `Settings.min_claimed_confidence: int = 60` in `backend/app/config.py`.
- New `KnobSpec("min_claimed_confidence", ..., min=0, max=100)` registered in
  `backend/app/admin_settings.py::KNOBS`.
- New `_apply_claimed_precision_floor(primary)` in `backend/app/checker.py`,
  invoked at the very end of `check_site` (after Phase 2 verify and Phase 4 ML
  rerank). When `primary.status is CLAIMED` and `primary.confidence < threshold`,
  demote to `UNKNOWN` and append signal `claimed-below-confidence-floor:<n>`.
- Tests: `backend/tests/test_checker.py::test_claimed_below_confidence_floor_is_demoted`
  parametrised over a few `(confidence, threshold)` pairs.
- Acceptance: 207 → 210 pytest, all green; admin can set the knob via
  `PATCH /api/admin/settings` and the next scan reflects it without restart.

### 5h-B — ML-confidence floor admin knob

- Mirrors 5h-A using `min_claimed_ml_confidence: int = 50` (0–100, mapped to the
  ONNX reranker's 0.0–1.0 range).
- Only applied when `primary.ml_confidence is not None` — non-username scans and
  ML-disabled deployments are unaffected.
- Signal: `claimed-below-ml-floor:<n>`.
- Acceptance: rule-based-claimed + ML-disagreed-low rows demote cleanly; tests cover
  the "ML score missing → no demotion" path.

### 5h-C — verify-required gate

- Mirrors 5h-A using `require_verify_for_claim: int = 0` (0 = off, 1 = strict).
- When `1`, demote `CLAIMED` rows whose `primary.verified is not True` — both
  `False` (Phase 2 disagreed) and `None` (verify wasn't run) trigger the demote.
- Signal: `claimed-not-verified`.
- Acceptance: opting-in produces a strict mode where claimed = "rule + verify both
  agreed"; opting-out is the existing behaviour exactly.

### 5h-D — post-claim link-liveness probe

- New `_link_liveness_probe(client, primary, *, cffi_rescue)` in
  `backend/app/checker.py`. Issues one extra `HEAD` (falls back to `GET` if HEAD
  is rejected) on `primary.url` (the human-facing display URL, post-substitution)
  with `follow_redirects=True` and a short `link_liveness_timeout` (default
  `4.0 s`).
- Demote rules (each adds a unique signal so the SPA can show a precise reason):
  - HTTP `404` / `410`                                       → `link-dead-after-claim`
  - Final URL path matches `_LINK_LIVENESS_DEAD_PATH_TOKENS` (`/login`, `/signup`,
    `/sign-in`, `/users/sign_in`, `/404`, `/not-found`, `/error`, `/missing`,
    `/deactivated`, …)                                       → `link-redirects-login`
  - Final URL canonical-host matches `urlMain` and final path is one of `/`,
    `/index`, `/home`                                        → `link-redirects-home`
  - Final URL query string contains a token from
    `_LINK_LIVENESS_DEAD_QUERY_TOKENS` (`missing=true`, `deleted=1`, …)
                                                            → `link-redirects-home`
  - HEAD / GET raises `httpx.RequestError`                  → no demote (fail-open;
    we don't want a transient network glitch to wipe a claim — instead we add
    `link-liveness-error` signal so the SPA can show "verification skipped")
- When `cffi_rescue` is configured and the HEAD/GET returns `403/429/451` or a
  body with the `_is_blocked_body` fingerprint, retry via curl-cffi Chrome 131
  TLS impersonation — same rescue path Phase 2.2 uses for primary probes.
- Successful liveness adds the positive signal `link-verified-live` so the SPA
  can render a green badge.
- Gated by `enable_link_liveness: bool = True` (`Settings`) + the
  `enable_link_liveness` admin knob, so an operator can toggle the extra probe
  off when egress is rate-limited.
- Tests: new `backend/tests/test_link_liveness.py` covering the six demote shapes
  + the network-error fail-open + the `enable_link_liveness=False` no-op.
- Acceptance: pytest growth ~+8 cases; HF Space scan of `github` with `All sites`
  surfaces zero claimed rows whose URLs `404` on click.

### 5h-E — dual-URL display check

- Many sites expose `urlProbe` separate from `url` (the SPA-displayed URL). When
  `site.url_probe` is set and differs from `site.url`, the post-claim liveness
  check (5h-D) runs against `site.url` regardless of which URL was probed —
  catching cases where the probe API is fine but the public profile URL has
  rotted.
- New signal: `display-url-dead` (separate from `link-dead-after-claim` so the
  SPA can distinguish "API said yes, public URL says no" from "the only URL we
  know is dead").
- Tests: `test_link_liveness.py::test_dual_url_demotes_when_display_dies`,
  `::test_dual_url_passes_when_both_alive`.
- Acceptance: claims on Dailymotion-shaped sites (`api.dailymotion.com` probe,
  `www.dailymotion.com` display) get demoted iff the public profile is gone.

### 5h-FE — SPA signal badges + filtered buckets

- `frontend/src/types.ts`: extend the `Status` discriminator with no new states
  (demoted rows are still `UNKNOWN`); the SPA uses signal substrings to show
  precise badges.
- `ResultCard.tsx`: render badge `Link verified live` (green, small, with
  external-link icon) when `signals.includes('link-verified-live')`. Render
  badge `Demoted (link dead)` (amber) when status is `UNKNOWN` and any of the
  demote signals is present.
- `Stats.tsx`: add two new counters next to existing `Claimed / Free / Unknown`:
  `Filtered (link)` aggregating rows demoted by 5h-D / 5h-E, and `Filtered
  (confidence)` aggregating rows demoted by 5h-A / 5h-B / 5h-C.
- Tests: extend `frontend/playwright/specs/search.spec.ts` to assert the new
  badges render when a stub backend emits the relevant signals.
- Acceptance: bundle size growth `<+1 KB gzip`; e2e count grows from 3 → 4.

### 5h-deploy — HF mirror + live click-test

- Run the `live_deployment.manual_mirror_recipe` from the snapshot to push the
  end-of-batch `main` SHA to `huggingface.co/spaces/yesmyyt/cacadoxx`.
- Wait for the HF Docker rebuild (~2 min).
- Verify endpoints: `/api/health → 200`, `sites=2506`; the four auth-gated
  search routes return `401` instead of `404`.
- Manual click-test:
  1. Log in to the live SPA, scan `github` with `All sites` (limit `2506`).
  2. Sample five random rows from the claimed bucket.
  3. For each, click the URL — if any lands on `404`, login wall, or the site
     homepage, that's a regression: open a Phase 5h-followup batch instead of
     marking 5h-deploy as `[V]`.
- Promote the row from `[x]` → `[V]` only when the live click-test passes.

---

## Phase 6 — accuracy & gap closure vs maigret

> **Goal:** quantitatively match or beat `soxoj/maigret` on every dimension where
> maigret is currently ahead (raw site count, per-site identity-type annotations).
> Make the gain measurable with a head-to-head benchmark.

### 6a — regenerate `sites.json` from `soxoj/maigret` upstream

- Add `backend/scripts/regen_from_maigret.py` — fetches latest `data.json` from
  `soxoj/maigret`, merges with our curated overrides + engine resolutions + ML
  features.
- Auto-apply the per-site `identityTypes` field (maigret already annotates this in
  YAML; we currently only have a 7-site `KNOWN_IDENTITY_OVERRIDES` map).
- Preserve disabled flags + alexa rank + per-site `prewarm` tags.
- Land as a single commit so a `git revert` reverts the catalog cleanly if
  regression hits.
- Acceptance: catalog grows from `2,506` → `~3,000+`; `pytest tests/test_sites.py`
  passes with the new schema; existing 207 tests stay green.

### 6b — engine-aware probe heuristics

- New `backend/app/engine_probes.py` with engine-specific probe signatures:
  - **XenForo**: GET `/members/<u>` + look for `data-user-id="<n>"` in HTML.
  - **Discourse**: GET `/u/<u>.json` instead of HTML scrape — saves bandwidth +
    gives clean 200/404 signal.
  - **vBulletin**: GET `member.php?username=<u>` + check for the `userinfo` block.
  - **uCoz**: subdomain `<u>.ucoz.com` resolution (DNS check before HTTP).
- `check_site` chooses the engine probe when `site.engine` is set; falls back to
  the generic regex/presence/absence model otherwise.
- Acceptance: Discourse family (`~80` sites) p95 drops by `>40 %`; false-positive
  rate on uCoz (`~600` sites) drops by `>60 %`.

### 6c — head-to-head benchmark vs `soxoj/maigret`

- Add `backend/scripts/bench_vs_maigret.py` — runs the same `100`-site set through
  both Reckora's HTTP API and a freshly-`pip install`-ed maigret CLI; compares
  precision/recall, p50/p95 latency, BLOCKED-rescue rate, false-positive count.
- Publishes a Markdown table to `docs/benchmark-phase-6.md` so the gain is auditable.
- Acceptance: Reckora ≥ maigret on **claimed-precision** and **BLOCKED-rescue
  rate**; latency comparable (within 1.2× to account for ML reranker overhead).

### 6d — PDF export (resurrected from Phase 5b)

- Add `weasyprint` as an **optional** extra (`pip install reckora-backend[pdf]`).
- New formatter `backend/app/exports.py::render_pdf` falls back gracefully to HTML
  when `weasyprint` isn't installed (no hard dependency on the multi-MB native
  stack at base install).
- Acceptance: HF Space's Docker image with the `[pdf]` extra includes the
  formatter; ScanModal's "Download PDF" button works end-to-end.

---

## Phase 7 — intel & enrichment

> **Goal:** capabilities `soxoj/maigret` does not have. Each sub-phase opens a new
> dimension of OSINT signal; Phase 7's dashboards leverage Phase 5d's admin knobs
> for runtime tuning.

### 7a — profile-image perceptual-hash (pHash) graph stitching  *(shipped)*

- `backend/app/extractor.py` was already capturing `avatar_url` from
  `og:image` meta tags + `socid_extractor`'s schemes; Phase 7a piggybacks
  on that without changing the extractor.
- New `backend/app/imagephash.py` — streams the avatar (size-capped,
  short timeout, follow-redirects) and computes a 64-bit perceptual
  fingerprint. Self-contained 8x8 dHash (resize → grayscale → left-vs-
  right pixel comparison) so we ship just `Pillow` and skip the
  `imagehash` package's scipy + PyWavelets dependency (~30 MB Docker
  bloat we don't need on a default-off knob).
- The hash is written to `CheckResult.extracted['avatar_phash']`, which
  rides the existing serialization path through `to_dict` and
  `record_scan`, so it persists in the `scans` history table without a
  schema change.
- `/api/search/graph` (`app/main.py`) collects every CLAIMED hit's
  `avatar_phash`, runs `find_phash_edges` over the unordered pairs,
  and emits a `phash_edge` SSE event per close pair (Hamming
  `≤ image_phash_max_distance`). The terminal `done` payload picks up
  a new `phash_edges` count.
- SPA renders pHash edges as a distinct dashed purple stroke between
  the two `site:` nodes, with a `↺ phash · d=N` label, and the
  `GraphView` SidePanel surfaces a `pHash matches` stat plus a
  helper-text mention of the new edge type.
- Two admin knobs: `enable_image_phash` (default `0` — the extra
  HTTP-per-claim is opt-in) and `image_phash_max_distance` (`0–32`,
  default `6`).
- Tests: `tests/test_imagephash.py` (20 cases) covers the pure hash
  math, threshold semantics, length mismatches, mocked httpx fetches
  (200 / 404 / oversized body / connect error / undecodable body);
  `tests/test_graph_phash.py` (4 cases) drives the graph endpoint
  with stubbed `stream_checks` results and asserts the `phash_edge`
  SSE payload + `done.phash_edges` count under all knob/threshold
  combinations; `frontend/e2e/phash-edge.spec.ts` exercises the full
  SPA path with an intercepted graph stream and asserts the dashed
  purple edge label + SidePanel `pHash matches` stat render.
- Acceptance: ✅ Phase 3 graph view renders pHash-derived edges as a
  distinct edge type; admin knob `image_phash_max_distance`
  (default `6`) tunes sensitivity.

### 7b — WHOIS / HIBP-style breach enrichment for `identity_type=email`  *(shipped)*

- New `backend/app/enrichment.py` runs **RDAP** (the structured
  successor to legacy WHOIS) on the email's domain and **HIBP**
  (`/api/v3/breachedaccount/{email}`) on the address itself. Both
  upstreams are read-only OSINT signals — no scraping, no credential
  discovery, no private data — and run **concurrently** through
  `asyncio.gather` so the SPA's worst-case wait is one
  `enrichment_timeout` instead of the sum.
- RDAP needs no API key; HIBP requires a paid key, kept env-only in
  `Settings.enrichment_hibp_api_key` and **never** echoed by the
  public `/api/settings` route. Missing keys disable HIBP cleanly
  (the WHOIS half still works).
- Per-actor sliding-window `RateLimiter` (60-second window, default
  `5 / min` admin-tunable via `enrichment_rate_per_min`) protects
  upstream quotas — the handler returns `HTTP 429` when the budget
  is empty.
- Single endpoint: `POST /api/enrich/email` (signed-in members only).
  Always returns `200` with a `disabled` boolean so the SPA can hide
  the panel cleanly when the admin knob is off.
- Two admin knobs: `enable_email_enrichment` (default `0` — opt-in)
  and `enrichment_rate_per_min` (`1-60`, default `5`).
- Frontend mounts `EmailEnrichmentPanel` after every list-mode
  `identity_type=email` scan finishes. The component renders a
  WHOIS card (registrar, created/expires/updated, nameservers,
  status) plus a breach-list card (HIBP `Breach` objects with
  date + data-class chips). Both halves degrade independently —
  if RDAP is down but HIBP succeeds, only the breach side renders.
- Tests: `tests/test_enrichment.py` (33 cases) covers the RDAP /
  HIBP parsers, the rate limiter (sliding-window eviction, per-
  actor isolation, zero-limit edge case), the HTTP fetchers
  driven through `httpx.MockTransport` (200 / 404 / 401 / transport
  error / non-JSON body / missing template placeholder / missing
  API key), and the FastAPI endpoint via `TestClient`
  (auth-required / disabled-knob / invalid-email / happy-path /
  rate-limit-trips). `frontend/e2e/email-enrichment.spec.ts`
  drives the SPA through the full flow with intercepted
  `/api/search` + `/api/enrich/email` responses.
- Acceptance: ✅ an email scan against a stub-breached test
  address surfaces the breach list; per-actor rate limit (default
  `5/min`, admin-tunable to `60/min`) protects upstream quotas.

### 7c — reverse-image lookup hook  *(shipped)*

- New `backend/app/reverseimage.py` is a pure URL builder — no
  network I/O, no API keys. It exposes three providers (Yandex
  Images / Google Lens / TinEye), each with a configurable URL
  template (`{url}` placeholder). The companion
  `POST /api/reverse-image` handler validates the avatar URL,
  rejects non-`http(s)` schemes (no `data:` / `javascript:` /
  `file:` deep-links), and returns a `{disabled, providers}`
  envelope so the SPA can hide the chip row when admins flip the
  knob off.
- One admin knob (`enable_reverse_image`, default **on**) and three
  env-only template overrides for operators who want to swap in a
  private mirror.
- Frontend `ReverseImageChips` mounts inside the existing
  extracted-fields panel (only visible when the operator clicks
  the `↗ extracted · N` chip on a CLAIMED card). When the
  extractor surfaced an `avatar_url`, the chip row renders three
  `target=_blank` deep-links into each upstream's public
  reverse-search landing page. Operator's browser hits the
  upstream directly on click; the backend never makes the call.
- Tests: `backend/tests/test_reverseimage.py` (21 cases) covers the
  URL-encoder edge cases (special chars, opaque mirrors, missing
  template, unsafe schemes), the metadata invariants (unique
  slugs, every provider points at a real `Settings` field), and
  the FastAPI endpoint via `TestClient` (auth-required, knob-off
  short-circuit, three-provider happy path with embedded encoded
  URL, `data:` URL rejection, empty `image_url` 422).
  `frontend/e2e/reverse-image.spec.ts` drives the SPA through the
  full flow with intercepted `/api/search` + `/api/reverse-image`,
  asserts all three chips render, and that each link's `href`
  embeds the URL-encoded `avatar_url` and opens in a new tab.
- Acceptance: ✅ graph node + result-card avatars surface a
  click-to-open chip row that lands on Yandex / Google Lens /
  TinEye with the image URL pre-filled.

### 7d — ML reranker retrain pipeline + drift detector  *(shipped)*

- `backend/scripts/train_ml.py` was refactored to expose a
  `build_training_rows()` helper so the runtime drift detector can
  reproduce the synthetic training distribution without pulling NumPy
  / sklearn into the request loop.
- `backend/app/ml_retrain.py` wraps the trainer in a synchronous
  `run_retrain(triggered_by=...)` entry point, writes a
  `RetrainStatus` blob (started_at, finished_at, duration_s, log,
  error, triggered_by, extras) to `app/ml/retrain_status.json` so
  the most recent run survives worker restarts, and exposes
  `model_age_days()` from the `model.onnx` mtime.
- `backend/app/ml_drift.py` is a pure-Python two-sample
  Kolmogorov-Smirnov drift detector: `summarise()` projects a list
  of floats into a `FeatureSummary` (count / mean / std / min / p50
  / p90 / max), `ks_two_sample()` computes the KS statistic plus an
  asymptotic p-value (Stephens 1970 correction) without `scipy`,
  and `compute_drift()` aggregates per-feature verdicts into a
  `DriftReport` whose `any_alarm` flag and `alarm_features` tuple
  drive the admin-panel UI. Features with fewer than
  `MIN_SAMPLE_SIZE` observations are silently skipped.
- `backend/app/ml.py` records every production feature vector that
  goes through `score()` into a bounded ring buffer
  (`record_features_for_drift` + `recent_feature_distribution`) so
  the drift detector has a "current" sample without needing a
  separate logging pipeline.
- Two new admin-only handlers in `backend/app/main.py`:
  - `GET /api/admin/ml/status` returns `{model_age_days,
    last_retrain, drift}`. The drift threshold is pulled from the
    new `ml_drift_ks_threshold_pct` admin knob (1-100, divided by
    100 at runtime), and the buffer cap is the `ml_drift_window_size`
    admin knob (64-65536).
  - `POST /api/admin/ml/retrain` runs `run_retrain` synchronously
    and returns the fresh status; the SPA refreshes
    `/api/admin/ml/status` after the call returns.
- SPA grows a new `ML` tab in `AdminPanel`. `MlTab.tsx` shows the
  model age, the last-retrain row (timestamp, duration, ok / error,
  triggered-by), a "Retrain now" button, the drift summary chip
  (`alarm` / `ok`) plus a per-feature KS table with reference and
  current row counts, and a collapsible last-retrain log block.
- Tests:
  - `backend/tests/test_ml_drift.py` (16 cases) covers `summarise`,
    `ks_two_sample` (identical, disjoint, partial-overlap,
    shifted-mean), and `compute_drift` (skipping, threshold gating,
    `to_dict` shape).
  - `backend/tests/test_ml_retrain.py` (9 cases) covers
    `run_retrain` happy path, failure path, exception capture, log
    truncation; `last_status` round-trips and graceful degradation
    on corrupt / non-dict JSON; `model_age_days` returning `None`
    when the model is missing.
  - `backend/tests/test_ml_admin_endpoints.py` (6 cases) covers
    auth gating on both endpoints, the threshold knob driving the
    drift envelope, the retrain endpoint persisting status (with a
    monkeypatched trainer), and failure surfacing.
  - `frontend/e2e/ml-admin-tab.spec.ts` Playwright e2e logs in as
    admin, navigates to the ML tab, asserts the drift table
    renders the alarmed and ok rows correctly, clicks "Retrain
    now", and verifies the status row + log block update on
    refresh.
- Acceptance: admin panel shows "model age", "last retrain", "drift
  status" per acceptance criteria; retrain status persists across
  restarts; drift alarm fires when the production distribution
  diverges from the synthetic training set above the admin-tunable
  threshold.

---

## Phase 8 — scale & infra

> **Goal:** make Reckora scale horizontally so a `10k`-site catalog and `100s`
> of concurrent operator scans don't melt a single uvicorn process.

### 8a — distributed worker fleet (Celery + Redis queue)

- Move per-site `check_site` calls behind a Celery task so a fleet of workers
  can shard the catalog horizontally.
- API process keeps the SSE stream open and reads results from the queue's
  result backend.
- Acceptance: `100`-site scan p95 with `2` workers is `<60 %` of single-process
  baseline; `500`-site scan stays under `120 s` p95.

### 8b — rate-limit-aware proxy rotator

- Phase 5e ships a single proxy URL. Phase 8b adds a pool with per-host rate
  budgeting (`token bucket` keyed by `(proxy, target_host)`).
- New admin knob `proxy_pool_size` exposes how many proxies to rotate over.
- Acceptance: a Cloudflare-protected family (Flickr/Tumblr) BLOCKED-rescue
  rate stays `>15 %` even when the test rate triples.

### 8c — GraphQL gateway (alternate transport)

- Mount a thin GraphQL layer in front of the existing REST/SSE routes for
  clients that prefer typed client codegen over OpenAPI.
- Acceptance: Apollo Studio introspection works; SSE streamed via subscription;
  REST/SSE endpoints stay unchanged for backwards-compat.

### 8d — public-scan edge cache

- Add an admin-toggle "public-shareable scan" — when enabled, the scan's
  results page is rendered as static HTML and pushed to a CDN-friendly cache
  (Cloudflare KV or HF Space static dir).
- Acceptance: shared scan URLs respond in `<50 ms` p95 from a cold start.

---

## Phase 9 — anti-bot probe + scale-out reliability  *(detailed scope)*

> **Why this is its own phase:** Phase 8 closes the *coordination* gaps in
> Reckora's egress (multi-proxy fleet + per-host rate limit budgeting + at-
> least-once Celery delivery). It does not close the *fingerprint* gaps
> (Cloudflare/Akamai/Datadome WAF JS challenge, JA3/JA4 TLS handshake
> classification), the *cluster-coordination* gap of the rate-limit budget
> across N Celery workers, the *region* of the egress IP, the *labelling
> accuracy* when a site responds with a CAPTCHA instead of a 429, the
> *freshness* of the bundled site catalog, the *time + cost cap* of a long-
> tail scan, the *reputation curve* of a freshly-added proxy, or the *cold-
> start latency* of an idle HF Space container. Phase 9 picks each of those
> apart with a dedicated knob-gated sub-phase. The residual-risk IDs (`C1`,
> `C2`, `M1`–`M7`) come from the Phase 8 deploy debrief and are surfaced in
> the master checklist entries above.

### 9a — anti-bot probe pipeline (closes residual risks **C1** + **C2**, severity **CRITICAL**)

- `app/anti_bot_transport.py` wraps `curl-cffi` (already a Phase 1 dependency
  used only by `chrome_tls_rescue` for a small allowlist) into an
  `httpx.AsyncBaseTransport` with `impersonate="chrome120" | "safari17" |
  "firefox120"` selectable per-host so a WAF that fingerprints the TLS
  handshake (cipher suite ordering + extension list = JA3 / JA4 hash) sees
  a real browser handshake instead of the Python uvloop default.
- `app/playwright_fallback.py` shells out to a headless chromium when a
  curl-cffi impersonation still returns a JS challenge HTML — limited to a
  configurable per-scan budget so a worst-case 2,500-site scan can't fan
  out 2,500 chromium spawns.
- Per-site override via a new `anti_bot_profile` field in `site_overrides`
  so a site that needs Safari TLS while default is Chrome can be tagged
  individually without affecting the rest of the catalog.
- New admin knobs: `enable_anti_bot_pipeline` (default 0),
  `anti_bot_default_impersonation` (string, default `"chrome120"`),
  `anti_bot_playwright_fallback` (default 0),
  `anti_bot_playwright_max_per_scan` (default 5),
  `anti_bot_playwright_timeout_sec` (default 10).
- `app/transport.py` gains `build_anti_bot_client()` so the Phase 8b
  `RotatingProxyTransport` stacks correctly *over* the Phase 9a
  impersonation transport (proxy rotation outside, fingerprint inside).
- `/api/health.anti_bot.{enabled, default_impersonation,
  playwright_fallback, playwright_chrome_installed}` so an operator can
  verify chromium is installed without firing a probe.
- Acceptance: 80–150 sites recover from `UNKNOWN` to `CLAIMED` / `FREE` on
  a Cloudflare-fronted catalog subset; pytest baseline +25 tests; ruff /
  mypy clean.

### 9b — distributed host rate limit (closes **M1**, severity **MEDIUM**)

- Replace the in-memory token bucket from Phase 8b with a Redis-backed
  sliding-window counter (single atomic Lua script handles `INCR + EXPIRE
  + check`) so N Celery workers share one bucket per host.
- New admin knob `host_rate_limit_backend` — `"memory"` default,
  `"redis"` opt-in (only honoured when `RECKORA_REDIS_URL` is set, same
  connection string as the Phase 8a Celery broker).
- Fail-open to in-memory bucket on Redis transport error so a Redis
  outage doesn't fail-closed every probe.
- `/api/health.host_rate_limit.backend` surfaces the active backend.
- Acceptance: a 4-worker `asyncio.gather` simulation respects a 60/min/host
  bucket as a *cluster-wide* total (not 240/min as today); `+12` tests
  added behind a CI matrix entry that spins a `redis-stack` service
  container exclusively for the new test file.

### 9c — geo-aware proxy selection (closes **M2**, severity **MEDIUM**)

- Extend `RECKORA_PROXY_POOL_URLS` parser to accept
  `region:scheme://host:port` syntax (bare URLs continue to work, tagged
  `region="default"`).
- New optional `preferred_region` field on per-site catalog entries; the
  9e regen script learns this for known geo-locked sites (Yandex / VK =
  `"ru"`, Sina Weibo = `"cn"`, Naver = `"kr"`, etc.).
- `proxy_pool.pick()` prefers a regional match → `"default"` → any
  healthy proxy as the last fallback, so a single-region pool still
  works.
- New admin knob `enable_geo_aware_proxy` (default 0).
- Acceptance: 30–60 region-locked sites recover; round-robin behaviour
  preserved when knob is off; `+10` tests.

### 9d — CAPTCHA classifier + structured `UNKNOWN` labelling (closes **M3**, severity **MEDIUM**)

- `app/captcha_signatures.py` ships a curated regex / selector signature
  DB for reCAPTCHA v2 / v3, hCaptcha, Cloudflare Turnstile, Datadome,
  AWS WAF Captcha, FunCaptcha, Geetest, plus the generic Apache form-
  action pattern.
- Checker pipeline classifies a 200-OK response that matches a CAPTCHA
  signature as `UNKNOWN` with `reason="captcha_required"` (instead of
  the current generic `reason="anomalous_response"`) so the SPA renders
  a dedicated badge.
- Per-vendor counts surface in `/api/admin/ml/status` so an operator can
  see "47 sites returned Cloudflare Turnstile this week".
- `+15` tests covering each signature, false-positive guards, and the
  SPA badge contract.

### 9e — catalog auto-refresh from upstream maigret (closes **M4**, severity **MEDIUM**)

- `scripts/regen-from-maigret.py` clones the latest `soxoj/maigret`
  release tag, parses `maigret/resources/data.json`, diffs against
  `backend/app/sites_data.json`, classifies each delta as one of
  `{added, removed, url_changed, identityType_changed,
  engine_hint_changed}`, and writes a structured PR-ready markdown
  report plus the regenerated catalog candidate.
- New GitHub Action `.github/workflows/catalog-regen.yml` (cron
  `0 5 1 */3 *` = first day of every quarter) runs the script and opens
  a PR titled `chore(catalog): quarterly refresh from soxoj/maigret
  <tag>` with the diff inlined.
- The regen pipeline preserves Reckora-specific fields (`engine_hints`,
  `identityTypes` overrides, Phase 6 trim flags) so a refresh doesn't
  undo Phase 6 precision wins.
- Acceptance: drift drops from ~5%/year to ≤1%/quarter; `+8` tests for
  the diff classifier.

### 9f — per-scan hard budget + `SKIPPED` bucket (closes **M5**, severity **MEDIUM**)

- Replace the existing single `scan_timeout_sec` with a triple-budget
  model: `scan_max_total_sec` (300s default — hard kill),
  `scan_per_site_timeout_sec` (8s default — separate from
  `httpx.connect/read` timeouts), and `scan_max_concurrent_skips` (50
  default — bulk-skip threshold).
- New result bucket `SKIPPED` next to CLAIMED / FREE / BLOCKED / UNKNOWN
  with `reason="budget_exceeded" | "scan_total_killed"`; SPA Stats panel
  gains a `Filtered (budget)` counter; operator can re-run only the
  SKIPPED rows instead of the whole scan.
- `+10` tests covering cumulative-budget bookkeeping, the bulk-skip
  threshold path, and the SSE `event: skipped` frame format.

### 9g — proxy reputation warm-up + weighted selection (closes **M6**, severity **MEDIUM**)

- Extend `proxy_pool.py` with per-proxy `success_count` /
  `failure_count` rolling counters (sliding window of last 100 requests
  per proxy, in-memory only — no extra Redis dependency), expose a
  `weight = success / (success + failure)` ratio, and bias `pool.pick()`
  to prefer high-weight proxies once each proxy has accumulated
  ≥ `proxy_warmup_min_requests` (default 50) requests.
- New admin knobs: `enable_proxy_pool_weighted_selection` (default 0),
  `proxy_warmup_min_requests` (default 50).
- `/api/health.proxy_pool.members[].weight` exposes per-proxy weight so
  an operator can see "proxy A 0.94 / proxy B 0.32 — proxy B is dragging
  the pool down".
- `+10` tests covering weight calculation, warm-up window enforcement,
  weighted-random fairness, and the round-robin fallback when the knob
  is off.

### 9h — HF Space keep-warm cron (closes **M7**, severity **MEDIUM-LOW**)

- New GitHub Action `.github/workflows/hf-keep-warm.yml` (cron
  `*/30 * * * *`) hits `GET https://yesmyyt-cacadoxx.hf.space/api/health`
  every 30 minutes so the HF Space free-tier container stays warm.
- Alternative: migrate to HF Spaces "Always On" tier when budget allows
  — left as a comment in the workflow for the operator.
- Acceptance: returning user hits a sub-second response instead of the
  30–45s Docker boot.

---

## Out of scope (Phase 9-distribution)

> Items the user has not asked for but that are natural follow-ons. These
> close maigret's *distribution* surface (PyPI publish, public docs site,
> single-CLI binary, plugin community), not accuracy / scale, so they're
> parked until explicitly requested.

- **9-distribution-cli**: `reckora-cli` thin wrapper (~200 LOC) calling
  the existing REST + SSE endpoints over HTTPS, packaged as a
  `pip install reckora-cli`-able wheel.
- **9-distribution-plugin**: `app/plugins/` directory + a stable `Site`
  ABC + an entry-point group so the community can drop in adapters for
  niche sites without forking `sites.json`. Doubles as the migration
  path for the catalog rule DSL (richer than maigret's regex-only model:
  JSONPath on JSON responses, CSS selectors, structured signals).
- **9-distribution-binary**: PyInstaller / Nuitka single-file
  distribution.
- **9-distribution-pypi**: publish the backend as a `pip install
  reckora`-able wheel with the frontend bundled via Docker.
- **9-distribution-docs**: public docs site (Mintlify / Docusaurus)
  auto-generating REST API reference (from FastAPI OpenAPI), GraphQL
  schema reference (from `graphql_api.py`), and admin knobs reference
  (from `admin_settings.KNOBS`).
- **9-distribution-license**: declare a public license (AGPL-3.0
  mirrors maigret's GPL-3.0 ethos and protects the SaaS form factor).

---

## Comparison snapshot — Reckora @ end-of-Phase-8 vs `soxoj/maigret`

| Dimension | maigret | Reckora @ Phase 8 |
| --- | --- | --- |
| Site catalog count | 3,000+ | 3,000+ |
| Per-site identity-type annotations | YAML | JSON + curated overrides + transforms |
| Confidence model | binary CLAIMED/FREE | 0–100 signals scorer + ONNX reranker (retrained from history) |
| Verify pass | single probe | two-stage with UA/referrer rotation |
| Anti-bot rescue | none | curl-cffi Chrome 131 TLS impersonation + cookie-jar prewarm (~928 sites) |
| Engine-aware probes | regex per site | dedicated probes for XenForo/Discourse/vBulletin/uCoz |
| Recursive identity discovery | none | graph depth-2 via socid_extractor + react-flow |
| Image stitching (pHash) | none | cross-link wajah lintas situs (Phase 7a) |
| Email/phone enrichment | none | WHOIS + HIBP-style breach data (Phase 7b) |
| Reverse-image lookup | none | one-click Yandex/Google Lens hook (Phase 7c) |
| ML reranker retrain | none | one-click admin retrain + KS-stat drift detector (Phase 7d) |
| Permutation strategies | basic | 4-strategy pipeline (separator + suffix + prefix + leetspeak) |
| History + diff | stateless | SQLite history + per-username diff |
| Auth & RBAC | none | members-only + admin role + bcrypt + bearer/cookie |
| Live admin DB editor | edit YAML + restart | UI edit + 5 s TTL cache live override |
| Concurrency model | aiohttp single-process | httpx HTTP/2 + Celery worker fleet (Phase 8a) |
| Proxy / Tor | partial | uniform across list/graph/permute + rotator (Phase 8b) |
| Transport | CLI text | REST + SSE + GraphQL (Phase 8c) |
| Edge cache | none | TTL cache + public-scan edge cache (Phase 8d) |
| UI | terminal output | React SPA + admin panel + graph view |
| Distribution | single CLI binary | Docker mono-image |
| CLI ergonomics | mature | API-first (Phase 9 candidate) |
| Plugin ecosystem | community plugins | none yet (Phase 9 candidate) |
| Observability | log file | Sentry SDK + structured logs |
| Test coverage | ~30 pytest | 207 → ~350+ post-Phase-8 |
| CI/CD | basic | ruff + mypy + pytest + frontend lint+tsc+build + Playwright e2e + auto-deploy |

**Net:** after Phase 8, Reckora is ahead of maigret on every dimension that
maps to **accuracy** or **operator-scale**, behind only on **CLI ergonomics**
and **plugin ecosystem** (intentionally deferred to Phase 9, gated on
explicit user demand).

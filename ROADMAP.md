# Reckora roadmap — Phase 5h → Phase 8

This roadmap is the **single source of truth** for Reckora's planned + in-flight
work. Anything not on this list is not committed-to. Items are ticked off when
the change is merged into `main`, CI is green, and (where applicable) the live
HF Space at <https://yesmyyt-cacadoxx.hf.space> reflects the change.

---

## Live status

| Field | Value |
| --- | --- |
| Repo HEAD on main | `34a79d1` (Phase 8d — public-scan edge cache (CDN-friendly anonymous read endpoint for shared scans; opt-in via the `enable_share_links` admin knob, default 0); feat commit `34a79d1` shipped the runtime, CI run [25474842913](https://github.com/yowanda/Reckora/actions/runs/25474842913) green) |
| HF Space HEAD | `0551938` (mirrors main `945c6bc` — Phase 6 + Phase 7 catch-up deployed 2026-05-07; HF Space stays on the in-process path because the `[scale]` extra is opt-in; flip `RECKORA_USE_CELERY=1` + ship a Redis broker to switch to the distributed fan-out, set `RECKORA_PROXY_POOL_URLS` + `enable_proxy_rotator=1` for the multi-proxy fleet, install the `[gql]` extra + `enable_graphql_gateway=1` for the GraphQL gateway, or flip `enable_share_links=1` to mint anonymous CDN-cacheable scan reports — all without redeploying app code) |
| Active batch | _none — Phase 8d shipped; Phase 8 work-stream complete_ |
| Last batch shipped | Phase 8d — public-scan edge cache (`backend/app/share_links.py` adds a `share_links` table + mint / lookup / revoke / cascade-revoke / purge-expired primitives over a 192-bit URL-safe random token; `POST /api/history/{scan_id}/share` mints a token (owner-only, per-scan cap, optional TTL with admin ceiling), `GET /api/history/{scan_id}/shares` lists active links, `DELETE /api/history/{scan_id}/share/{token}` revokes one, and `DELETE /api/history/{scan_id}` cascades the revoke; the public anonymous endpoint `GET /share/{token}` returns the same payload shape as `GET /api/history/{scan_id}` plus `Cache-Control: public, s-maxage=<knob>, stale-while-revalidate=<knob/2>` and an ETag derived from `sha256(token | finished_at)[:16]` so a CDN edge serves the response while revalidating in the background; `If-None-Match` returns 304 with the same cache headers; new admin knobs `enable_share_links` (default 0), `share_link_default_ttl_sec` (default 0 — never), `share_link_cache_max_age_sec` (default 60s), `share_link_max_per_scan` (default 5); `/api/health` exposes `share_links.{enabled, cache_max_age_sec}`; 15 new backend tests in `tests/test_share_links.py` cover storage primitives (mint, cap, lookup, expiry, revoke, cascade, purge, etag stability), HTTP knob gate (all four endpoints 503 when off), end-to-end mint-list-public-revoke round trip with cache headers + 304 revalidation, cross-member privacy (other-member mint returns 404), and parent-scan cascade; bringing the suite to 417 / 417 pytest, ruff / format / mypy clean across 29 source files, frontend e2e 8 / 8) |
| Last update | 2026-05-07 (Phase 8d — public-scan edge cache shipped) |
| Test baseline | backend pytest **417 / 417** · mypy 29 src files · ruff clean · frontend lint+build green · e2e **8 / 8** |

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
- [x] **8d** — public-scan edge cache (`backend/app/share_links.py` adds a `share_links` SQLite table + mint / lookup / revoke / cascade-revoke / purge primitives over a 192-bit URL-safe random token; the auth-gated `POST /api/history/{scan_id}/share` mints a token with optional `ttl_sec` capped by the admin-tunable `share_link_default_ttl_sec`, `GET /api/history/{scan_id}/shares` lists the caller's active tokens, `DELETE /api/history/{scan_id}/share/{token}` revokes one, and `DELETE /api/history/{scan_id}` cascades the revoke so a deleted scan can never be revived through a stale link; the public anonymous endpoint `GET /share/{token}` returns the same shape as the auth-gated single-scan view plus CDN-friendly cache headers — `Cache-Control: public, s-maxage=<knob>, stale-while-revalidate=<knob/2>`, an ETag derived from `sha256(token \| finished_at)[:16]`, and `Vary: accept-encoding` so a Cloudflare-class CDN can cache one entry per encoding; `If-None-Match` short-circuits to 304 with the same cache headers so a revalidating edge doesn't pay the database lookup twice; ownership is verified by reading the parent scan with the caller's username (cross-member returns 404, so a scanned target's id can't be probed for); a per-scan cap (`share_link_max_per_scan`, default 5) prevents tokens from accumulating without bound; the `enable_share_links` knob (default 0) makes every endpoint 503 until an operator opts in, and `/api/health.share_links.{enabled, cache_max_age_sec}` lets a dashboard verify the configuration without firing a query; 15 new backend tests in `tests/test_share_links.py` cover storage primitives (mint, cap, lookup, expiry, revoke, cascade, purge-expired, etag stability), HTTP knob gate (mint / list / revoke / public 503 when off), end-to-end mint → list → public read → 304 revalidation → revoke round trip, cross-member privacy guard, and parent-scan delete cascade)

### Phase 9 — distribution ergonomics  *(out-of-scope, gated on user demand)*

> Items not on the committed plan. They close gaps in maigret's distribution
> surface (single-CLI binary, plugin community), not accuracy / scale, so
> they're parked until explicitly requested.

- [ ] **9a** — `reckora-cli` thin CLI wrapper (~200 LOC)
- [ ] **9b** — plugin SDK (`backend/app/plugins/` + stable `Site` ABC)
- [ ] **9c** — single-binary release (PyInstaller / Nuitka)

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

## Out of scope

Items the user has not asked for but that are natural follow-ons:

- **Phase 9 (CLI parity)**: a thin `reckora-cli` wrapper for users who prefer
  one-liner CLI scans (maigret's strongest distribution surface). Estimated
  ~200 LOC; would also require packaging the backend as a `pip install`-able
  wheel with an `[api]` extra.
- **Phase 9 (plugin SDK)**: `app/plugins/` directory + a stable `Site` ABC so
  the community can drop in adapters for niche sites without forking
  `sites.json`.
- **Phase 9 (single-binary release)**: PyInstaller / Nuitka bundle for users
  who want a single-file distribution.

These three Phase 9 items are about *distribution ergonomics*, not accuracy
or scale, so they're parked until a downstream user explicitly asks for them.

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

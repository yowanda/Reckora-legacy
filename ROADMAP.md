# Reckora roadmap — Phase 5h → Phase 8

This roadmap is the **single source of truth** for Reckora's planned + in-flight
work. Anything not on this list is not committed-to. Items are ticked off when
the change is merged into `main`, CI is green, and (where applicable) the live
HF Space at <https://yesmyyt-cacadoxx.hf.space> reflects the change.

---

## Live status

| Field | Value |
| --- | --- |
| Repo HEAD on main | `784ac59` (Phase 5h-FE — SPA signal badges + Stats counters + Playwright e2e) |
| HF Space HEAD | `784ac59` (mirrors main; Docker rebuilt + /api/health 200 verified) |
| Active batch | **Phase 6c — head-to-head benchmark vs maigret** |
| Last batch shipped | Phase 6b — engine-aware probe heuristics (XenForo/Discourse/vBulletin/uCoz) + 13 tests |
| Last update | 2026-05-06 (Phase 6b — engine probes) |
| Test baseline | backend pytest **241 / 241** · mypy 18 src files · ruff clean · frontend lint+build green · e2e **4 / 4** |

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

### Phase 6 — accuracy & gap closure vs maigret  *(planned)*

> **Goal:** quantitatively match or beat `soxoj/maigret` on every dimension where
> maigret is currently ahead (raw site count, per-site identity-type annotations).
> Make the gain measurable with a head-to-head benchmark.

- [x] **6a** — regenerate `sites.json` from `soxoj/maigret` upstream (3,154 raw → 2,506 active; auto-import `identityTypes` on 8 sites)
- [x] **6b** — engine-aware probe heuristics (XenForo / Discourse / vBulletin / uCoz) + `engine_probes.py` + 13 tests
- [ ] **6c** — head-to-head benchmark vs `soxoj/maigret` CLI (precision / recall / p95 / BLOCKED-rescue rate)
- [ ] **6d** — PDF export (resurrected from Phase 5b deferral)

### Phase 7 — intel & enrichment beyond maigret  *(planned)*

> **Goal:** capabilities `soxoj/maigret` does not have. Each sub-phase opens a new
> dimension of OSINT signal.

- [ ] **7a** — profile-image perceptual-hash (pHash) graph stitching
- [ ] **7b** — WHOIS / HIBP-style breach enrichment for `identity_type=email`
- [ ] **7c** — reverse-image lookup hook (Yandex / Google Lens)
- [ ] **7d** — ML reranker retrain pipeline + drift detector

### Phase 8 — scale & infra  *(planned)*

> **Goal:** make Reckora scale horizontally so a `10k`-site catalog and `100s`
> of concurrent operator scans don't melt a single `uvicorn` process.

- [ ] **8a** — distributed worker fleet (Celery + Redis queue)
- [ ] **8b** — rate-limit-aware proxy rotator (multi-proxy pool + per-host token bucket)
- [ ] **8c** — GraphQL gateway (alternate transport over the existing REST/SSE)
- [ ] **8d** — public-scan edge cache (CDN-friendly static rendering for shared scans)

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

### 7a — profile-image perceptual-hash (pHash) graph stitching

- Extend `backend/app/extractor.py` to capture profile-image URLs (already
  scraped via socid_extractor in some cases).
- New `backend/app/imagephash.py` — fetches the image (`GET` with size limit),
  computes a perceptual hash (`PIL` + `imagehash`), stores the 64-bit fingerprint
  in `scans` history.
- Graph view stitches an extra edge between two CLAIMED hits if their pHash
  Hamming distance is `≤ 6` — strong signal that the same person owns both
  accounts even if usernames differ.
- Acceptance: Phase 3 graph view renders pHash-derived edges as a distinct
  edge type; admin knob `image_phash_max_distance` (default `6`) tunes
  sensitivity.

### 7b — WHOIS / HIBP-style breach enrichment for `identity_type=email`

- New `backend/app/enrichment.py` calls a configurable WHOIS provider for
  domains discovered in CLAIMED email scans, and (admin-keyed) HIBP for
  breach exposure.
- Result rendered as a side panel in the SearchView when the active scan is
  `identity_type=email`.
- Acceptance: an email scan against a known-breached test address surfaces
  the breach list; rate-limited (default `5/min` admin-tunable).

### 7c — reverse-image lookup hook

- For each pHash captured in 7a, expose a "reverse-search" button that opens
  Yandex Images / Google Lens with the image URL pre-filled (no API key
  required — uses public landing-page URL pattern).
- Acceptance: graph node images are clickable and open the reverse-search
  in a new tab.

### 7d — ML reranker retrain pipeline + drift detector

- `backend/scripts/train_ml.py` already exists; Phase 7d wires it into a
  monthly admin-triggered retrain that pulls features from `scans` history.
- New `backend/app/ml_drift.py` computes a calibration KS-stat between the
  reranker's score distribution at deploy time vs current production traffic;
  surfaces a warning in the admin panel when drift is statistically significant.
- Acceptance: admin panel shows "model age", "last retrain", "drift status"
  per knob.

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

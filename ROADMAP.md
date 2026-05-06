# Reckora roadmap (Phase 6–8)

This roadmap continues from Phase 5 (operator polish, shipped 6/6 sub-phases — see
[`README.md`](./README.md) for the Phase 5 changelog). Phases 1-5 brought Reckora to
**parity with `soxoj/maigret` plus a confidence-scoring + ML-rerank + graph-recursive +
admin-tunable supersession** of every maigret feature. Phase 6 closes the remaining
*coverage* gap (site count + per-site identity-type annotations) — Phase 7 pushes
beyond maigret on intel/enrichment — Phase 8 makes the platform horizontally
scalable so `>10k`-site catalogs and team-scale operator workloads stay snappy.

Each sub-phase is scoped to land **direct-to-main** with green CI (backend mypy +
ruff, frontend eslint + tsc + build, Playwright e2e) and an auto-deploy to the live
HF Space, mirroring Phase 5's workflow.

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

# Changelog

All notable changes to Reckora are documented here. Entries are grouped by
phase (matching `ROADMAP.md`), and each phase lists the user-visible
`feat(...)` and `fix(...)` commits that landed on `main`.

The format is loosely based on [Keep a Changelog][kac]. Reckora does **not**
publish versioned releases yet — phases are the de-facto release boundary,
and the live demo at <https://yesmyyt-cacadoxx.hf.space> tracks `main`.

[kac]: https://keepachangelog.com/en/1.1.0/

---

## [Unreleased]

### Phase 10 — close the maigret gap (in flight)

Phase 10 closes the remaining capability gaps vs. `soxoj/maigret`. Every
sub-phase ships behind a default-off knob so the live HF Space deployment
stays byte-for-byte identical until an operator opts in.

#### 10i — public OSS launch governance docs
* `docs(10i)`: open-source the repo with `CODE_OF_CONDUCT.md` (Contributor
  Covenant 2.1), `CONTRIBUTING.md` (PR review flow + Phase-N runbook
  lifecycle + commit-message convention), `SECURITY.md`
  (responsible-disclosure address + 90-day fix window), `CHANGELOG.md`
  (one entry per phase), and a README that emphasises the differentiation
  vs. maigret. No runtime change, no new tests.

#### 10g — AI investigation summary
* `feat(10g)`: AI investigation summary with streaming markdown +
  runtime secrets store (default-off `enable_ai_summary` knob, opt-in
  per-admin API key with TTL).

#### 10h — extra export formats
* `feat(10h)`: XMind8 (`.xmind`), D3-HTML interactive force graph, and
  plain-text export formats added to `app.exports`. Existing CSV / JSON /
  Markdown / HTML / PDF formats unchanged.

#### 10d — recursive graph depth > 1
* `feat(10d)`: recursive graph depth > 1 with stricter secondary
  confidence floor + per-depth fanout knobs so deeper sweeps don't
  blow up the SSE payload.

#### 10b — tentative-sites catalog parity
* `feat(10b)`: tentative-sites catalog parity with default-off precision
  gate. Operators can opt in via `enable_tentative_sites=1` to widen
  the catalog by ~600 entries with a higher false-positive rate.

#### 10c — identity-types parity (+6)
* `feat(10c)`: identity-types parity — adds `vk_id`, `ok_id`, `steam_id`,
  `wikimapia_uid`, `uidme_uguid`, `yelp_userid` so Reckora's identity
  picker matches maigret's coverage.

#### 10a — auto-PDF on initial CLAIMED hit
* `feat(10a)`: streaming auto-PDF preliminary export on initial CLAIMED
  hit, so operators get a shareable artifact before the full scan
  finishes.

#### admin-knobs hotfix (between 9h and 10a)
* `fix(admin)`: expose all `admin_settings` knobs in
  `AdminSettingsPatch` so the SPA admin panel could PATCH every
  registered knob (previously the schema lagged behind the registry).

---

## Phase 9 — anti-bot probe pipeline + scale-out reliability (shipped)

Phase 9 made Reckora resilient to Cloudflare-class anti-bot defences and
scaled the multi-proxy fleet horizontally.

* `feat(9h)`: HF Space keep-warm cron (closes M7) — pings the Space on a
  schedule so the first user request doesn't pay cold-start latency.
* `feat(9g)`: proxy reputation warm-up + weighted selection (closes M6) —
  the rotator now favours proxies with healthy recent track records and
  warms up new proxies before sending bulk traffic through them.
* `feat(9f)`: per-scan hard budget + SKIPPED bucket — operators can cap
  wall-clock per scan; sites that don't make the budget land in a clear
  SKIPPED bucket instead of being silently dropped.
* `feat(9e)`: quarterly catalog auto-refresh from upstream
  `soxoj/maigret` (one PR per quarter, behind a `chore(catalog)` commit).
* `feat(9d)`: CAPTCHA classifier + structured UNKNOWN labelling — UNKNOWN
  results now carry a sub-reason (`captcha`, `403_login`, `5xx_retry_exhausted`).
* `feat(9c)`: geo-aware proxy selection — proxies are tagged with their
  exit country and the rotator can prefer a region for a given target.
* `feat(9b)`: distributed host rate limit (Redis sliding-window) — the
  per-host token bucket from 8b now coordinates across the entire
  Celery worker fleet.
* `feat(9a)`: anti-bot probe pipeline — TLS / JA3 / JA4 impersonation
  via `curl-cffi`, with a Playwright fallback for the hardest sites.

---

## Phase 8 — distributed scale-out (shipped)

Phase 8 turned the single-process checker into a horizontally-scalable
service.

* `feat(8d)`: public-scan edge cache for shared scans — repeated hits on
  the same shared scan ID are served from cache without re-running the
  pipeline.
* `feat(8c)`: GraphQL gateway as alternate transport over REST/SSE
  (`[gql]` extra; default-off, REST/SSE remains the canonical
  transport).
* `feat(8b)`: rate-limit-aware proxy rotator — multi-proxy pool with a
  per-host token bucket so Reckora respects each upstream's rate limits.
* `feat(8a)`: distributed worker fleet via Celery + Redis (`[scale]`
  extra). Default-off; the single-process scheduler still works without
  the extra.
* `fix(ci, 8a)`: install `[scale]` extra on the backend job + skip Celery
  tests when the extra is absent so contributors who don't enable
  `[scale]` still get a green local baseline.

---

## Phase 7 — enrichment + ML reranker (shipped)

Phase 7 layered enrichment and learned scoring on top of Phase 6's catalog
work.

* `feat(7d)`: ML reranker retrain pipeline + drift detector — the bundled
  ONNX model can be retrained from labelled scan history and the drift
  detector flags when retraining is overdue.
* `feat(7c)`: reverse-image lookup hook — Yandex / Google Lens / TinEye
  URL builder, 21 tests, Playwright e2e.
* `feat(7b)`: WHOIS / HIBP-style breach enrichment for email scans —
  RDAP + HIBP fetchers, 33 tests, Playwright e2e.
* `feat(7a)`: profile-image pHash graph stitching — Pillow-only dHash,
  24 tests, Playwright e2e.

---

## Phase 6 — catalog + benchmark + PDF export (shipped)

Phase 6 closed the remaining catalog gap with maigret and shipped a
PDF report.

* `feat(6d)`: PDF export via `weasyprint` (optional `[pdf]` extra; the
  HTML fallback stays available when the extra isn't installed).
* `feat(6c)`: head-to-head benchmark script Reckora vs. maigret + initial
  report (`comparison-maigret.md`).
* `feat(6b)`: engine-aware probe heuristics (XenForo, Discourse,
  vBulletin, uCoz) + 13 tests.
* `feat(6a)`: regenerate `sites.json` from upstream `soxoj/maigret` +
  `identityTypes` auto-import — the catalog jumps to ~2 506 active sites.

---

## Phase 5 — UX polish + scan ergonomics (shipped)

Phase 5 broke into eight sub-phases (5a → 5h) covering identity types,
exports, permutations, admin knobs, scan-depth, infra hardening, and
the post-claim precision floor.

* `feat(5h)`: claimed precision floor as admin knobs — multi-layer
  post-claim demotion (link-liveness probe, dual-URL check, conf floor,
  ML floor, verify-required) shipped across batches 1 → 5.
* `feat(5g)`: scan-depth picker so a single search can probe all 2 506
  sites (`SearchRequest.limit` cap raised to `le=3000`).
* `feat(5f)`: pre-commit, mypy gate, ruff format, Playwright e2e —
  infrastructure hardening.
* `feat(5e)`: outbound proxy / Tor support (HTTP-only proxies; SOCKS /
  Tor / I2P transport adapter still pending in 10e).
* `feat(5d)`: runtime knobs for graph fanout + extractor floor.
* `feat(5c)`: username permutation scan (`/api/search/permute` SSE).
* `feat(5b)`: scan export (CSV / JSON / Markdown / HTML).
* `feat(5a)`: identity types — email / phone / GAIA / Yandex / Skype.
* `fix(tests)`: apply `ruff format` to `test_proxy.py` so the pre-commit
  formatter no-ops on a fresh clone.

---

## Phase 4 — ML classifier + admin live DB editor + scan history (shipped)

* `feat(phase-4)`: ML classifier (8-feature ONNX LogisticRegression,
  ~1 KB), live site DB editor (`POST /api/admin/sites` with override
  merge), per-member scan history with `GET /api/history/diff/{a}/{b}`.

---

## Phase 3 — recursive identity extraction + react-flow graph view (shipped)

* `feat(phase-3)`: recursive identity extraction + `react-flow` graph
  view, `/api/search/graph` SSE with depth-1 recursion and bounded
  fanout.

---

## Phase 2 — anti-bot rescue + 2-stage verify (shipped)

* `phase 2.2`: `curl-cffi` Chrome-TLS rescue for blocked sites.
* `phase 2.1`: per-host cookie prewarm + 2-stage verify (rotated UA on
  the second probe).

---

## Phase 1 — engine expansion (shipped)

* `phase 1`: engine expansion + confidence + retry + BLOCKED + HEAD /
  HTTP/2 / brotli — first end-to-end pipeline running against the full
  catalog.

---

## Phase 0 — initial app (shipped)

* `feat(backend)`: HTTP cache-busting headers for single-origin SPA.
* `refactor`: rename project to **Reckora** and refresh OSINT UI.
* `fix(deploy)`: default uvicorn port to 7860 + link live HF Space in
  README.
* `feat(auth)`: members-only login + admin panel for member management.
* `Initial commit`.

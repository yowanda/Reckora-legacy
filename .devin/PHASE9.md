# Phase 9 — Reckora (Single Source of Truth)

> **Audience**: Devin sessions resuming Phase 9 implementation work.
> **Goal**: Skip the "context-loading" step and start producing commits in <2% quota.
>
> This file replaces the prior split into `PHASE9_RUNBOOK.md` + `phase9_progress.json`. Read this file FIRST in every new session.

---

## 0. The 30-second context

- **Repo**: <https://github.com/yowanda/Reckora>
- **Branch**: `main` (direct push, **no PRs**, never force-push)
- **Live demo**: <https://yesmyyt-cacadoxx.hf.space> (HF Space; behind `main` HEAD until full Phase 9 deploy — see §7)
- **Author/committer**: `yowanda <yodadwi4@gmail.com>` (NOT the default Devin author — see §1)
- **Active phase**: 9g (9a + 9b + 9c + 9d + 9e + 9f shipped — see §3 status table)

Phase 9 closes residual risks from the Phase 8 deploy debrief.

---

## 1. Git push method (the ONLY method)

The user has explicitly required this method to bypass Devin's auto co-author and use a stable author identity. **Do not deviate.**

```bash
# Commit (write the body to a file first to avoid shell-escape bugs)
HOME=/tmp/empty-home \
GIT_AUTHOR_NAME=yowanda \
GIT_AUTHOR_EMAIL=yodadwi4@gmail.com \
GIT_COMMITTER_NAME=yowanda \
GIT_COMMITTER_EMAIL=yodadwi4@gmail.com \
command git commit -F /tmp/commit-msg-<phase>.txt

# Push direct to main (no PR)
HOME=/tmp/empty-home command git push origin main
```

Why each piece matters:

- `HOME=/tmp/empty-home` — strips Devin's `~/.gitconfig` so its auto co-author trailer is not appended.
- `GIT_AUTHOR_*` / `GIT_COMMITTER_*` — pins the identity to `yowanda`.
- `command git ...` — bypasses any `git` shell alias the env may have set.
- `-F <file>` — passes a multi-line commit message via file so shell quoting bugs cannot corrupt the body.

**Forbidden:**
- ❌ `git commit -m "..."` with a multi-line message (use `-F` instead)
- ❌ `git push --force` or `git push -f` to `main`
- ❌ `git commit --amend` on a pushed commit
- ❌ Creating PRs (the user has explicitly opted out of the PR workflow)
- ❌ `git add .` (always stage explicit paths)
- ❌ Skipping pre-commit hooks (`--no-verify`)

---

## 2. Per-batch workflow (feat → CI → docs → report)

For every Phase 9 sub-phase the cycle is identical:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Read sub-phase spec from ROADMAP.md (master checklist line 100-110)      │
│ 2. Implement code + tests + admin knob                                      │
│ 3. Local sweep: ruff check . / ruff format --check . / mypy / pytest -q     │
│ 4. Write commit message file: /tmp/commit-msg-<phase>.txt                   │
│ 5. git add <explicit paths>                                                 │
│ 6. Commit feat(<phase>) using §1 method                                     │
│ 7. git push origin main                                                     │
│ 8. Wait for CI: GitHub Actions ~3-5 min for backend+frontend+e2e            │
│    → poll: curl GH check-runs API every 90s until all 3 conclusion=success  │
│ 9. Update ROADMAP.md (mark [x], bump Repo HEAD, update Last batch shipped)  │
│ 10. Update README.md (Phase 9 row in milestone table + lead-in paragraph)   │
│ 11. Update §3 of THIS file (flip phase to "shipped", advance active_batch)  │
│ 12. Commit docs(<phase>) using §1 method                                    │
│ 13. git push origin main                                                    │
│ 14. Wait for docs CI green (same poll pattern)                              │
│ 15. message_user: report both commit SHAs, test baseline, summary           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Local sweep commands

```bash
cd /home/ubuntu/repos/Reckora/backend

uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy

# Pytest — choose ONE based on coverage you want:
uv run --extra dev pytest -q                               # 551 + 7 skipped (no Redis, no GraphQL)
uv run --extra dev --extra scale --extra gql pytest -q     # 551 + 7 skipped (Redis tests skipped without URL)
RECKORA_TEST_REDIS_URL=redis://localhost:6379 \
  uv run --extra dev --extra scale --extra gql pytest -q   # 558 / 558 (full coverage)
```

The 558 / 558 baseline is the **gold standard** for any commit that touches `host_rate_limit_redis.py`. CI runs this combo via the `redis:7-alpine` service container declared in `.github/workflows/ci.yml`.

### CI poll command (after `git push`)

```bash
SHA=<commit-sha>
curl -sS \
  -H "Authorization: Bearer ${PAT_GITHUB}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/yowanda/Reckora/commits/${SHA}/check-runs" \
| python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data.get('check_runs', []):
    print(f\"{r['name']:20s} status={r['status']:12s} conclusion={r.get('conclusion') or 'pending'}\")"
```

Expected: 3 check_runs (`backend`, `frontend`, `e2e (Playwright)`), all `status=completed conclusion=success`. e2e takes the longest (~3-5 min after backend + frontend finish in ~1-2 min).

### Commit message template

```
<type>(<phase>): <one-line summary>

* <bullet 1>
* <bullet 2>
...
* `Test baseline` <old> → <new>; mypy <N> src files clean
* `Closes residual-risk <ID>` (<severity>)
```

`<type>` is `feat` for code/tests, `docs` for ROADMAP/README/this-file updates. `<phase>` is `9a`, `9b`, …, `9h`.

---

## 3. Phase 9 state (machine-readable + human-readable)

### Status table

| Phase | Title | Risk closed | Status | feat SHA | docs SHA | Tests added |
| --- | --- | --- | --- | --- | --- | --- |
| 9a | anti-bot probe pipeline | C1, C2 (CRITICAL) | shipped | [`b911a72`](https://github.com/yowanda/Reckora/commit/b911a72) | [`7e8073e`](https://github.com/yowanda/Reckora/commit/7e8073e) | 41 |
| 9b | distributed host rate limit (Redis sliding-window) | M1 (MEDIUM) | shipped | [`b6073af`](https://github.com/yowanda/Reckora/commit/b6073af) | [`3d75879`](https://github.com/yowanda/Reckora/commit/3d75879) | 15 |
| 9c | geo-aware proxy selection | M2 (MEDIUM) | shipped | [`ecbebef`](https://github.com/yowanda/Reckora/commit/ecbebef) | [`085fb95`](https://github.com/yowanda/Reckora/commit/085fb95) | 18 |
| 9d | CAPTCHA classifier + structured UNKNOWN labelling | M3 (MEDIUM) | shipped | [`18903bc`](https://github.com/yowanda/Reckora/commit/18903bc) | [`d05fb62`](https://github.com/yowanda/Reckora/commit/d05fb62) | 26 |
| 9e | quarterly catalog auto-refresh from upstream maigret | M4 (MEDIUM) | shipped | [`ac9bbdf`](https://github.com/yowanda/Reckora/commit/ac9bbdf) | [`47e1a23`](https://github.com/yowanda/Reckora/commit/47e1a23) | 24 |
| 9f | per-scan hard budget with new SKIPPED bucket | M5 (MEDIUM) | shipped | [`7dd7bfd`](https://github.com/yowanda/Reckora/commit/7dd7bfd) | _pending docs commit_ | 17 |
| 9g | proxy reputation warm-up + weighted selection | M6 (MEDIUM) | pending | — | — | — |
| 9h | HF Space keep-warm cron | M7 (MEDIUM) | pending | — | — | — |

### Test baseline ratchet

| Phase | Baseline (default `[dev]`) | Baseline (`[scale]+[gql]`+Redis) |
| --- | --- | --- |
| 8d (pre-9a) | 397 + 7 skipped | 417 |
| 9a | 438 + 7 skipped | 458 (+41 tests) |
| 9b | 466 + 7 skipped | 473 (+15 tests) |
| 9c | 484 + 7 skipped | 491 (+18 tests) |
| 9d | 510 + 7 skipped | 517 (+26 tests) |
| 9e | 534 + 7 skipped | 541 (+24 tests) |
| 9f | 551 + 7 skipped | 558 (+17 tests) |
| 9g (target) | ~565 + 7 skipped | ~575 (+18 tests) |

**The number must monotonically grow.** Never delete or weaken existing tests.

### Machine-readable state (JSON)

```json
{
  "schema_version": 2,
  "last_updated": "2026-05-07T07:30:00Z",
  "session": {
    "git_author": "yowanda <yodadwi4@gmail.com>",
    "branch": "main"
  },
  "repo": {
    "url": "https://github.com/yowanda/Reckora",
    "head": "7dd7bfd",
    "live_demo": "https://yesmyyt-cacadoxx.hf.space"
  },
  "test_baseline": {
    "default_dev_extra": "551 passed + 7 skipped",
    "with_scale_and_gql_extras": "558 passed (with RECKORA_TEST_REDIS_URL set)",
    "mypy_source_files": 34
  },
  "phases": {
    "9a": {"status": "shipped", "feat": "b911a72", "docs": "7e8073e", "tests_added": 41},
    "9b": {"status": "shipped", "feat": "b6073af", "docs": "3d75879", "tests_added": 15},
    "9c": {"status": "shipped", "feat": "ecbebef", "docs": "085fb95", "tests_added": 18},
    "9d": {"status": "shipped", "feat": "18903bc", "docs": "d05fb62", "tests_added": 26},
    "9e": {"status": "shipped", "feat": "ac9bbdf", "docs": "47e1a23", "tests_added": 24},
    "9f": {"status": "shipped", "feat": "7dd7bfd", "docs": null, "tests_added": 17},
    "9g": {"status": "pending", "feat": null, "docs": null, "tests_added": null},
    "9h": {"status": "pending", "feat": null, "docs": null, "tests_added": null}
  },
  "active_batch": {
    "phase": "9g",
    "blocking": true,
    "blocking_reason": "User requested pause after 9f full ship; awaiting explicit signal before starting 9g",
    "next_action": "Wait for user signal, then implement Phase 9g proxy reputation warm-up + weighted selection — see §4 for file-touch map"
  },
  "deploy_strategy": {
    "decision_date": "2026-05-07",
    "locked_in": true,
    "decision": "Wait until Phase 9 is fully complete (9a-9h shipped + green CI) before mirroring to HF Space. After 9h ships, do ONE deploy + activate pass."
  }
}
```

When you advance state at the end of a batch, edit BOTH the table above AND the JSON block (keep them in sync). Use the `docs(<phase>)` commit step (#11 in §2) to do so.

---

## 4. File-touch map for remaining sub-phases

The Phase 9 design pattern: a **new module** + a **wiring point in `checker.py`** + an **admin knob** + a **test file**.

### 9d — CAPTCHA classifier
- NEW: `backend/app/captcha_signatures.py`
- WIRE: `backend/app/checker.py` (line ~928, inside the `_is_blocked_body` branch)
- KNOB: `backend/app/config.py` + `backend/app/admin_settings.py` (e.g. `enable_captcha_classifier`)
- ADMIN: `backend/app/main.py` (`/api/admin/ml/status` adds `captcha_vendors_seen`)
- DATACLASS: `backend/app/checker.py` `CheckResult.reason: str | None = None` (new field; update `to_dict()` and `graphql_api.CheckResultInfo`)
- TESTS: `backend/tests/test_captcha_signatures.py` (~15 tests)

### 9e — quarterly catalog auto-refresh
- NEW: `backend/scripts/refresh_catalog.py`
- WIRE: `backend/app/main.py` (admin endpoint to trigger), GitHub Actions cron in `.github/workflows/`
- TESTS: `backend/tests/test_catalog_refresh.py`

### 9f — per-scan hard budget + SKIPPED bucket
- NEW: `backend/app/scan_budget.py`
- WIRE: `backend/app/run_scan.py` (or wherever the scan orchestrator lives)
- KNOB: `enable_scan_budget` + `scan_budget_max_seconds`
- TESTS: `backend/tests/test_scan_budget.py`

### 9g — proxy reputation warm-up
- NEW: `backend/app/proxy_reputation.py`
- WIRE: `backend/app/proxy_pool.py` (extends `ProxyPool.acquire()` with weighted selection)
- KNOB: `enable_proxy_reputation` + `proxy_reputation_decay_seconds`
- TESTS: `backend/tests/test_proxy_reputation.py`

### 9h — HF Space keep-warm cron
- NEW: `.github/workflows/hf-space-keep-warm.yml`
- NEW: `backend/scripts/hf_space_keep_warm.py` (small ping script)
- TESTS: lightweight smoke test only (cron is GH Actions native)

---

## 5. Phase 9d design decisions (locked in)

When resuming 9d, follow these decisions to avoid re-derivation:

- **Signature format**: regex over response body text (fast, no DOM parser needed); per-vendor compiled regex precomputed at import time so the hot path is O(N_vendors) per probe with N_vendors~10.
- **`reason` field design**: new optional `CheckResult.reason: str|None`; default `None` preserves byte-for-byte compatibility with all existing callers and serialised payloads.
- **Detection position**: runs *inside* the existing `_is_blocked_body` branch in `checker.py`, *not* before it, so we don't accidentally upgrade a generic Cloudflare interstitial to a more specific reason if the cookie-prewarm path already would have flagged it; CAPTCHA signature match always wins over the generic body fingerprint when both fire.
- **Counter storage**: in-process `collections.Counter` wrapped in a `threading.Lock`; `reset_counts_for_tests()` helper for unit tests; never persisted to disk because operators only need a "this week" snapshot.
- **Admin endpoint extension**: `GET /api/admin/ml/status` returns the existing payload + new `captcha_vendors_seen: {vendor: count}` key; no new endpoint, no SPA changes.
- **Vendor list (~10)**: reCAPTCHA v2, reCAPTCHA v3, hCaptcha, Cloudflare Turnstile, Datadome, AWS WAF Captcha, FunCaptcha, Geetest, generic Apache `<form action="/captcha">`, PerimeterX (already partially in `_BLOCK_FINGERPRINTS`).

---

## 6. Phase 9 default-off contract (CRITICAL)

**Every Phase 9 admin knob defaults to 0** (or empty string for URL fields). This is non-negotiable.

When an operator opts in:
- `enable_anti_bot_pipeline=1` (9a) — needs `pip install playwright && playwright install chromium` in Dockerfile, else `chromium_available()` returns False and fallback is a graceful no-op.
- `host_rate_limit_use_redis=1` (9b) — needs `RECKORA_HOST_RATE_LIMIT_REDIS_URL` set; else fail-open to in-memory bucket.
- `enable_geo_aware_proxy=1` (9c) — needs `RECKORA_PROXY_POOL_URLS` with `region:url` syntax; else 3-pass acquire collapses to single-region round-robin (no-op).

Tests **must** verify the default-off path matches the pre-phase behaviour.

---

## 7. Deploy & activate strategy (LOCKED IN)

**Decision date**: 2026-05-07. **Decision**: Wait until Phase 9 is fully complete (9a–9h shipped + green CI) before mirroring to HF Space. After 9h ships, do **one** deploy + activate pass:

1. Mirror `main` HEAD → HF Space remote (`huggingface.co/spaces/<owner>/<space>`). Credentials: `HF_TOKEN` already saved in Devin secrets.
2. Update `Dockerfile` to install Playwright + Chromium (for 9a activation).
3. Provision a Redis service (managed Redis URL) for 9b activation.
4. Configure `RECKORA_PROXY_POOL_URLS` with `region:url` syntax for 9c.
5. Flip admin knobs in order: 9a → 9b → 9c → 9d → 9f → 9g (9e and 9h are infra-only, no knob).
6. Smoke test each capability via the live demo.

**Reasoning**: avoids the "half-deployed Phase 9" state where some capabilities are live and others are not — operators only need to learn the activation sequence once.

---

## 8. Resume protocol for new sessions

1. **Read this file** (~30s, ~1% quota).
2. **Check repo state**:
   ```bash
   cd /home/ubuntu/repos/Reckora
   git fetch origin && git log --oneline -5
   git status --short  # must be clean
   ```
3. **Identify active phase** from §3 → `active_batch.phase`.
4. **Read the corresponding line in ROADMAP.md** master checklist (lines ~100-110) for the full spec.
5. **Pre-flight check**:
   ```bash
   cd backend && uv run --extra dev pytest -q --collect-only | tail -3
   ```
   Should show ~491 tests collected (or whatever the current baseline says).
6. **Start the per-batch workflow** from §2 step 1.

---

## 9. Estimated quota per batch (empirical)

| Activity | Quota |
| --- | --- |
| Read context (this file + ROADMAP/README + key source files) | ~3-5% |
| Implement new module + wire-in | ~5-8% |
| Write ~15-20 tests | ~3-5% |
| Local sweep (ruff + mypy + pytest) | ~2-3% |
| Commit feat + push | ~1-2% |
| Wait CI green (~5 min, mostly idle) | ~3-5% |
| Write commit message + update ROADMAP/README/this file | ~3-5% |
| Commit docs + push + wait CI | ~2-3% |
| Report to user | ~1% |
| **Total per batch** | **~20-25%** |

A new session needs **~20-25% quota** for one Phase 9 batch.
- 80%+ free → 3-4 batches in one session
- 40-60% free → 1-2 batches safely
- 20-30% free → 1 batch with margin
- <15% free → save progress, end session, defer to next

Whenever quota drops below 15%, **immediately**:
1. Update §3 state in this file
2. Commit via §1 method
3. Push
4. Send a checkpoint `message_user` with this file attached
5. Block on user

---

## 10. Common pitfalls (lessons from past batches)

- **mypy strict literal mismatch on curl-cffi** (Phase 9a): when calling `AsyncSession(impersonate=...)`, cast the str to the union literal type manually (mypy can't infer from `Settings.field`). See `checker_cffi.py` for the pattern.
- **ruff SIM105** (Phase 9b): `try: ... except Exception: pass` — replace with `contextlib.suppress(Exception)`.
- **Pre-commit hook hangs on big diffs**: hooks are in `.git/hooks/` so `HOME=/tmp/empty-home` doesn't disable them. If a hook auto-fixes files, re-`git add` and retry the commit.
- **`git diff --merge-base`**: only works after `git fetch --unshallow` if the clone is shallow. Check with `git rev-parse --is-shallow-repository`.
- **Pytest collection time**: `uv run pytest -q --collect-only | wc -l` is cheaper than running the full suite to verify test discovery is healthy.
- **Don't `git add .`**: always stage explicit paths.

---

## 11. Rules (recap)

- All Phase 9 admin knobs default to 0 (or empty string for URL fields). HF Space stays byte-for-byte unchanged until an operator opts in.
- **No PRs.** User has explicitly requested direct push to main.
- **No force-push.** Never to `main`.
- **No Devin co-author.** Always use `GIT_AUTHOR_NAME=yowanda` + `GIT_AUTHOR_EMAIL=yodadwi4@gmail.com` for every commit.
- **No frontend changes in Phase 9.** Phase 9 is backend + infra only.
- **Test baseline must grow.** Each sub-phase adds tests; never delete or weaken existing tests.
- **CI must pass before docs commit.** feat → wait CI green → docs. Both pushed direct to main.

---

*Last updated: 2026-05-07 — Phase 9c shipped, 9d–9h pending, deploy strategy locked-in.*

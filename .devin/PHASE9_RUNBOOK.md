# Phase 9 Runbook — Reckora

**Audience**: Devin sessions resuming Phase 9 implementation work.
**Goal**: Skip the "context-loading" step and start producing commits in <2% quota.

This file is the operational complement to `.devin/phase9_progress.json`.
The JSON tracks **what has shipped**; this runbook tracks **how to ship it**.

---

## 0. The 30-second context

- **Repo**: <https://github.com/yowanda/Reckora>
- **Branch**: `main` (direct push, **no PRs**, never force-push)
- **Live demo**: <https://yesmyyt-cacadoxx.hf.space> (HF Space; behind `main` HEAD until full Phase 9 deploy — see §6)
- **Author/committer**: `yowanda <yodadwi4@gmail.com>` (NOT the default Devin author — see §1)
- **Active phase**: see `phase9_progress.json` → `active_batch.phase`

Phase 9 closes residual risks from the Phase 8 deploy debrief:

| Phase | Risk closed | Status (see JSON for SHAs) |
| --- | --- | --- |
| 9a | C1, C2 (CRITICAL) — anti-bot pipeline | shipped |
| 9b | M1 (MEDIUM) — distributed host rate limit | shipped |
| 9c | M2 (MEDIUM) — geo-aware proxy selection | shipped |
| 9d | M3 (MEDIUM) — CAPTCHA classifier + structured UNKNOWN | pending |
| 9e | M4 (MEDIUM) — quarterly catalog auto-refresh | pending |
| 9f | M5 (MEDIUM) — per-scan hard budget + SKIPPED bucket | pending |
| 9g | M6 (MEDIUM) — proxy reputation warm-up | pending |
| 9h | M7 (MEDIUM) — HF Space keep-warm cron | pending |

---

## 1. Git push method (the ONLY method)

The user has explicitly required this method to bypass Devin's auto co-author
and use a stable author identity. **Do not deviate.**

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

- `HOME=/tmp/empty-home` — strips Devin's `~/.gitconfig` so its auto co-author
  trailer is not appended.
- `GIT_AUTHOR_*` / `GIT_COMMITTER_*` — pins the identity to `yowanda`.
- `command git ...` — bypasses any `git` shell alias the env may have set.
- `-F <file>` — passes a multi-line commit message via file so shell quoting
  bugs cannot corrupt the body.

**Forbidden**:

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
│ 11. Update .devin/phase9_progress.json (flip phase to "shipped",            │
│     advance active_batch.phase to the next sub-phase)                       │
│ 12. Commit docs(<phase>) using §1 method                                    │
│ 13. git push origin main                                                    │
│ 14. Wait for docs CI green (same poll pattern)                              │
│ 15. message_user: report both commit SHAs, test baseline, summary           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Local sweep commands

```bash
cd /home/ubuntu/repos/Reckora/backend

# Ruff (lint + format check)
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .

# Mypy
uv run --extra dev mypy

# Pytest — choose ONE based on coverage you want:
uv run --extra dev pytest -q                               # 484 + 7 skipped (no Redis, no GraphQL)
uv run --extra dev --extra scale --extra gql pytest -q     # 484 + 7 skipped (Redis tests skipped without URL)
RECKORA_TEST_REDIS_URL=redis://localhost:6379 \
  uv run --extra dev --extra scale --extra gql pytest -q   # 491 / 491 (full coverage)
```

The 491 / 491 baseline is the **gold standard** for any commit that touches
`host_rate_limit_redis.py`. CI runs this combo via the `redis:7-alpine`
service container declared in `.github/workflows/ci.yml`.

### CI poll command (after `git push`)

```bash
# Wait ~90s after push for CI to start, then poll every 90s.
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

Expected: 3 check_runs (`backend`, `frontend`, `e2e (Playwright)`), all
`status=completed conclusion=success`. e2e takes the longest (~3-5 min after
backend + frontend finish in ~1-2 min).

### Commit message template

```
<type>(<phase>): <one-line summary>

* <bullet 1>
* <bullet 2>
* <bullet 3>
...
* `Test baseline` <old> → <new>; mypy <N> src files clean
* `Closes residual-risk <ID>` (<severity>)
```

`<type>` is `feat` for code/tests, `docs` for ROADMAP/README/JSON updates.
`<phase>` is `9a`, `9b`, …, `9h`. **Always** use the file form (`-F`).

---

## 3. Phase 9 default-off contract (CRITICAL)

**Every Phase 9 admin knob defaults to 0** (or empty string for URL fields).
This is non-negotiable. Why:

- HF Space is a single-container deployment; we cannot guarantee Redis,
  Playwright, multi-region proxies, etc. are available.
- Default-off means a rebuild from `main` HEAD is **byte-for-byte equivalent**
  to the previous Phase 8 deploy (no behavioural change unless an operator
  flips the knob).
- This lets us ship code without coordinating infrastructure changes.

When an operator opts in:

- `enable_anti_bot_pipeline=1` (9a) — needs `pip install playwright && playwright install chromium` in Dockerfile, else `chromium_available()` returns False and fallback is a graceful no-op.
- `host_rate_limit_use_redis=1` (9b) — needs `RECKORA_HOST_RATE_LIMIT_REDIS_URL` set; else fail-open to in-memory bucket.
- `enable_geo_aware_proxy=1` (9c) — needs `RECKORA_PROXY_POOL_URLS` with `region:url` syntax; else 3-pass acquire collapses to single-region round-robin (no-op).

Tests **must** verify the default-off path matches the pre-phase behaviour.

---

## 4. File-touch map per sub-phase

The Phase 9 design pattern: a **new module** + a **wiring point in `checker.py`** + an **admin knob** + a **test file**. Files to expect for the remaining sub-phases:

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

## 5. Test baseline ratchet

| Phase | Baseline (default `[dev]`) | Baseline (`[scale]+[gql]`+Redis) |
| --- | --- | --- |
| 8d (pre-9a) | 397 + 7 skipped | 417 |
| 9a | 438 + 7 skipped | 458 (+41 tests) |
| 9b | 466 + 7 skipped | 473 (+15 tests) |
| 9c | 484 + 7 skipped | 491 (+18 tests) |
| 9d (target) | ~499 + 7 skipped | ~506 (+15 tests) |

**The number must monotonically grow.** Never delete or weaken existing tests.
If a test must be modified, the modification must keep the same set of
behaviours covered (e.g. unpacking a tuple return).

---

## 6. Deploy & activate strategy (LOCKED IN)

**Decision date**: 2026-05-07.
**Decision**: Wait until Phase 9 is fully complete (9a–9h shipped + green CI)
before mirroring to HF Space. After 9h ships, do **one** deploy + activate
pass:

1. Mirror `main` HEAD → HF Space remote (`huggingface.co/spaces/<owner>/<space>`).
   Credentials: `HF_TOKEN` already saved in Devin secrets.
2. Update `Dockerfile` to install Playwright + Chromium (for 9a activation).
3. Provision a Redis service (managed Redis URL) for 9b activation.
4. Configure `RECKORA_PROXY_POOL_URLS` with `region:url` syntax for 9c.
5. Flip admin knobs in order: 9a → 9b → 9c → 9d → 9f → 9g (9e and 9h are infra-only, no knob).
6. Smoke test each capability via the live demo.

**Reasoning**: avoids the "half-deployed Phase 9" state where some capabilities
are live and others are not — operators only need to learn the activation
sequence once.

---

## 7. Resume protocol for new sessions

1. **Read this file + `.devin/phase9_progress.json`** (~30s, ~1% quota).
2. **Check repo state**:
   ```bash
   cd /home/ubuntu/repos/Reckora
   git fetch origin && git log --oneline -5
   git status --short  # must be clean
   ```
3. **Identify active phase** from `active_batch.phase` in JSON.
4. **Read the corresponding line in ROADMAP.md** master checklist (lines ~100-110)
   for the full spec.
5. **Pre-flight check**:
   ```bash
   cd backend && uv run --extra dev pytest -q --collect-only | tail -3
   ```
   Should show ~491 tests collected (or whatever the current baseline says).
6. **Start the per-batch workflow** from §2 step 1.

If `active_batch.blocking=true`:
- Read `active_batch.blocking_reason` and `resume_instructions` in the JSON
- Address the blocker (usually: confirm with user that quota is safe)
- Then flip `blocking=false` and proceed

---

## 8. Estimated quota per batch (empirical from this session)

| Activity | Quota |
| --- | --- |
| Read context (`README.md`, `ROADMAP.md`, key source files) | ~3-5% |
| Implement new module + wire-in | ~5-8% |
| Write ~15-20 tests | ~3-5% |
| Local sweep (ruff + mypy + pytest) | ~2-3% |
| Commit feat + push | ~1-2% |
| Wait CI green (~5 min, mostly idle) | ~3-5% |
| Write commit message + update ROADMAP/README/JSON | ~3-5% |
| Commit docs + push + wait CI | ~2-3% |
| Report to user | ~1% |
| **Total per batch** | **~20-25%** |

A new session needs **~20-25% quota** for one Phase 9 batch.
Budget accordingly:
- 80%+ free → can do 3-4 batches in one session
- 40-60% free → 1-2 batches safely
- 20-30% free → 1 batch with margin
- <15% free → save progress, end session, defer to next

Whenever quota drops below 15%, **immediately**:
1. Update `.devin/phase9_progress.json` with current state
2. Commit the JSON via §1 method
3. Push
4. Send a checkpoint message_user with the JSON attached
5. Block on user

---

## 9. Common pitfalls (lessons from past batches)

- **mypy strict literal mismatch on curl-cffi** (Phase 9a): when calling
  `AsyncSession(impersonate=...)`, cast the str to the union literal type
  manually (mypy can't infer from `Settings.field`). See `checker_cffi.py`
  for the pattern.
- **ruff SIM105** (Phase 9b): `try: ... except Exception: pass` — replace
  with `contextlib.suppress(Exception)`.
- **Pre-commit hook hangs on big diffs**: hooks are in `.git/hooks/` so
  `HOME=/tmp/empty-home` doesn't disable them. If a hook auto-fixes files,
  re-`git add` and retry the commit (see git guidelines).
- **`git diff --merge-base`**: only works after `git fetch --unshallow` if
  the clone is shallow. Check with `git rev-parse --is-shallow-repository`.
- **Pytest collection time**: `uv run pytest -q --collect-only | wc -l` is
  cheaper than running the full suite to verify test discovery is healthy.
- **Don't `git add .`**: always stage explicit paths (`git add ROADMAP.md README.md backend/app/foo.py backend/tests/test_foo.py`). Avoids
  accidentally committing `.tmp/` or `__pycache__/`.

---

*Last updated: 2026-05-07 (Phase 9c shipped, 9d–9h pending).
This file is the source of truth for Phase 9 mechanics; the JSON is the
source of truth for Phase 9 state.*

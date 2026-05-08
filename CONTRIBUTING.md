# Contributing to Reckora

Thanks for taking an interest in Reckora. The project is small and opinionated,
so this guide is intentionally concrete: it tells you exactly how a change
lands on `main` and how that change reaches the live HF Space mirror.

If you'd rather just file a bug or request a feature, jump to
[Issues and feature requests](#issues-and-feature-requests).

---

## Table of contents

* [Code of conduct](#code-of-conduct)
* [Project shape](#project-shape)
* [Getting set up](#getting-set-up)
* [The Phase-N runbook lifecycle](#the-phase-n-runbook-lifecycle)
* [Commit-message convention](#commit-message-convention)
* [Pull request review flow](#pull-request-review-flow)
* [Default-off invariant](#default-off-invariant)
* [Tests, lint, and CI](#tests-lint-and-ci)
* [Issues and feature requests](#issues-and-feature-requests)
* [Security](#security)

---

## Code of conduct

This project follows the [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md).
By participating, you agree to abide by it. Violations can be reported to
**security@duriancompany.us**.

## Project shape

```
backend/    FastAPI + async checker + SSE streaming + SQLite admin store
frontend/   Vite + React + Tailwind SPA (single-origin in production)
data/       Curated site database derived from soxoj/maigret (MIT)
docs/       Long-form runbooks, benchmarks, deploy notes
ROADMAP.md  Single source of truth for planned + in-flight work
```

* `backend/app/checker.py` — per-site probe + verify pass + ML reranker.
* `backend/app/sites.py` — catalog loader + admin overrides + tag index.
* `backend/app/main.py` — FastAPI routes + SSE streams + auth gate.
* `backend/app/admin_settings.py` — knob registry, 5s-TTL cache, env-var fallback.
* `backend/app/cli/` — `reckora` console-script (Phase 10f, see ROADMAP).
* `frontend/src/` — SPA components, types, Playwright e2e suite.

`ROADMAP.md` is the single source of truth. If you're not sure whether a
change makes sense, check the roadmap first — every shipped feature has a
sub-phase with its own scope.

## Getting set up

You need:

* Python 3.11 (the backend pins to it)
* [`uv`](https://docs.astral.sh/uv/) for the Python toolchain
* Node 22 + `pnpm` 9 for the SPA + e2e suite

```bash
git clone https://github.com/yowanda/Reckora
cd Reckora

# Backend
(cd backend && uv venv --python 3.11 && uv pip install -e ".[dev,scale,gql,cli]")

# Frontend + Playwright browsers
(cd frontend && pnpm install --frozen-lockfile)
(cd frontend && pnpm exec playwright install --with-deps chromium)

# Pre-commit hooks (trailing whitespace, ruff, ruff-format, eslint)
pre-commit install
```

Run the backend dev server:

```bash
cd backend
RECKORA_ADMIN_USERNAME=adminganteng \
RECKORA_ADMIN_PASSWORD=adminadmin \
uv run uvicorn app.main:app --reload   # http://localhost:8000
```

Run the frontend in another terminal:

```bash
cd frontend && pnpm dev   # http://localhost:5173
```

Vite proxies `/api` to `http://127.0.0.1:8000`.

## The Phase-N runbook lifecycle

Reckora ships in numbered phases, each broken into sub-phases (e.g. Phase 10
sub-phase 10a, 10b, 10c, …). Every sub-phase follows the same lifecycle:

1. **Scope.** A sub-phase is added to `ROADMAP.md` as a `[ ]` line item with
   a one-paragraph spec describing the new module(s), admin knobs, tests,
   and the gap in `comparison-maigret.md` it closes.
2. **Branch.** Implementation lands on a topic branch named
   `devin/<timestamp>-<slug>` (the timestamp is `date +%s` so branches sort
   chronologically when listed).
3. **Local baseline must stay green** before the PR opens:
   * `cd backend && uv run --extra dev ruff check . && uv run --extra dev ruff format --check . && uv run --extra dev mypy app && uv run --extra dev --extra scale --extra gql pytest -q`
   * `cd frontend && pnpm lint && pnpm tsc --noEmit && pnpm build`
   * `cd frontend && pnpm exec playwright test` (must stay 8 / 8)
4. **PR.** Open a pull request against `main` titled
   `feat(<phase>): <one-liner>` and let the three CI jobs (`backend`,
   `frontend`, `e2e (Playwright)`) finish before requesting review.
5. **Merge.** Once CI is green, the PR is merged into `main` (squash or
   plain merge — both are fine).
6. **ROADMAP.md update.** The same PR (or an immediate follow-up `docs()`
   PR) flips the sub-phase from `[ ]` to `[x]`, bumps the
   `Last batch shipped`, `Last update`, and `Test baseline` lines in the
   `Live status` table, and (when applicable) marks `[D]` once the HF Space
   mirror has caught up.
7. **Live verify.** When the HF Space mirror is redeployed, the operator
   click-tests the smoke flows and ticks `[V]` for sub-phases whose new
   surface is reachable in production.

### Default-off invariant

Every Phase 10 capability ships with a default-off admin knob (or a
default-empty extra) so the live HF Space deployment stays byte-for-byte
identical until an operator opts in. New PRs are expected to honour this:

* New runtime behaviour goes behind an `enable_<feature>` knob registered
  in `admin_settings.KNOBS` with `default=0`.
* New optional dependencies go behind a `[<extra>]` block in
  `backend/pyproject.toml` and are guarded with a runtime import-error
  message when the extra is missing.
* New CLI / API surface that depends on third-party state (proxies, AI
  providers, tor daemon) defaults to off and surfaces a clear `503` /
  `412` when the operator hasn't configured the prerequisites.

If you're unsure whether your change qualifies, default to making it
opt-in and ask in the PR description.

## Commit-message convention

We follow a small subset of [Conventional Commits][cc] so the
`CHANGELOG.md` generator can group entries by phase + type:

```
<type>(<scope>): <summary>

[optional body]
[optional footer]
```

`<type>` is one of:

* `feat` — new user-facing capability (most Phase-N work).
* `fix` — bug fix.
* `chore` — dependency / tooling / repo plumbing.
* `docs` — documentation-only change (ROADMAP, README, CHANGELOG, …).
* `refactor` — neutral internal restructuring with no behaviour change.
* `test` — test-only change.

`<scope>` is the phase or area, e.g. `feat(10f): reckora-cli console-script`.
The summary is imperative and ≤ 72 chars. The optional body explains the
*why* and lists the new admin knobs / tests; the optional footer references
the ROADMAP item or related issue. See the existing
[`git log`](https://github.com/yowanda/Reckora/commits/main) for examples.

[cc]: https://www.conventionalcommits.org/en/v1.0.0/

## Pull request review flow

1. **Open the PR against `main`.** Branches off `main`; the PR title
   matches the commit-message convention above.
2. **CI must finish all three jobs.** Backend (`ruff check` + `ruff format
   --check` + `mypy app` + `pytest -q`), frontend (`pnpm lint` + `pnpm tsc
   --noEmit` + `pnpm build`), and `e2e (Playwright)`.
3. **PR description.** Use the body to describe scope, new knobs / extras,
   test counts (delta vs the prior baseline), and any default-off
   trade-offs. Link the closing ROADMAP item.
4. **Reviewer pass.** A maintainer reviews the diff for default-off
   compliance, mypy / ruff cleanliness, and roadmap alignment. Reviewers
   should ask for the missing pieces rather than fixing them in-tree.
5. **Merge.** Once approved + green, the maintainer merges. We don't
   force-push to `main`; rebase + squash is fine for cleanup.
6. **Post-merge.** If the PR didn't already update `ROADMAP.md`, open a
   small `docs(<phase>)` follow-up that flips the `[ ]` to `[x]` and bumps
   the `Live status` table. This keeps `main` and the roadmap in sync.

### Scope-of-PR guidance

* One sub-phase per PR. If a PR grows past ~1 000 added lines, split it.
* New tests live next to the code they cover (`backend/tests/test_<module>.py`,
  `frontend/e2e/<flow>.spec.ts`).
* No code changes go into a `docs(...)` PR. Mixed-scope PRs slow review.
* No force-pushes to `main`. Force-pushes to your own feature branch are
  fine (`git push --force-with-lease`).

## Tests, lint, and CI

The full local baseline:

```bash
# Backend
cd backend
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy app
uv run --extra dev --extra scale --extra gql pytest -q

# Frontend
cd ../frontend
pnpm lint
pnpm tsc --noEmit
pnpm build

# End-to-end
pnpm exec playwright test
```

The same three jobs run in CI on every push and pull request
(`.github/workflows/ci.yml`). `e2e` uploads a `playwright-report` artifact
on failure for offline triage.

If you add a runtime knob, write a knob round-trip test (PATCH
`/api/admin/settings`, then assert the effective value via
`admin_settings.effective_int(...)`). If you add a new SSE event field,
update the relevant `frontend/src/types.ts` literal so the SPA still
type-checks.

## Issues and feature requests

* **Bugs.** Open a GitHub issue with: Reckora commit SHA, repro steps, the
  observed `result` SSE payload (or stack trace), and any non-default
  admin-knob values. If the bug only reproduces with a specific
  identifier, please use a public-figure / synthetic identifier so we can
  reproduce without exposing private data.
* **Feature requests.** Open an issue prefixed `[feature]` describing the
  user need + the slice of `comparison-maigret.md` it closes (if any).
  We'll reply on the issue with whether it makes sense as a Phase-N
  sub-phase, a follow-up phase, or out of scope.
* **Catalog tweaks.** Site catalog updates land via the quarterly auto-
  refresh PR (`chore(catalog): quarterly refresh from soxoj/maigret
  <tag>`). For one-off corrections (a site moved, an error string
  changed), open a small `chore(catalog)` PR with the new entry.

## Security

Security-relevant findings should NOT be filed as public issues. See
[`SECURITY.md`](./SECURITY.md) for the responsible-disclosure address and
the 90-day fix window.

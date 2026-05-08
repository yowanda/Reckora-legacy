# Security Policy

Reckora is an OSINT username-footprint scanner. Because the live HF Space
mirror serves authenticated members and stores admin-knob state in SQLite,
we treat reports of authentication, authorization, SSRF, and rate-limit
bypass bugs as security issues — not feature requests.

## Reporting a vulnerability

**Do not** open a public GitHub issue. Instead, email:

> **security@duriancompany.us**

Please include:

* The Reckora commit SHA (and HF Space build, if applicable).
* The class of issue (auth bypass, SSRF, RCE, info disclosure, …).
* A minimal, ideally self-contained, reproduction (HTTP request + expected
  vs. actual response is ideal). Test against your own admin account.
* Any logs, payloads, or stack traces that help triage.
* A name / handle / contact you'd like credited in the fix's CHANGELOG entry,
  if any.

We acknowledge new reports within **3 business days** and will follow up
with a CVSS-style severity assessment plus a tentative remediation
schedule.

If you do not receive an acknowledgement within 5 business days, the
report may have been spam-filtered — please re-send from a different
address or ping a maintainer directly through the GitHub repo with a
non-sensitive note ("I've sent you a security report; please check
spam") and we will respond.

## Disclosure timeline (90-day fix window)

We commit to the following timeline from the day a valid report is
received:

| Day | Action |
| --- | --- |
| 0 | Report received, automated acknowledgement. |
| ≤ 3 business days | Human triage + severity assessment. |
| ≤ 14 days | Confirmation of reproduction + remediation owner assigned. |
| ≤ 60 days | Patch landed on `main` (behind a default-off knob if the fix is risky to enable in flight) and deployed to the HF Space mirror. |
| ≤ 90 days | Coordinated public disclosure: the CHANGELOG entry, an advisory in the GitHub Security Advisory tab, and (if the issue affected upstream) a heads-up to the relevant upstream project. |

If the issue is straightforward we ship the fix faster — but **90 days is
the hard ceiling**. After 90 days you are free to disclose publicly even
if a fix has not landed; we will still credit you if you ask.

If a fix needs more than 90 days due to upstream constraints (e.g. a
third-party site blocking us out of band), we will tell you well before
day 60 and propose an extension. Extensions are only requested when
absolutely necessary.

## In-scope

* The `reckora-backend` FastAPI app under `backend/app/`.
* The `frontend/src/` SPA bundle and its single-origin
  `backend/app/static/` mount.
* The `reckora` CLI under `backend/app/cli/`.
* Admin knobs registered in `backend/app/admin_settings.py`.
* The HF Space deploy: <https://yesmyyt-cacadoxx.hf.space>.
* The Celery `[scale]` worker fleet and Redis rate-limit / queue
  surfaces (Phase 8a, 9b).
* The GraphQL gateway (Phase 8c).

## Out-of-scope

* Vulnerabilities in third-party sites we probe. Forward those to the
  upstream operator. Reckora's job is to *probe* sites; we do not own
  their security posture.
* Cosmetic UI bugs, broken links, or typos. Open a normal GitHub issue.
* Self-XSS or social-engineering attacks against the admin account that
  require the admin to paste hostile JavaScript into their own browser.
* Issues that require a malicious admin to exploit (the admin is already
  trusted; that's the whole point of the admin role).
* Issues that require physical access to the HF Space build host.
* Findings derived from automated scanners with no proof-of-concept.

## Safe-harbour

We will not pursue or support any legal action related to security
research conducted in good faith against your own Reckora install,
provided you:

* Avoid privacy violations, destruction of data, and interruption of
  service to other operators.
* Do not exfiltrate any data beyond the minimum needed to demonstrate
  the issue.
* Give us reasonable time to fix the issue before any public
  disclosure.
* Do not access an admin account that isn't yours.

Thanks for helping keep Reckora and its members safe.

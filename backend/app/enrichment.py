"""Phase 7b — WHOIS / HIBP-style breach enrichment for ``identity_type=email``.

Goal
====
When the operator runs an email scan and at least one CLAIMED hit
matches, the SPA pulls a side-panel summary that goes *beyond*
maigret's per-site checks: who registered the email's domain (RDAP),
and whether the address has appeared in any public breach lists
(Have-I-Been-Pwned API). Both are read-only OSINT lookups —
no scraping, no credential discovery, no private data — but they're
strong intel signals on top of the catalog hit-list.

This module is intentionally small and self-contained:

- ``fetch_whois``: one bounded GET against an RDAP endpoint
  (default ``https://rdap.org/domain/{domain}``). RDAP is the
  IETF's structured replacement for legacy WHOIS, returns JSON,
  and crucially needs no API key. Network failures degrade
  silently to ``None``.
- ``fetch_breaches``: one bounded GET against HIBP's
  ``api/v3/breachedaccount/{email}`` endpoint, with the admin-
  configured API key in the ``hibp-api-key`` header. The 404
  "not breached" status maps to ``[]`` (the sweet spot — a clean
  result, not an error). Anything else degrades silently to
  ``None`` so partial enrichment still renders.
- ``enrich_email``: runs both concurrently via ``asyncio.gather``
  so a slow RDAP server doesn't block the breach lookup or vice
  versa. Returns a serialisable :class:`EnrichmentResult` dict.
- :class:`RateLimiter`: per-actor sliding-window limiter so a
  member can't spam the upstream APIs by hammering the SPA's
  enrichment button.

The HIBP API key is treated as a *secret*: it lives in
``Settings.enrichment_hibp_api_key`` (env-only), is **never**
returned by the public ``/api/settings`` route, and only the
``/api/enrich/email`` handler reads it. RDAP needs no key at all.

Default off — ``enable_email_enrichment=0``. Operators flip it
on via the admin settings panel.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("reckora.enrichment")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window per-actor request limiter.

    The bucket records the timestamps of the most recent requests
    per ``actor`` key. ``allow`` evicts everything outside the
    window before counting; if the survivors stay under the limit
    we append the new timestamp and return ``True``. The window
    is fixed at 60 seconds so the per-minute limit reads
    intuitively from the admin knob.

    The limiter is intentionally process-local — one uvicorn
    instance, one bucket. A multi-worker deployment would either
    park the bucket in Redis (out of scope for Phase 7b) or
    accept slightly looser limits per replica.
    """

    def __init__(self, *, window_seconds: float = 60.0) -> None:
        self.window = float(window_seconds)
        self._hits: dict[str, deque[float]] = {}

    def allow(self, actor: str, *, limit: int, now: float | None = None) -> bool:
        """Return True if ``actor`` may make one more request.

        Records the timestamp on success. ``limit <= 0`` is treated
        as "no requests allowed" (the floor — the admin would never
        actually configure this, but we shouldn't crash if they
        did).
        """
        if limit <= 0:
            return False
        ts = float(now) if now is not None else time.monotonic()
        bucket = self._hits.setdefault(actor, deque())
        cutoff = ts - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(ts)
        return True

    def reset(self, actor: str | None = None) -> None:
        """Drop one or all per-actor buckets (test helper)."""
        if actor is None:
            self._hits.clear()
        else:
            self._hits.pop(actor, None)


# Process-wide limiter shared by the FastAPI handler. Tests can
# call ``GLOBAL_LIMITER.reset()`` to start clean.
GLOBAL_LIMITER = RateLimiter()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhoisRecord:
    """Distilled subset of an RDAP ``domain`` response."""

    domain: str
    registrar: str | None
    created: str | None
    expires: str | None
    updated: str | None
    nameservers: tuple[str, ...]
    status: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "registrar": self.registrar,
            "created": self.created,
            "expires": self.expires,
            "updated": self.updated,
            "nameservers": list(self.nameservers),
            "status": list(self.status),
        }


@dataclass(frozen=True)
class Breach:
    """Distilled subset of an HIBP ``Breach`` object."""

    name: str
    title: str
    domain: str | None
    breach_date: str | None
    added_date: str | None
    pwn_count: int | None
    data_classes: tuple[str, ...]
    is_verified: bool
    is_fabricated: bool
    is_sensitive: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "domain": self.domain,
            "breach_date": self.breach_date,
            "added_date": self.added_date,
            "pwn_count": self.pwn_count,
            "data_classes": list(self.data_classes),
            "is_verified": self.is_verified,
            "is_fabricated": self.is_fabricated,
            "is_sensitive": self.is_sensitive,
        }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _rdap_event_date(events: list[dict[str, Any]] | None, action: str) -> str | None:
    """Pull a single ``eventDate`` (ISO-8601) by ``eventAction`` from
    an RDAP ``events`` array. Returns ``None`` on miss."""
    if not events:
        return None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("eventAction") == action:
            date = ev.get("eventDate")
            if isinstance(date, str):
                return date
    return None


def _rdap_registrar(entities: list[dict[str, Any]] | None) -> str | None:
    """Walk an RDAP ``entities`` tree and return the first registrar
    name we find (the entity tagged with role ``registrar``)."""
    if not entities:
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if isinstance(roles, list) and "registrar" in roles:
            vcard = ent.get("vcardArray")
            # vcardArray is ["vcard", [["fn", {}, "text", "<name>"], ...]]
            if isinstance(vcard, list) and len(vcard) >= 2 and isinstance(vcard[1], list):
                for entry in vcard[1]:
                    if (
                        isinstance(entry, list)
                        and len(entry) >= 4
                        and entry[0] == "fn"
                        and isinstance(entry[3], str)
                    ):
                        return entry[3]
            # Fallback: handle / publicId on the entity itself.
            handle = ent.get("handle")
            if isinstance(handle, str):
                return handle
    return None


def _rdap_nameservers(nameservers: list[dict[str, Any]] | None) -> tuple[str, ...]:
    if not nameservers:
        return ()
    out: list[str] = []
    for ns in nameservers:
        if not isinstance(ns, dict):
            continue
        name = ns.get("ldhName") or ns.get("unicodeName")
        if isinstance(name, str):
            out.append(name)
    return tuple(out)


def parse_rdap_domain(payload: Mapping[str, Any]) -> WhoisRecord:
    """Distil an RDAP domain response into a :class:`WhoisRecord`.

    Tolerant: missing fields become ``None`` / empty tuples instead
    of raising — RDAP servers are notoriously inconsistent in which
    blocks they populate.
    """
    domain = ""
    raw_domain = payload.get("ldhName") or payload.get("unicodeName")
    if isinstance(raw_domain, str):
        domain = raw_domain
    events_raw = payload.get("events")
    events = events_raw if isinstance(events_raw, list) else None
    entities_raw = payload.get("entities")
    entities = entities_raw if isinstance(entities_raw, list) else None
    nameservers_raw = payload.get("nameservers")
    nameservers = nameservers_raw if isinstance(nameservers_raw, list) else None
    status_raw = payload.get("status")
    status: tuple[str, ...] = ()
    if isinstance(status_raw, list):
        status = tuple(s for s in status_raw if isinstance(s, str))
    return WhoisRecord(
        domain=domain,
        registrar=_rdap_registrar(entities),
        created=_rdap_event_date(events, "registration"),
        expires=_rdap_event_date(events, "expiration"),
        updated=_rdap_event_date(events, "last changed"),
        nameservers=_rdap_nameservers(nameservers),
        status=status,
    )


def parse_hibp_breaches(payload: list[Mapping[str, Any]] | Any) -> list[Breach]:
    """Distil HIBP's ``BreachedAccount`` array into :class:`Breach` objects.

    HIBP returns an array of breach objects; non-list responses are
    treated as "nothing parseable" → empty list.
    """
    if not isinstance(payload, list):
        return []
    out: list[Breach] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        name = raw.get("Name") or raw.get("name")
        title = raw.get("Title") or raw.get("title") or name
        if not isinstance(name, str) or not isinstance(title, str):
            continue
        dc_raw = raw.get("DataClasses") or raw.get("dataClasses") or []
        if not isinstance(dc_raw, list):
            dc_raw = []
        out.append(
            Breach(
                name=name,
                title=title,
                domain=raw.get("Domain") or raw.get("domain") or None,
                breach_date=raw.get("BreachDate") or raw.get("breachDate") or None,
                added_date=raw.get("AddedDate") or raw.get("addedDate") or None,
                pwn_count=raw.get("PwnCount") or raw.get("pwnCount") or None,
                data_classes=tuple(c for c in dc_raw if isinstance(c, str)),
                is_verified=bool(raw.get("IsVerified") or raw.get("isVerified")),
                is_fabricated=bool(raw.get("IsFabricated") or raw.get("isFabricated")),
                is_sensitive=bool(raw.get("IsSensitive") or raw.get("isSensitive")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# HTTP fetchers
# ---------------------------------------------------------------------------


def email_domain(email: str) -> str | None:
    """Return the lowercased domain after the ``@``, or ``None`` on
    a malformed input. ``is_valid_identifier`` already validates
    shape upstream; this is a defensive split."""
    at = email.rfind("@")
    if at <= 0 or at >= len(email) - 1:
        return None
    return email[at + 1 :].strip().lower()


async def fetch_whois(
    client: httpx.AsyncClient,
    domain: str,
    *,
    url_template: str,
    timeout: float,
) -> WhoisRecord | None:
    """One bounded RDAP lookup. Returns a parsed record on 200, else None.

    Failure modes (return ``None``):
    - non-2xx HTTP status
    - non-JSON body
    - transport / connect / timeout errors

    ``url_template`` must contain ``{domain}`` as the substitution
    placeholder; everything else is interpolated as-is.
    """
    if "{domain}" not in url_template:
        return None
    url = url_template.format(domain=domain)
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        logger.debug("WHOIS fetch failed for %s", domain, exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return parse_rdap_domain(payload)


async def fetch_breaches(
    client: httpx.AsyncClient,
    email: str,
    *,
    url_template: str,
    api_key: str,
    timeout: float,
) -> list[Breach] | None:
    """One bounded HIBP lookup. Returns a list (possibly empty) on
    success, ``None`` on failure / missing key.

    HIBP semantics:
    - HTTP 200 + JSON array → parsed list
    - HTTP 404 → email is **not** in any breach (clean result; ``[]``)
    - HTTP 401 / 403 → bad API key → ``None``
    - HTTP 429 → upstream rate limit → ``None``
    - everything else → ``None``
    """
    if not api_key:
        return None
    if "{email}" not in url_template:
        return None
    url = url_template.format(email=email)
    headers = {
        "hibp-api-key": api_key,
        "User-Agent": "Reckora-OSINT-Scanner/7b",
    }
    try:
        resp = await client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        logger.debug("HIBP fetch failed for %s", email, exc_info=True)
        return None
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    return parse_hibp_breaches(payload)


@dataclass(frozen=True)
class EnrichmentResult:
    email: str
    domain: str | None
    whois: WhoisRecord | None
    breaches: list[Breach] | None
    breaches_disabled: bool  # True when no HIBP key is configured

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "domain": self.domain,
            "whois": self.whois.to_dict() if self.whois is not None else None,
            "breaches": (
                [b.to_dict() for b in self.breaches] if self.breaches is not None else None
            ),
            "breaches_disabled": self.breaches_disabled,
        }


async def enrich_email(
    client: httpx.AsyncClient,
    email: str,
    *,
    whois_url_template: str,
    hibp_url_template: str,
    hibp_api_key: str,
    timeout: float,
) -> EnrichmentResult:
    """Run WHOIS + HIBP lookups concurrently for ``email``.

    Either / both lookups may return ``None`` independently. The
    handler decides how to render that — typically "WHOIS upstream
    didn't respond" + "no breaches" still renders a useful panel.
    """
    domain = email_domain(email)
    whois_coro: Any
    if domain is None:
        whois_coro = _none()
    else:
        whois_coro = fetch_whois(client, domain, url_template=whois_url_template, timeout=timeout)
    breaches_coro: Any
    breaches_disabled = not hibp_api_key
    if breaches_disabled:
        breaches_coro = _none()
    else:
        breaches_coro = fetch_breaches(
            client,
            email,
            url_template=hibp_url_template,
            api_key=hibp_api_key,
            timeout=timeout,
        )
    import asyncio

    whois_record, breaches_list = await asyncio.gather(whois_coro, breaches_coro)
    return EnrichmentResult(
        email=email,
        domain=domain,
        whois=whois_record,
        breaches=breaches_list,
        breaches_disabled=breaches_disabled,
    )


async def _none() -> None:
    return None

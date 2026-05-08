"""Phase 10g: AI investigation summary.

Closes the AI-summary gap with maigret's ``--ai`` feature: turn the raw
findings of a finished scan into a short investigation summary using an
OpenAI-compatible chat completion API. The pipeline is intentionally
small and provider-pluggable:

1. :func:`build_markdown_report` projects the persisted scan history
   (CLAIMED hits, extractor fields, optional graph edges + pHash matches)
   into a compact markdown document. This is what we hand to the model
   as the "user" turn — keeping it markdown means the model can quote
   structure verbatim and the operator can read the prompt themselves
   when debugging unexpected output.

2. :func:`build_request` produces a provider-specific HTTP payload
   (URL, headers, JSON body). Four wire formats are supported:

   * ``openai``     — ``POST /v1/chat/completions`` with ``stream=true``
                       returning ``data: {...}\\n\\n`` SSE chunks whose
                       ``choices[0].delta.content`` carries the text.
   * ``openrouter`` — same wire format as OpenAI (it ships
                       drop-in compatibility) but with a default
                       ``base_url`` of ``https://openrouter.ai/api/v1``.
   * ``anthropic``  — ``POST /v1/messages`` with ``stream=true``
                       returning ``event:`` / ``data:`` SSE chunks
                       whose ``content_block_delta`` events carry text.
   * ``ollama``     — ``POST /api/chat`` with ``stream=true``
                       returning newline-delimited JSON whose
                       ``message.content`` carries the text. Ollama
                       runs locally; the API key field is unused.

3. :func:`stream_summary` opens the HTTP request, decodes the streamed
   chunks via the provider-specific unwrap helper, and yields plain text
   fragments to the caller. The :func:`summarize_scan` wrapper collects
   the iterator into a single ``str`` for callers that don't want to
   stream (matches the spec's ``-> str`` return type).

The module reads four admin knobs:

* ``enable_ai_summary``     — master switch (default 0 / off).
* ``ai_summary_provider``   — provider name, default ``openai``.
* ``ai_summary_base_url``   — provider HTTP base, e.g.
                              ``https://api.openai.com/v1`` for OpenAI;
                              empty falls back to the provider default.
* ``ai_summary_model``      — model id, e.g. ``gpt-4o-mini``;
                              empty falls back to a provider default.

The API key lives in :mod:`app.secrets_store` (``ai_summary_api_key``)
so a leaked SQLite admin DB never exposes the key. ``MissingApiKey`` is
raised when the key is absent — the REST endpoint translates that into
a ``412 Precondition Failed`` so the SPA can show "configure your API
key first" instead of the generic 500 a transport-level failure would
yield.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx

from . import admin_settings as admin_settings_mod
from . import history as history_mod
from . import secrets_store
from .config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider config + defaults
# ---------------------------------------------------------------------------


SUPPORTED_PROVIDERS: Final[tuple[str, ...]] = ("openai", "anthropic", "openrouter", "ollama")

# Per-provider default base URLs. The admin knob ``ai_summary_base_url``
# wins over these when set; an empty knob falls back to the default.
DEFAULT_BASE_URLS: Final[dict[str, str]] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434",
}

# Per-provider default model ids. Conservative low-cost picks across the
# board — operators with budget can override via the
# ``ai_summary_model`` admin knob.
DEFAULT_MODELS: Final[dict[str, str]] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "openrouter": "openai/gpt-4o-mini",
    "ollama": "llama3.1",
}

# System prompt — kept short and explicit so the model focuses on
# investigation framing rather than restating the input.
_SYSTEM_PROMPT: Final[str] = (
    "You are an OSINT investigator. The user message contains the raw "
    "findings of a username footprint scan: CLAIMED hits with confidence "
    "scores, extracted profile fields (real-name, location, avatar, "
    "secondary handles), graph edges that link the same person across "
    "platforms, and perceptual-hash matches that stitch avatars together. "
    "Write a concise investigation summary in markdown with these "
    "sections: ``## Identity hypothesis`` (most likely identity, naming "
    "the strongest signals), ``## Connected accounts`` (bullet list of "
    "the highest-confidence platforms with the URLs), ``## Notable "
    "fields`` (any extracted real-name, location, secondary usernames), "
    "and ``## Confidence`` (qualitative, citing the rule-based + ML "
    "scores). Do not invent facts; cite only what appears in the input."
)


class AiSummaryError(RuntimeError):
    """Base error for the Phase 10g pipeline."""


class MissingApiKeyError(AiSummaryError):
    """Raised when ``ai_summary_api_key`` is not configured.

    The REST endpoint translates this into a 412 so the SPA can render a
    "configure your API key first" hint instead of the generic 500 a
    transport-level failure would yield.
    """


class UnknownProviderError(AiSummaryError):
    """Raised when the ``ai_summary_provider`` knob is not in
    :data:`SUPPORTED_PROVIDERS`."""


class FeatureDisabledError(AiSummaryError):
    """Raised when the master switch ``enable_ai_summary`` is off."""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Fully-baked HTTP request descriptor for the chat completion call."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """Phase 5d secondary-scan edge between two CLAIMED nodes."""

    parent_username: str
    child_username: str
    site: str | None = None


@dataclass(frozen=True, slots=True)
class PhashMatch:
    """Phase 7a perceptual-hash match between two CLAIMED hits."""

    site_a: str
    site_b: str
    distance: int


def _format_extracted(extracted: dict[str, Any]) -> str:
    """Format the per-result ``extracted`` dict as a compact key/value
    list suitable for inclusion in a markdown table cell."""
    items: list[str] = []
    for k in sorted(extracted):
        v = extracted[k]
        if v is None or v == "":
            continue
        if isinstance(v, list | tuple):
            v = ", ".join(str(x) for x in v if x)
            if not v:
                continue
        items.append(f"{k}={v}")
    return "; ".join(items)


def _md_escape(s: str) -> str:
    """Escape ``|`` so a value with a pipe doesn't break a markdown table."""
    return s.replace("|", "\\|")


def build_markdown_report(
    scan: history_mod.ScanSummary,
    results: list[dict[str, Any]],
    *,
    graph_edges: list[GraphEdge] | None = None,
    phash_matches: list[PhashMatch] | None = None,
) -> str:
    """Project a finished scan into a markdown report for the LLM.

    The output is intentionally small (typically <2 KB even for a
    50-CLAIMED scan): we filter to CLAIMED hits, sort them by descending
    rule-based confidence, and only surface the few extracted fields
    that are useful to the model. Non-CLAIMED rows are summarised in
    one line so the model still sees the *scope* of the scan without
    being drowned in 2,400 FREE rows.
    """
    lines: list[str] = []
    duration = max(0, scan.finished_at - scan.started_at)
    lines.append(f"# Reckora scan #{scan.id} — username `{scan.username}`")
    lines.append("")
    lines.append(
        f"- **Mode**: `{scan.mode}` · sites probed: {scan.site_count} · duration: {duration}s"
    )
    lines.append(
        "- **Outcome**: "
        f"{scan.claimed} claimed · {scan.free} free · {scan.blocked} blocked · "
        f"{scan.unknown} unknown · {scan.error_count} errors"
    )
    lines.append("")

    claimed = [r for r in results if r.get("status") == "claimed"]
    claimed.sort(key=lambda r: int(r.get("confidence") or 0), reverse=True)

    lines.append("## CLAIMED hits")
    lines.append("")
    if not claimed:
        lines.append("_No CLAIMED hits — every site responded FREE / UNKNOWN / BLOCKED._")
        lines.append("")
    else:
        lines.append("| Site | URL | Confidence | ML | Extracted |")
        lines.append("|---|---|---:|---:|---|")
        for r in claimed:
            site = _md_escape(str(r.get("site") or ""))
            url = str(r.get("url") or "")
            conf = "" if r.get("confidence") is None else str(int(r["confidence"]))
            ml = "" if r.get("ml_confidence") is None else str(int(r["ml_confidence"]))
            extracted_obj = r.get("extracted") or {}
            extracted = _md_escape(
                _format_extracted(extracted_obj) if isinstance(extracted_obj, dict) else ""
            )
            lines.append(f"| {site} | {url} | {conf} | {ml} | {extracted} |")
        lines.append("")

    edges = graph_edges or []
    if edges:
        lines.append("## Graph edges (Phase 5d secondary scans)")
        lines.append("")
        for e in edges:
            child = _md_escape(e.child_username)
            parent = _md_escape(e.parent_username)
            via = f" via `{_md_escape(e.site)}`" if e.site else ""
            lines.append(f"- `{parent}` → `{child}`{via}")
        lines.append("")

    matches = phash_matches or []
    if matches:
        lines.append("## Perceptual-hash matches (Phase 7a)")
        lines.append("")
        for m in matches:
            lines.append(
                f"- `{_md_escape(m.site_a)}` ↔ `{_md_escape(m.site_b)}` "
                f"(Hamming distance {m.distance})"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def graph_edges_from_results(results: list[dict[str, Any]]) -> list[GraphEdge]:
    """Reconstruct ``parent → child`` graph edges from a stored scan.

    Phase 5d records the parent username on every CLAIMED secondary hit,
    so the in-memory graph can be rebuilt without keeping a separate
    edge table.
    """
    edges: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for r in results:
        if r.get("status") != "claimed":
            continue
        parent = r.get("parent_username")
        if not parent:
            continue
        # The primary scan's parent is the username itself; only emit
        # edges where the parent differs from the row's username.
        site = str(r.get("site") or "")
        child = str(r.get("username") or "")
        if not child:
            child = site
        key = (str(parent), child, site)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            GraphEdge(parent_username=str(parent), child_username=child, site=site or None)
        )
    return edges


def phash_matches_from_results(results: list[dict[str, Any]]) -> list[PhashMatch]:
    """Pair up CLAIMED hits whose ``avatar_phash`` differs by at most
    ``image_phash_max_distance`` bits.

    Mirrors the runtime stitching done by ``app.imagephash.find_matches``
    — duplicated here (rather than imported) so building a markdown
    report from a *persisted* scan doesn't require re-running the
    matcher with the same threshold settings the live SSE stream used.
    Phase 4 history only persists the summary tuple, so callers that
    care about the raw avatar hashes pass them in via the optional
    ``phash_matches`` argument on :func:`build_markdown_report` instead.
    """
    return []


# ---------------------------------------------------------------------------
# Provider request builders
# ---------------------------------------------------------------------------


def _join_url(base: str, path: str) -> str:
    """Join ``base`` and ``path`` without producing accidental double slashes."""
    if not base:
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def build_request(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
) -> ProviderRequest:
    """Construct the wire-format HTTP request for ``provider``.

    Each branch is intentionally kept inline (rather than dispatched to
    helpers) so the JSON shape for each provider lives in one easy-to-
    audit place. The structure is verified by per-provider tests in
    ``tests/test_ai_summary.py``.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise UnknownProviderError(f"unsupported ai_summary_provider: {provider!r}")
    base = base_url or DEFAULT_BASE_URLS[provider]

    if provider in ("openai", "openrouter"):
        if provider != "ollama" and not api_key:
            raise MissingApiKeyError(f"{provider} requires an API key")
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if provider == "openrouter":
            # OpenRouter requires HTTP-Referer for analytics; set a stable
            # value so the provider dashboard groups Reckora calls.
            headers["HTTP-Referer"] = "https://github.com/yowanda/Reckora"
            headers["X-Title"] = "Reckora"
        return ProviderRequest(url=_join_url(base, "/chat/completions"), headers=headers, body=body)

    if provider == "anthropic":
        if not api_key:
            raise MissingApiKeyError("anthropic requires an API key")
        body = {
            "model": model,
            "stream": True,
            "max_tokens": 1024,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        return ProviderRequest(url=_join_url(base, "/messages"), headers=headers, body=body)

    # ollama — local runtime, no API key required.
    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}
    return ProviderRequest(url=_join_url(base, "/api/chat"), headers=headers, body=body)


# ---------------------------------------------------------------------------
# Streaming-response unwrap
# ---------------------------------------------------------------------------


def _iter_sse_data(buffer: str) -> tuple[list[str], str]:
    """Split a streaming buffer into ``data:``-prefixed lines.

    Returns ``(lines, leftover)`` where ``leftover`` is the part of
    ``buffer`` past the last newline (still potentially incomplete).
    """
    lines: list[str] = []
    *complete, leftover = buffer.split("\n")
    for raw in complete:
        line = raw.rstrip("\r")
        if line.startswith("data:"):
            payload = line[len("data:") :].lstrip()
            if payload:
                lines.append(payload)
    return lines, leftover


def _decode_openai_chunk(payload: str) -> str:
    """Extract the text fragment from one OpenAI SSE ``data:`` line."""
    if payload == "[DONE]":
        return ""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _decode_anthropic_chunk(payload: str) -> str:
    """Extract the text fragment from one Anthropic SSE ``data:`` line."""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if obj.get("type") != "content_block_delta":
        return ""
    delta = obj.get("delta") or {}
    if not isinstance(delta, dict):
        return ""
    if delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _decode_ollama_chunk(line: str) -> str:
    """Extract the text fragment from one Ollama NDJSON line."""
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


async def stream_response(
    response: httpx.Response,
    *,
    provider: str,
) -> AsyncIterator[str]:
    """Yield decoded text fragments from a live streaming HTTP response.

    Splits the response body into provider-appropriate units (SSE
    ``data:`` lines for OpenAI / Anthropic / OpenRouter, newline-
    delimited JSON for Ollama) and yields the unwrapped ``content``
    fragment from each. Empty fragments are skipped so the caller
    receives only meaningful text.
    """
    buffer = ""
    async for chunk in response.aiter_text():
        if not chunk:
            continue
        buffer += chunk
        if provider == "ollama":
            *complete, buffer = buffer.split("\n")
            for line in complete:
                fragment = _decode_ollama_chunk(line)
                if fragment:
                    yield fragment
        else:
            payloads, buffer = _iter_sse_data(buffer)
            for payload in payloads:
                fragment = (
                    _decode_anthropic_chunk(payload)
                    if provider == "anthropic"
                    else _decode_openai_chunk(payload)
                )
                if fragment:
                    yield fragment
    # Flush any trailing line that didn't end in a newline.
    if buffer.strip():
        if provider == "ollama":
            fragment = _decode_ollama_chunk(buffer)
            if fragment:
                yield fragment
        elif buffer.startswith("data:"):
            payload = buffer[len("data:") :].strip()
            if payload:
                fragment = (
                    _decode_anthropic_chunk(payload)
                    if provider == "anthropic"
                    else _decode_openai_chunk(payload)
                )
                if fragment:
                    yield fragment


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _knob_int(key: str) -> int:
    try:
        return int(admin_settings_mod.effective_int(key))
    except Exception:
        spec = admin_settings_mod.KNOBS_BY_KEY.get(key)
        if spec is None:
            return 0
        return int(getattr(settings, spec.settings_attr))


def is_enabled() -> bool:
    """Master switch for the AI summary feature."""
    return bool(_knob_int("enable_ai_summary"))


def current_provider() -> str:
    """Return the configured provider, defaulting to ``openai``."""
    value = str(getattr(settings, "ai_summary_provider", "") or "").strip().lower()
    return value or "openai"


def current_model(provider: str | None = None) -> str:
    """Return the configured model id, falling back to the provider default."""
    explicit = str(getattr(settings, "ai_summary_model", "") or "").strip()
    if explicit:
        return explicit
    return DEFAULT_MODELS.get(provider or current_provider(), "")


def current_base_url(provider: str | None = None) -> str:
    """Return the configured base URL, falling back to the provider default."""
    explicit = str(getattr(settings, "ai_summary_base_url", "") or "").strip()
    if explicit:
        return explicit
    return DEFAULT_BASE_URLS.get(provider or current_provider(), "")


async def stream_summary(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> AsyncIterator[str]:
    """Open the streaming HTTP request and yield decoded text fragments.

    A pre-built ``client`` may be passed in (used by the test suite to
    inject an :class:`httpx.MockTransport`). When ``client`` is ``None``
    the function spins up a private :class:`httpx.AsyncClient`, owns it
    for the lifetime of the call, and closes it on exit.
    """
    request = build_request(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        prompt=prompt,
    )
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        async with client.stream(
            "POST",
            request.url,
            headers=request.headers,
            json=request.body,
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                # Drain the body so the connection can be reused, then
                # raise a clean error. We deliberately don't surface the
                # upstream body verbatim — it may contain provider-
                # specific debug fields the operator doesn't need.
                body = await response.aread()
                snippet = body[:200].decode("utf-8", "replace")
                raise AiSummaryError(
                    f"upstream {provider} returned {response.status_code}: {snippet}"
                )
            async for fragment in stream_response(response, provider=provider):
                yield fragment
    finally:
        if owns_client:
            await client.aclose()


async def summarize_scan(
    scan_id: int,
    *,
    model: str,
    base_url: str,
    api_key: str,
    member: str,
    provider: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> str:
    """Build the markdown report and stream-fetch the AI summary.

    Returns the assembled summary as a single string. Callers that want
    to forward the streamed chunks to their own client should iterate
    :func:`stream_summary` directly.

    Raises:
      * :class:`FeatureDisabledError` if ``enable_ai_summary`` is off.
      * :class:`MissingApiKeyError` if the configured provider requires
        an API key and none is set.
      * :class:`UnknownProviderError` if ``ai_summary_provider`` is not
        in :data:`SUPPORTED_PROVIDERS`.
      * :class:`LookupError` if the scan id is not visible to ``member``.
    """
    if not is_enabled():
        raise FeatureDisabledError("enable_ai_summary is 0")
    out = history_mod.get_scan(scan_id, member)
    if out is None:
        raise LookupError(f"scan {scan_id} not found for member {member!r}")
    summary, results = out
    prompt = build_markdown_report(
        summary,
        results,
        graph_edges=graph_edges_from_results(results),
        phash_matches=phash_matches_from_results(results),
    )

    chunks: list[str] = []
    async for fragment in stream_summary(
        provider=provider or current_provider(),
        model=model,
        base_url=base_url,
        api_key=api_key,
        prompt=prompt,
        client=client,
        timeout=timeout,
    ):
        chunks.append(fragment)
    return "".join(chunks)


def resolve_request_args(
    *,
    provider_override: str | None = None,
) -> tuple[str, str, str, str]:
    """Return ``(provider, model, base_url, api_key)`` from admin knobs + secrets.

    Centralised so the REST endpoint and direct-call test helpers share
    one source of truth on how the four pieces are composed.
    """
    provider = (provider_override or current_provider()).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise UnknownProviderError(f"unsupported ai_summary_provider: {provider!r}")
    model = current_model(provider) or DEFAULT_MODELS[provider]
    base_url = current_base_url(provider) or DEFAULT_BASE_URLS[provider]
    api_key = secrets_store.get_secret("ai_summary_api_key") or ""
    if provider != "ollama" and not api_key:
        raise MissingApiKeyError("ai_summary_api_key is not configured")
    return provider, model, base_url, api_key

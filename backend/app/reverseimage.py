"""Phase 7c — reverse-image lookup URL builder.

For every CLAIMED hit whose extractor pass produced an ``avatar_url``
we want a one-click deep-link into the public reverse-image search
landing page of Yandex / Google Lens / TinEye. None of these
endpoints require an API key — the SPA simply opens the upstream URL
in a new tab and lets the operator's browser hit it directly.

This module is intentionally small: a single :func:`build_lookup_urls`
helper that turns an avatar URL plus a set of provider templates into
a flat ``dict[str, str]`` of ``{"yandex": "https://...", ...}``. Pure
URL math, no network I/O — keeps the reverse-image feature trivially
unit-testable and makes admins' provider-template overrides cheap to
swap in without redeploying the backend.

The companion handler at ``GET /api/reverse-image`` exposes both the
opt-in knob (``enable_reverse_image``) and the resolved provider URLs
to the SPA so the same business rules govern both the backend and
the React component.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

# Public API surface — :func:`build_lookup_urls` is the only consumer
# beyond the FastAPI handler itself.
__all__ = [
    "PROVIDERS",
    "ReverseImageProvider",
    "build_lookup_urls",
    "is_lookupable_url",
]


@dataclass(frozen=True)
class ReverseImageProvider:
    """Static metadata for a single reverse-image search provider.

    The ``slug`` is the stable key the SPA uses to render its chip
    label; ``label`` is the human-readable button text. ``settings_attr``
    points at the ``Settings`` field that holds the URL template
    (``{url}`` placeholder for the encoded image URL).
    """

    slug: str
    label: str
    settings_attr: str


# Order matters — matches the SPA's chip layout.
PROVIDERS: tuple[ReverseImageProvider, ...] = (
    ReverseImageProvider(
        slug="yandex",
        label="Yandex",
        settings_attr="reverse_image_yandex_template",
    ),
    ReverseImageProvider(
        slug="google",
        label="Google Lens",
        settings_attr="reverse_image_google_template",
    ),
    ReverseImageProvider(
        slug="tineye",
        label="TinEye",
        settings_attr="reverse_image_tineye_template",
    ),
)


# Schemes we'll deep-link. Anything else (``data:``, ``javascript:``,
# private CDN protocols) is dropped — those would either fail in the
# upstream UI or risk leaking attacker-supplied scheme metadata into
# the operator's browser session.
_SAFE_SCHEMES = ("http://", "https://")


def is_lookupable_url(image_url: str) -> bool:
    """Return ``True`` when ``image_url`` is safe to paste into a
    third-party reverse-image search page.

    We require an explicit ``http://`` or ``https://`` prefix (covers
    the common CDN-served avatar case) and reject anything else. The
    SPA mirrors this check before rendering the chip row, but having
    it on the backend makes the opt-in handler defensive against
    malicious extractor output, too.
    """

    return any(image_url.startswith(scheme) for scheme in _SAFE_SCHEMES)


def build_lookup_urls(
    image_url: str,
    *,
    templates: dict[str, str],
) -> dict[str, str]:
    """Return ``{provider_slug: lookup_url}`` for every provider whose
    template was supplied.

    ``templates`` keys must match :data:`PROVIDERS` slugs; missing
    slugs are silently skipped so the caller can disable specific
    providers by withholding their template. Templates are expected
    to contain a ``{url}`` placeholder; templates without one are
    treated as opaque overrides and returned as-is (handy for
    admin-supplied private mirrors that already inline the image URL
    via a different mechanism).

    URL-encodes ``image_url`` with :func:`urllib.parse.quote`,
    treating ``:/`` as safe so the upstream's parser sees a
    well-formed http(s) URL on the other side of the placeholder.
    """

    if not is_lookupable_url(image_url):
        return {}
    encoded = quote(image_url, safe="")
    out: dict[str, str] = {}
    for provider in PROVIDERS:
        template = templates.get(provider.slug)
        if not template:
            continue
        if "{url}" in template:
            out[provider.slug] = template.replace("{url}", encoded)
        else:
            out[provider.slug] = template
    return out

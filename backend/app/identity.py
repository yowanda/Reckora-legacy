"""Phase 5a: identity-type aware searches.

Reckora's Phase 1-4 catalog is username-only — every site in
``data/sites.json`` accepts a username substituted into the
``{username}`` URL template. Phase 5a generalises the input to a small
set of **identity types**:

- ``username`` — default; any of letters, digits, ``_ . -`` (1-64 chars)
- ``email`` — RFC 5321ish ``addr-spec``
- ``phone`` — E.164 (``+`` followed by 7-15 digits, leading digit 1-9)
- ``gaia`` — 21-digit Google Account ID (Blogger-by-GAIA-id, etc.)
- ``yandex`` — 16-character lowercase alphanumeric Yandex public_id
- ``skype`` — Skype Name (``live:<alphanum>`` or 6-32 alphanumeric)

The catalog stays the same shape: each :class:`~app.sites.Site` carries
a tuple of identity types it accepts (defaulting to ``("username",)``
for backwards compatibility). A scan with ``identity_type=foo`` runs
only against sites whose ``identity_types`` includes ``foo``.

Some identifiers need a per-site transform before substitution into
``{username}`` — Gravatar, for example, expects the lower-cased
MD5 hex of the email rather than the email itself. Those transforms
are declared in :data:`IDENTITY_TRANSFORMS` and applied by the checker.

Sites in :data:`KNOWN_IDENTITY_OVERRIDES` are annotated with
non-default identity types at catalog load time. Admins can also
extend / override identity types per-site via the live DB editor by
patching ``identityTypes`` in the override JSON; that path skips the
curated map entirely.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Final, Literal

IdentityType = Literal["username", "email", "phone", "gaia", "yandex", "skype"]

ALL_IDENTITY_TYPES: Final[tuple[IdentityType, ...]] = (
    "username",
    "email",
    "phone",
    "gaia",
    "yandex",
    "skype",
)

DEFAULT_IDENTITY_TYPE: Final[IdentityType] = "username"

# Per-type human-readable metadata used by the SPA's picker to swap
# placeholder text + ``input[pattern]`` attributes when the user picks
# a non-default identity. Mirrored on the frontend in ``types.ts``.
IDENTITY_TYPE_LABELS: Final[dict[IdentityType, str]] = {
    "username": "username",
    "email": "email",
    "phone": "phone (E.164)",
    "gaia": "Google GAIA id",
    "yandex": "Yandex public_id",
    "skype": "Skype name",
}

# ---------------------------------------------------------------------------
# Per-type validators. Authoritative — the FastAPI ``SearchRequest``
# field validator dispatches into ``is_valid_identifier`` so the wire
# format matches the runtime checker exactly.
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_GAIA_RE = re.compile(r"^\d{21}$")
_YANDEX_RE = re.compile(r"^[a-z0-9]{16}$")
_SKYPE_RE = re.compile(r"^(?:live:)?[A-Za-z0-9_.\-]{6,32}$")


def is_valid_username(value: str) -> bool:
    return bool(_USERNAME_RE.match(value))


def is_valid_email(value: str) -> bool:
    if len(value) > 254:
        return False
    return bool(_EMAIL_RE.match(value))


def is_valid_phone(value: str) -> bool:
    return bool(_PHONE_RE.match(value))


def is_valid_gaia(value: str) -> bool:
    return bool(_GAIA_RE.match(value))


def is_valid_yandex(value: str) -> bool:
    return bool(_YANDEX_RE.match(value))


def is_valid_skype(value: str) -> bool:
    return bool(_SKYPE_RE.match(value))


_VALIDATORS: dict[IdentityType, Callable[[str], bool]] = {
    "username": is_valid_username,
    "email": is_valid_email,
    "phone": is_valid_phone,
    "gaia": is_valid_gaia,
    "yandex": is_valid_yandex,
    "skype": is_valid_skype,
}


def is_valid_identifier(value: str, identity_type: IdentityType) -> bool:
    """Return ``True`` iff ``value`` is well-formed for ``identity_type``."""
    validator = _VALIDATORS.get(identity_type)
    if validator is None:
        return False
    return validator(value)


def normalise_identifier(value: str, identity_type: IdentityType) -> str:
    """Strip + canonicalise an identifier to a comparable form.

    Applied **before** validation so callers don't have to remember to
    pre-trim. Per-type rules:

    - ``email``: stripped + lower-cased (RFC 5321 says local-part is
      case-sensitive, but in practice every operator treats it as
      case-insensitive).
    - ``phone``: stripped of every char except ``+`` and digits.
    - ``yandex``: stripped + lower-cased (public_id is always lower in
      practice).
    - everything else: stripped (case preserved).
    """
    v = value.strip()
    if identity_type == "email":
        return v.lower()
    if identity_type == "phone":
        return "".join(c for c in v if c == "+" or c.isdigit())
    if identity_type == "yandex":
        return v.lower()
    return v


# ---------------------------------------------------------------------------
# Curated identity-type overrides for sites in the bundled catalog.
#
# Applied after ``_coerce_site`` parses the raw maigret entry; the patch
# JSON in the admin DB editor takes precedence (an admin-supplied
# ``identityTypes`` field skips this map entirely). Sites NOT in this
# map default to ``("username",)``.
#
# Conservatively kept small — annotating a site here only makes sense
# if the URL template substitutes the identifier directly OR has a
# matching transform in :data:`IDENTITY_TRANSFORMS`.
# ---------------------------------------------------------------------------

KNOWN_IDENTITY_OVERRIDES: Final[dict[str, tuple[IdentityType, ...]]] = {
    # Gravatar's profile URL is built from the lower-cased MD5 of the
    # email; the transform below handles the hashing.
    "Gravatar": ("email",),
    # Google Account ID (GAIA) — Blogger profiles are addressed by GAIA
    # rather than a vanity username.
    "Blogger (by GAIA id)": ("gaia",),
    # Yandex public_id-addressed services. The 16-char ID isn't a
    # username — letting these through on a username scan would surface
    # 2,500+ false-positive UNKNOWNs because the URL pattern doesn't
    # actually accept arbitrary text.
    "YandexCollections API (by yandex_public_id)": ("yandex",),
    "YandexZnatoki": ("yandex",),
    "YandexReviews": ("yandex",),
    "YandexZenUser": ("yandex",),
    "YandexZenChannel": ("yandex",),
}


def _md5_hex_lower(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


# Per-(site, identity_type) input transform. Applied by the checker
# **after** ``normalise_identifier`` and **before** the
# ``{username}`` substitution. Defaults to identity (passes the value
# through unchanged) when no entry is registered.
IDENTITY_TRANSFORMS: Final[dict[tuple[str, IdentityType], Callable[[str], str]]] = {
    ("Gravatar", "email"): _md5_hex_lower,
}


def transform_identifier(site_name: str, identity_type: IdentityType, value: str) -> str:
    """Return the value as it should appear in the ``{username}`` slot
    of the site's URL template."""
    fn = IDENTITY_TRANSFORMS.get((site_name, identity_type))
    if fn is None:
        return value
    return fn(value)

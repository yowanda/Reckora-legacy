"""Phase 5a / Phase 10c: identity-type aware searches.

Reckora's Phase 1-4 catalog is username-only — every site in
``data/sites.json`` accepts a username substituted into the
``{username}`` URL template. Phase 5a generalised the input to a small
set of **identity types**, and Phase 10c rounds the set out to match
maigret's full primary set:

- ``username`` — default; any of letters, digits, ``_ . -`` (1-64 chars)
- ``email`` — RFC 5321ish ``addr-spec``
- ``phone`` — E.164 (``+`` followed by 7-15 digits, leading digit 1-9)
- ``gaia`` — 21-digit Google Account ID (Blogger-by-GAIA-id, etc.)
- ``yandex`` — 16-character lowercase alphanumeric Yandex public_id
- ``skype`` — Skype Name (``live:<alphanum>`` or 6-32 alphanumeric)
- ``vk_id`` — numeric VK user id (1-11 digits, drives ``vk.com/id{id}``)
- ``ok_id`` — numeric Odnoklassniki user id (9-18 digits)
- ``steam_id`` — 17-digit SteamID64 or legacy ``STEAM_X:Y:Z`` literal
- ``wikimapia_uid`` — numeric Wikimapia user id
- ``uidme_uguid`` — numeric uID.me uguid
- ``yelp_userid`` — 22-char base64-style Yelp internal user id

The catalog stays the same shape: each :class:`~app.sites.Site` carries
a tuple of identity types it accepts (defaulting to ``("username",)``
for backwards compatibility). A scan with ``identity_type=foo`` runs
only against sites whose ``identity_types`` includes ``foo``. Phase 10c
adds zero behaviour change for existing username/email/phone scans —
the new types only fire when the operator explicitly picks them.

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

IdentityType = Literal[
    "username",
    "email",
    "phone",
    "gaia",
    "yandex",
    "skype",
    "vk_id",
    "ok_id",
    "steam_id",
    "wikimapia_uid",
    "uidme_uguid",
    "yelp_userid",
]

ALL_IDENTITY_TYPES: Final[tuple[IdentityType, ...]] = (
    "username",
    "email",
    "phone",
    "gaia",
    "yandex",
    "skype",
    "vk_id",
    "ok_id",
    "steam_id",
    "wikimapia_uid",
    "uidme_uguid",
    "yelp_userid",
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
    "vk_id": "VK numeric id",
    "ok_id": "Odnoklassniki id",
    "steam_id": "Steam id",
    "wikimapia_uid": "Wikimapia uid",
    "uidme_uguid": "uID.me uguid",
    "yelp_userid": "Yelp user id",
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
# Phase 10c: VK numeric id — ``vk.com/id{id}`` accepts 1-11 digit ids.
_VK_ID_RE = re.compile(r"^[1-9]\d{0,10}$")
# Phase 10c: Odnoklassniki numeric id — 9-18 digits in practice.
_OK_ID_RE = re.compile(r"^[1-9]\d{8,17}$")
# Phase 10c: SteamID64 (17 digits, all start with 7656119) OR the legacy
# ``STEAM_X:Y:Z`` literal still accepted by community sites.
_STEAM_ID_RE = re.compile(r"^(?:7656119\d{10}|STEAM_[01]:[01]:\d{1,15})$")
# Phase 10c: Wikimapia internal user id — numeric, 1-12 digits.
_WIKIMAPIA_UID_RE = re.compile(r"^[1-9]\d{0,11}$")
# Phase 10c: uID.me uguid — numeric ``uguid`` (the ``uID.me (by uguid)``
# entry in ``data/sites.json`` ships with ``usernameClaimed=1050362129``;
# 6-15 digits covers the observed range without false-positives).
_UIDME_UGUID_RE = re.compile(r"^[1-9]\d{5,14}$")
# Phase 10c: Yelp internal user id — 22-char base64-ish slug, e.g.
# ``iX_lqhcobUBvM2S671_RyA`` (used by ``Yelp (by id)`` in maigret).
_YELP_USERID_RE = re.compile(r"^[A-Za-z0-9_\-]{22}$")


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


def is_valid_vk_id(value: str) -> bool:
    return bool(_VK_ID_RE.match(value))


def is_valid_ok_id(value: str) -> bool:
    return bool(_OK_ID_RE.match(value))


def is_valid_steam_id(value: str) -> bool:
    return bool(_STEAM_ID_RE.match(value))


def is_valid_wikimapia_uid(value: str) -> bool:
    return bool(_WIKIMAPIA_UID_RE.match(value))


def is_valid_uidme_uguid(value: str) -> bool:
    return bool(_UIDME_UGUID_RE.match(value))


def is_valid_yelp_userid(value: str) -> bool:
    return bool(_YELP_USERID_RE.match(value))


_VALIDATORS: dict[IdentityType, Callable[[str], bool]] = {
    "username": is_valid_username,
    "email": is_valid_email,
    "phone": is_valid_phone,
    "gaia": is_valid_gaia,
    "yandex": is_valid_yandex,
    "skype": is_valid_skype,
    "vk_id": is_valid_vk_id,
    "ok_id": is_valid_ok_id,
    "steam_id": is_valid_steam_id,
    "wikimapia_uid": is_valid_wikimapia_uid,
    "uidme_uguid": is_valid_uidme_uguid,
    "yelp_userid": is_valid_yelp_userid,
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
    if identity_type == "steam_id":
        # Operators paste ``steam_xxxx``; canonicalise to the upper-case
        # literal so the validator's case-sensitive prefix matches.
        return v.upper() if v.lower().startswith("steam_") else v
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
    # Phase 10c: VK numeric id — the URL hard-prefixes ``id`` so the
    # ``{username}`` slot must be a numeric VK uid.
    "VK (by id)": ("vk_id",),
    # ``my.mail.ru/vk/{vk_id}`` is a VK aggregator that addresses users
    # by VK numeric id; the catalog ships ``usernameClaimed=337779600``.
    "My.Mail.ru@VK": ("vk_id",),
    # Odnoklassniki accepts both vanity screen-names and numeric ids at
    # the same ``ok.ru/{id_or_name}`` URL — keep ``username`` so the
    # existing username scan still probes OK and add ``ok_id`` so a
    # numeric scan exercises the same site.
    "OK": ("username", "ok_id"),
    # Steam community profiles addressed by SteamID64 / STEAM_X:Y:Z —
    # the ``/profiles/{id}`` URL never accepts a vanity name.
    "Steam (by id)": ("steam_id",),
    "Steamid (by id)": ("steam_id",),
    "Steamidfinder (by id)": ("steam_id",),
    # Wikimapia user pages are keyed by numeric uid; ``wikimapia.org/
    # user/{uid}`` returns a 200 only for digits.
    "WikimapiaProfile": ("wikimapia_uid",),
    # uID.me's ``uguid`` endpoint is numeric-only.
    "uID.me (by uguid)": ("uidme_uguid",),
    # Yelp's internal user id is a 22-char slug — the ``user_details``
    # URL only resolves for that opaque format.
    "Yelp (by id)": ("yelp_userid",),
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

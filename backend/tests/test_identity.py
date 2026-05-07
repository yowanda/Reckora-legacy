"""Phase 5a: identity-type validators + transforms."""

from __future__ import annotations

import hashlib

import pytest

from app import identity

# ---------------------------------------------------------------------------
# Per-type validators.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("alice", True),
        ("alice_42", True),
        ("a.b-c", True),
        ("", False),
        ("a" * 65, False),
        ("bad name", False),
        ("../etc/passwd", False),
        ("hello@world", False),  # an @ kicks the value out of username land
    ],
)
def test_is_valid_username(value: str, expected: bool) -> None:
    assert identity.is_valid_username(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("alice@example.com", True),
        ("Alice.42@subdomain.example.co", True),
        ("user+tag@example.org", True),
        ("plainstring", False),
        ("@example.com", False),
        ("user@", False),
        ("user@host", False),  # no TLD
        ("a" * 256 + "@x.io", False),  # absurdly long
    ],
)
def test_is_valid_email(value: str, expected: bool) -> None:
    assert identity.is_valid_email(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("+12025551234", True),
        ("+442071838750", True),
        ("+6281234567", True),
        ("12025551234", False),  # missing leading +
        ("+0123456789", False),  # leading zero forbidden by E.164
        ("+", False),
        ("+12345", False),  # too short
        ("+1234567890123456", False),  # too long (16 digits)
    ],
)
def test_is_valid_phone(value: str, expected: bool) -> None:
    assert identity.is_valid_phone(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("123456789012345678901", True),
        ("12345678901234567890", False),  # 20 digits
        ("1234567890123456789012", False),  # 22 digits
        ("12345678901234567890a", False),  # non-digit
        ("", False),
    ],
)
def test_is_valid_gaia(value: str, expected: bool) -> None:
    assert identity.is_valid_gaia(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("aaaaaaaaaaaaaaaa", True),  # 16 lowercase
        ("0123456789abcdef", True),  # 16 hex-style
        ("AAAAAAAAAAAAAAAA", False),  # uppercase rejected
        ("abc123", False),  # too short
        ("0123456789abcdef0", False),  # too long
        ("", False),
    ],
)
def test_is_valid_yandex(value: str, expected: bool) -> None:
    assert identity.is_valid_yandex(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("live:johndoe", True),
        ("live:john.doe-42", True),
        ("johndoe42", True),
        ("hi", False),  # too short
        ("a" * 33, False),  # too long
        ("not allowed", False),  # space
    ],
)
def test_is_valid_skype(value: str, expected: bool) -> None:
    assert identity.is_valid_skype(value) is expected


# ---------------------------------------------------------------------------
# Dispatch helpers.
# ---------------------------------------------------------------------------


def test_is_valid_identifier_dispatches_per_type() -> None:
    assert identity.is_valid_identifier("alice", "username") is True
    assert identity.is_valid_identifier("alice@example.com", "email") is True
    assert identity.is_valid_identifier("+12025551234", "phone") is True
    assert identity.is_valid_identifier("123456789012345678901", "gaia") is True
    assert identity.is_valid_identifier("0123456789abcdef", "yandex") is True
    assert identity.is_valid_identifier("live:johndoe", "skype") is True
    # Phase 10c: dispatch must reach every new validator.
    assert identity.is_valid_identifier("270433952", "vk_id") is True
    assert identity.is_valid_identifier("123456789", "ok_id") is True
    assert identity.is_valid_identifier("76561197960287930", "steam_id") is True
    assert identity.is_valid_identifier("12345", "wikimapia_uid") is True
    assert identity.is_valid_identifier("1050362129", "uidme_uguid") is True
    assert identity.is_valid_identifier("iX_lqhcobUBvM2S671_RyA", "yelp_userid") is True


def test_is_valid_identifier_rejects_mismatched_format() -> None:
    # An email shouldn't validate as a username.
    assert identity.is_valid_identifier("alice@example.com", "username") is False
    # A username shouldn't validate as an email.
    assert identity.is_valid_identifier("alice", "email") is False
    # A phone without ``+`` doesn't validate.
    assert identity.is_valid_identifier("12025551234", "phone") is False


def test_is_valid_identifier_rejects_unknown_type() -> None:
    assert identity.is_valid_identifier("alice", "bogus") is False  # type: ignore[arg-type]


def test_normalise_identifier_per_type() -> None:
    # Whitespace stripped for every type.
    assert identity.normalise_identifier("  alice  ", "username") == "alice"
    # Email lower-cased.
    assert identity.normalise_identifier("Alice@Example.COM", "email") == "alice@example.com"
    # Phone strips spaces / dashes / parens.
    assert identity.normalise_identifier("+1 (202) 555-1234", "phone") == "+12025551234"
    # Yandex lower-cased.
    assert identity.normalise_identifier("ABCDEF0123456789", "yandex") == "abcdef0123456789"
    # Skype preserves case so ``live:`` prefix survives.
    assert identity.normalise_identifier("  Live:Alice  ", "skype") == "Live:Alice"


# ---------------------------------------------------------------------------
# Per-site transforms.
# ---------------------------------------------------------------------------


def test_transform_identifier_passthrough_by_default() -> None:
    assert identity.transform_identifier("Twitter", "username", "alice") == "alice"
    # Phone scan against an unannotated site → identifier passes through
    # raw so the URL template substitution stays predictable.
    assert identity.transform_identifier("Some Site", "phone", "+12025551234") == "+12025551234"


def test_transform_identifier_gravatar_email_md5() -> None:
    raw = "alice@example.com"
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest()
    out = identity.transform_identifier("Gravatar", "email", raw)
    assert out == expected
    # Sanity: 32 hex chars.
    assert len(out) == 32
    int(out, 16)  # raises on non-hex


# ---------------------------------------------------------------------------
# Curated site-overrides map.
# ---------------------------------------------------------------------------


def test_known_identity_overrides_shape() -> None:
    # Every override key must be a known identity type so that
    # ``catalog.filtered`` doesn't silently swallow typos.
    for site_name, types in identity.KNOWN_IDENTITY_OVERRIDES.items():
        assert types, f"{site_name!r} has empty identity_types"
        for t in types:
            assert t in identity.ALL_IDENTITY_TYPES, (
                f"{site_name!r} declares unknown identity_type {t!r}"
            )


def test_known_identity_overrides_includes_gravatar_and_yandex() -> None:
    # Pin the curated set so an accidental rename of the YAML / json
    # entries shows up as a test failure rather than silent breakage.
    assert identity.KNOWN_IDENTITY_OVERRIDES["Gravatar"] == ("email",)
    assert "yandex" in identity.KNOWN_IDENTITY_OVERRIDES["YandexZnatoki"]
    assert "gaia" in identity.KNOWN_IDENTITY_OVERRIDES["Blogger (by GAIA id)"]


# ---------------------------------------------------------------------------
# Phase 10c: identity-types parity (vk_id, ok_id, steam_id, wikimapia_uid,
# uidme_uguid, yelp_userid).
#
# Two tests per type:
#   1. validator accept / reject (parametrized)
#   2. catalog tagging + URL substitution (loads the live catalog so an
#      accidental drop in ``data/sites.json`` or ``KNOWN_IDENTITY_OVERRIDES``
#      surfaces here rather than as a silent zero-site filter result).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("270433952", True),  # 9-digit VK id
        ("1", True),  # single digit still valid
        ("99999999999", True),  # 11-digit upper bound
        ("0123", False),  # leading zero forbidden
        ("123456789012", False),  # 12 digits > upper bound
        ("alice", False),  # non-numeric rejected
        ("", False),
    ],
)
def test_is_valid_vk_id(value: str, expected: bool) -> None:
    assert identity.is_valid_vk_id(value) is expected


def test_vk_id_catalog_tagging_and_url() -> None:
    # The curated map drives ``_coerce_site``; ``VK (by id)`` flips
    # from username-only to vk_id-only so a numeric scan reaches it.
    assert identity.KNOWN_IDENTITY_OVERRIDES["VK (by id)"] == ("vk_id",)
    assert identity.KNOWN_IDENTITY_OVERRIDES["My.Mail.ru@VK"] == ("vk_id",)

    from app.sites import load_catalog

    cat = load_catalog()
    by_id = cat.by_name("VK (by id)")
    assert by_id is not None
    assert by_id.identity_types == ("vk_id",)
    # URL substitution stays the simple ``{username}`` swap.
    raw = "270433952"
    out = identity.transform_identifier("VK (by id)", "vk_id", raw)
    assert out == raw
    assert by_id.url.format(username=out) == f"https://vk.com/id{raw}"
    # Vanity ``VK`` stays username-only so existing scans don't change.
    vanity = cat.by_name("VK")
    assert vanity is not None and vanity.identity_types == ("username",)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("123456789", True),  # 9-digit lower bound
        ("123456789012345678", True),  # 18-digit upper bound
        ("12345678", False),  # 8 digits < lower bound
        ("1234567890123456789", False),  # 19 digits > upper bound
        ("0123456789", False),  # leading zero
        ("abcdefghi", False),
        ("", False),
    ],
)
def test_is_valid_ok_id(value: str, expected: bool) -> None:
    assert identity.is_valid_ok_id(value) is expected


def test_ok_id_catalog_tagging_and_url() -> None:
    # OK accepts both vanity screen-names and numeric ids at the same
    # URL — the curated map keeps ``username`` in place so the existing
    # username scan still hits OK and adds ``ok_id`` so a numeric scan
    # also exercises the same site.
    assert identity.KNOWN_IDENTITY_OVERRIDES["OK"] == ("username", "ok_id")

    from app.sites import load_catalog

    cat = load_catalog()
    ok = cat.by_name("OK")
    assert ok is not None
    assert ok.identity_types == ("username", "ok_id")
    raw = "123456789"
    out = identity.transform_identifier("OK", "ok_id", raw)
    assert out == raw
    assert ok.url.format(username=out) == f"https://ok.ru/{raw}"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("76561197960287930", True),  # canonical SteamID64
        ("76561197982198022", True),  # second canonical id
        ("STEAM_0:0:11101", True),  # legacy STEAM_X:Y:Z
        ("STEAM_1:1:42", True),
        ("12345678901234567", False),  # 17 digits but wrong prefix
        ("steam_0:0:11101", False),  # case-sensitive prefix
        ("STEAM_2:0:11101", False),  # X must be 0 or 1
        ("", False),
    ],
)
def test_is_valid_steam_id(value: str, expected: bool) -> None:
    assert identity.is_valid_steam_id(value) is expected


def test_steam_id_catalog_tagging_and_url() -> None:
    # All three ``Steam … (by id)`` variants are flipped to steam_id-only.
    for site_name in ("Steam (by id)", "Steamid (by id)", "Steamidfinder (by id)"):
        assert identity.KNOWN_IDENTITY_OVERRIDES[site_name] == ("steam_id",)

    # ``normalise_identifier`` upper-cases the legacy ``steam_…`` prefix
    # so the validator's case-sensitive regex still matches.
    assert identity.normalise_identifier(" steam_0:0:11101 ", "steam_id") == "STEAM_0:0:11101"

    from app.sites import load_catalog

    cat = load_catalog()
    by_id = cat.by_name("Steam (by id)")
    assert by_id is not None
    assert by_id.identity_types == ("steam_id",)
    raw = "76561197960287930"
    out = identity.transform_identifier("Steam (by id)", "steam_id", raw)
    assert out == raw
    assert by_id.url.format(username=out) == f"https://steamcommunity.com/profiles/{raw}"
    # Vanity ``Steam`` stays username-only.
    vanity = cat.by_name("Steam")
    assert vanity is not None and vanity.identity_types == ("username",)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12345", True),
        ("1", True),
        ("999999999999", True),  # 12-digit upper bound
        ("0123", False),  # leading zero
        ("1234567890123", False),  # 13 digits > upper bound
        ("abc", False),
        ("", False),
    ],
)
def test_is_valid_wikimapia_uid(value: str, expected: bool) -> None:
    assert identity.is_valid_wikimapia_uid(value) is expected


def test_wikimapia_uid_catalog_tagging_and_url() -> None:
    assert identity.KNOWN_IDENTITY_OVERRIDES["WikimapiaProfile"] == ("wikimapia_uid",)

    from app.sites import load_catalog

    cat = load_catalog()
    site = cat.by_name("WikimapiaProfile")
    assert site is not None
    assert site.identity_types == ("wikimapia_uid",)
    raw = "12345"
    out = identity.transform_identifier("WikimapiaProfile", "wikimapia_uid", raw)
    assert out == raw
    assert site.url.format(username=out) == f"http://wikimapia.org/user/{raw}"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1050362129", True),  # 10-digit example from maigret data
        ("123456", True),  # 6-digit lower bound
        ("123456789012345", True),  # 15-digit upper bound
        ("12345", False),  # 5 digits < lower bound
        ("1234567890123456", False),  # 16 digits > upper bound
        ("0123456789", False),  # leading zero
        ("", False),
    ],
)
def test_is_valid_uidme_uguid(value: str, expected: bool) -> None:
    assert identity.is_valid_uidme_uguid(value) is expected


def test_uidme_uguid_catalog_tagging_and_url() -> None:
    assert identity.KNOWN_IDENTITY_OVERRIDES["uID.me (by uguid)"] == ("uidme_uguid",)

    from app.sites import load_catalog

    cat = load_catalog()
    site = cat.by_name("uID.me (by uguid)")
    assert site is not None
    assert site.identity_types == ("uidme_uguid",)
    raw = "1050362129"
    out = identity.transform_identifier("uID.me (by uguid)", "uidme_uguid", raw)
    assert out == raw
    assert site.url.format(username=out) == f"http://uid.me/uguid/{raw}"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("iX_lqhcobUBvM2S671_RyA", True),  # 22-char canonical example
        ("a" * 22, True),
        ("ABCDEFGHIJKLMNOPQRSTUV", True),
        ("with-hyphen0123456789a", True),
        ("a" * 21, False),  # too short
        ("a" * 23, False),  # too long
        ("has space12345678901234", False),
        ("", False),
    ],
)
def test_is_valid_yelp_userid(value: str, expected: bool) -> None:
    assert identity.is_valid_yelp_userid(value) is expected


def test_yelp_userid_catalog_tagging_and_url() -> None:
    assert identity.KNOWN_IDENTITY_OVERRIDES["Yelp (by id)"] == ("yelp_userid",)

    from app.sites import load_catalog

    cat = load_catalog()
    site = cat.by_name("Yelp (by id)")
    assert site is not None
    assert site.identity_types == ("yelp_userid",)
    raw = "iX_lqhcobUBvM2S671_RyA"
    out = identity.transform_identifier("Yelp (by id)", "yelp_userid", raw)
    assert out == raw
    assert site.url.format(username=out) == f"https://www.yelp.com/user_details?userid={raw}"

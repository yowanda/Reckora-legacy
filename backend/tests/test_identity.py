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
            assert (
                t in identity.ALL_IDENTITY_TYPES
            ), f"{site_name!r} declares unknown identity_type {t!r}"


def test_known_identity_overrides_includes_gravatar_and_yandex() -> None:
    # Pin the curated set so an accidental rename of the YAML / json
    # entries shows up as a test failure rather than silent breakage.
    assert identity.KNOWN_IDENTITY_OVERRIDES["Gravatar"] == ("email",)
    assert "yandex" in identity.KNOWN_IDENTITY_OVERRIDES["YandexZnatoki"]
    assert "gaia" in identity.KNOWN_IDENTITY_OVERRIDES["Blogger (by GAIA id)"]

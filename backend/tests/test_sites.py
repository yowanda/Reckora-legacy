"""Smoke tests for the site catalog loader."""

from __future__ import annotations

from app.sites import load_catalog


def test_catalog_loads() -> None:
    catalog = load_catalog()
    # Phase 1 engine expansion: build_sites.py now resolves uCoz/XenForo/
    # phpBB/Discourse/vBulletin/Flarum/op.gg/Wordpress-Author/etc., so the
    # catalog must be substantially bigger than the pre-expansion 555.
    assert len(catalog.sites) >= 1800
    sample = catalog.sites[0]
    assert "{username}" in sample.url
    assert sample.url_main


def test_filter_by_tag() -> None:
    catalog = load_catalog()
    social = catalog.filtered(tags=["social"], limit=50)
    assert social
    assert all("social" in [t.lower() for t in s.tags] for s in social)


def test_engine_derived_sites_present() -> None:
    """At least the major engines should contribute to the catalog."""
    catalog = load_catalog()
    engines_present = {s.engine for s in catalog.sites if s.engine}
    # uCoz alone contributes 600+ sites in Maigret upstream — its absence
    # would mean engine resolution silently broke.
    assert "uCoz" in engines_present
    assert "XenForo" in engines_present
    assert "Discourse" in engines_present


# ---------------------------------------------------------------------------
# Phase 5a: identity-type aware filtering.
# ---------------------------------------------------------------------------


def test_filter_by_identity_type_username_default() -> None:
    catalog = load_catalog()
    out = catalog.filtered(limit=50)
    # Default is ``username`` — every entry must declare it.
    assert all("username" in s.identity_types for s in out)


def test_filter_by_identity_type_email_isolates_known_sites() -> None:
    """Email scans should match only sites whose curated overrides declare ``email``."""
    catalog = load_catalog()
    email_sites = catalog.filtered(identity_type="email", limit=50)
    assert email_sites, "no email-aware sites surfaced — the override map regressed"
    names = {s.name for s in email_sites}
    assert "Gravatar" in names
    # Username-only sites must not leak into an email scan.
    for s in email_sites:
        assert "email" in s.identity_types


def test_filter_by_identity_type_yandex_isolates_yandex_sites() -> None:
    catalog = load_catalog()
    yandex_sites = catalog.filtered(identity_type="yandex", limit=50)
    assert yandex_sites, "no yandex-aware sites surfaced — the override map regressed"
    for s in yandex_sites:
        assert "yandex" in s.identity_types
        # Curated yandex annotations don't carry the username type, so
        # they must not show up on a username-only scan.
        assert "username" not in s.identity_types


def test_known_identity_overrides_apply_to_loaded_catalog() -> None:
    catalog = load_catalog()
    by_name = {s.name: s for s in catalog.sites}
    assert by_name["Gravatar"].identity_types == ("email",)
    assert by_name["Blogger (by GAIA id)"].identity_types == ("gaia",)

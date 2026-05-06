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

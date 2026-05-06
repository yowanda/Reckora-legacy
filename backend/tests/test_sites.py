"""Smoke tests for the site catalog loader."""
from __future__ import annotations

from app.sites import load_catalog


def test_catalog_loads() -> None:
    catalog = load_catalog()
    assert len(catalog.sites) > 100
    sample = catalog.sites[0]
    assert "{username}" in sample.url
    assert sample.url_main


def test_filter_by_tag() -> None:
    catalog = load_catalog()
    social = catalog.filtered(tags=["social"], limit=50)
    assert social
    assert all("social" in [t.lower() for t in s.tags] for s in social)
